import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grant_trace as gt  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_selftest_cases(self):
        for raw, canon, scheme in gt.SELFTEST_CASES:
            n = gt.normalize(raw)
            self.assertEqual((n.canonical, n.scheme), (canon, scheme), raw)

    def test_variants_contain_canonical_and_are_unique(self):
        for raw, canon, scheme in gt.SELFTEST_CASES:
            n = gt.normalize(raw)
            if scheme != "UNPARSED":
                self.assertIn(canon, n.variants)
                self.assertEqual(len(n.variants), len(set(n.variants)))

    def test_same_project_collapses(self):
        spellings = ["2014R1A6A1030419", "NRF-2014R1A6A1030419", "NRF‐2014R1A6A1030419", "NRF- 2014R1A6A1030419"]
        self.assertEqual(len({gt.normalize(s).canonical for s in spellings}), 1)

    def test_numeric_needs_funder_filter(self):
        for s in ("20018494", "1415184668", "20173010012940"):
            self.assertTrue(gt.normalize(s).needs_funder_filter)
        self.assertFalse(gt.normalize("2016R1A2B4014600").needs_funder_filter)

    def test_nrf_code_fragment_not_named(self):
        n = gt.normalize("2016R1A2B4014600")
        self.assertEqual(n.code, "R1A2B4")
        self.assertEqual(n.year, 2016)

    def test_escaped_unicode_in_string(self):
        n = gt.normalize("RS-2023\\u201300261088")
        self.assertEqual(n.canonical, "RS-2023-00261088")
        self.assertTrue(any("이스케이프" in x for x in n.notes))

    def test_unparsed_has_no_variants(self):
        for s in ("", "2022-", "RS-2023", "Gravitational Waves", "N/A"):
            n = gt.normalize(s)
            self.assertEqual(n.scheme, "UNPARSED")
            self.assertEqual(n.variants, [])


class TestFlatten(unittest.TestCase):
    def test_flatten_minimal(self):
        w = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/ABC", "title": "t", "publication_year": 2020,
             "type": "article", "cited_by_count": 3, "fwci": 1.2, "open_access": {"is_oa": True, "oa_status": "gold"},
             "authorships": [{"countries": ["KR", "US"], "institutions": [{"country_code": "KR", "display_name": "KAIST"}]}],
             "primary_topic": {"display_name": "X", "field": {"display_name": "F"}},
             "awards": [{"funder_award_id": "NRF-2016R1A2B4014600", "funder_id": "https://openalex.org/F4320322120"}]}
        r = gt._flatten(w)
        self.assertEqual(r["id"], "W1")
        self.assertEqual(r["doi"], "10.1/abc")
        self.assertEqual(r["intl"], 1)
        self.assertEqual(r["n_countries"], 2)
        self.assertEqual(r["kr_inst"], "KAIST")
        self.assertIn("F4320322120", r["awards_json"])


if __name__ == "__main__":
    unittest.main()
