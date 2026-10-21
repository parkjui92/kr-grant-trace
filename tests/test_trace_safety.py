import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grant_trace as gt

AWARD = "2016R1A2B4014600"


class TestSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "cache.db")

    def test_http_errors_do_not_leak_key(self):
        with patch.object(gt, "_api_key", return_value="SECRET"), patch.object(gt, "_get_urllib", side_effect=urllib.error.HTTPError("https://x?api_key=SECRET", 404, "missing", {}, None)):
            with self.assertRaises(gt.OpenAlexHTTPError) as caught: gt.http_get_json("https://x")
        self.assertEqual(caught.exception.status, 404)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_supported_page_size(self):
        with patch.object(gt, "_api_key", return_value=""), patch.object(gt, "http_get_json", return_value={}) as request:
            gt._works_page("x", gt.WORK_FIELDS)
        self.assertIn("per_page=100", request.call_args.args[0])

    def test_cursor_cap_is_reported(self):
        truncated = []
        with patch.object(gt, "_api_key", return_value="key"), patch.object(gt, "_works_page", return_value={"meta": {"count": 500, "next_cursor": "next"}, "results": [{"id": "W1"}]}):
            list(gt.iter_works("x", max_pages=1, truncated=truncated))
        self.assertEqual(truncated[0][1], "page_limit")

    def trace(self, works, refresh):
        args = SimpleNamespace(ids=[AWARD], file=None, column=None, db=self.db, refresh=refresh, batch=1, out=str(self.root / "papers.json"), format="json")
        with patch.object(gt, "_api_key", return_value="key"), patch.object(gt, "iter_works", return_value=iter(works)), contextlib.redirect_stderr(io.StringIO()): gt.trace(args)
        return json.loads((self.root / "papers.json").read_text())

    def test_refresh_replaces_previous_matches(self):
        self.trace([{"id": "W1", "awards": [{"funder_award_id": AWARD}]}], True)
        result = self.trace([], True)
        self.assertEqual(result["rows"], [])

    def test_failed_refresh_rolls_back(self):
        self.trace([{"id": "W1", "awards": [{"funder_award_id": AWARD}]}], True)
        def failing():
            raise RuntimeError("offline")
            yield
        args = SimpleNamespace(ids=[AWARD], file=None, column=None, db=self.db, refresh=True, batch=1, out=None, format="json")
        with patch.object(gt, "_api_key", return_value="key"), patch.object(gt, "iter_works", return_value=failing()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError): gt.trace(args)
        con = gt.open_db(self.db); self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 1)

    def test_coverage_counts_missing_doi_and_network_failures(self):
        source = self.root / "ntis.csv"
        with source.open("w", newline="") as f:
            w = csv.writer(f); w.writerow(["doi", "award"])
            w.writerows([["", AWARD], ["10.1/a", AWARD], ["10.1/b", AWARD]])
        args = SimpleNamespace(db=self.db, ntis=str(source), doi_column=None, column=None, out=str(self.root / "detail.csv"))
        with patch.object(gt, "http_get_json", side_effect=[gt.OpenAlexHTTPError(429), gt.OpenAlexHTTPError(404)]), contextlib.redirect_stdout(io.StringIO()) as output:
            gt.coverage(args)
        self.assertIn("NTIS 성과 3행", output.getvalue())
        rows = list(csv.DictReader(io.StringIO((self.root / "detail.csv").read_text())))
        self.assertEqual([r["판정"] for r in rows], ["DOI 누락", "조회 실패(미색인 여부 미확인)", "DOI가 OpenAlex에 없음"])

    def test_empty_coverage_is_explained(self):
        source = self.root / "ntis.csv"; source.write_text("doi,award\n")
        with self.assertRaisesRegex(SystemExit, "비어"):
            gt.coverage(SimpleNamespace(db=self.db, ntis=str(source), doi_column=None, column=None, out=None))

    def test_partial_collection_warning_survives_cache_reuse(self):
        args = SimpleNamespace(ids=[AWARD], file=None, column=None, db=self.db, refresh=True, batch=1, out=str(self.root / "papers.json"), format="json")
        def partial(expr, truncated):
            truncated.append(("x", 2020, 150))
            yield {"id": "W1", "awards": [{"funder_award_id": AWARD}]}
        with patch.object(gt, "_api_key", return_value=""), patch.object(gt, "iter_works", side_effect=partial), contextlib.redirect_stderr(io.StringIO()): gt.trace(args)
        args.refresh = False
        with patch.object(gt, "_api_key", return_value=""), patch.object(gt, "iter_works") as fetch, contextlib.redirect_stderr(io.StringIO()) as output: gt.trace(args)
        fetch.assert_not_called()
        self.assertIn("잘린 구간", output.getvalue())
        self.assertEqual(json.loads((self.root / "papers.json").read_text())["truncations"][0][2], 150)

    def test_numeric_award_requires_its_own_korean_funder(self):
        args = SimpleNamespace(ids=["20018494"], file=None, column=None, db=self.db, refresh=True, batch=1, out=str(self.root / "papers.json"), format="json")
        work = {"id": "W1", "awards": [{"funder_award_id": "20018494", "funder_id": "F_foreign"}, {"funder_award_id": "other", "funder_id": next(iter(gt.KR_FUNDERS))}]}
        with patch.object(gt, "_api_key", return_value="key"), patch.object(gt, "iter_works", return_value=iter([work])), contextlib.redirect_stderr(io.StringIO()): gt.trace(args)
        self.assertEqual(json.loads((self.root / "papers.json").read_text())["rows"], [])
