#!/usr/bin/env python3
"""kr-grant-trace — 한국 정부 R&D 과제번호 → OpenAlex 논문 추적.

표준 라이브러리만 쓴다. 명령:
  normalize  번호를 정규형·유형·질의 변형으로 푼다
  trace      번호(들)의 사사가 달린 논문을 OpenAlex에서 모아 CSV/JSON으로 낸다
  summarize  trace 결과를 과제·사업코드·연도별로 집계한다
  coverage   NTIS 성과 목록(DOI·과제번호 CSV)과 대조해 매칭률(하한)과 미매칭 사유를 낸다
  selftest   정규화 회귀·자체 점검

설계: docs/design.md
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

__version__ = "0.1.1"
API = "https://api.openalex.org"
PAGE_SIZE = 100  # supported list-page limit; grouped responses still allow 200

# OpenAlex 한국 펀더 ID(2026-10-20 조회). 숫자만인 번호는 이 목록으로 범위를 좁힌다.
KR_FUNDERS = {
    "F4320322120": "National Research Foundation of Korea",
    "F4320328359": "Ministry of Science and ICT",
    "F4320322030": "Ministry of Science, ICT and Future Planning",
    "F4320321408": "Ministry of Education",
    "F4320321681": "Ministry of Trade, Industry and Energy",
    "F4320322349": "Ministry of Education, Science and Technology",
    "F4320322007": "Ministry of Environment",
    "F4320322035": "Rural Development Administration",
    "F4320335489": "Institute for Information and Communications Technology Promotion",
    "F4320334879": "Korea Evaluation Institute of Industrial Technology",
    "F4320322107": "Korea Health Industry Development Institute",
    "F4320335199": "Korea Institute of Energy Technology Evaluation and Planning",
    "F4320325370": "National Research Council of Science and Technology",
    "F4320322068": "Korea Institute of Planning and Evaluation for Technology in Food, Agriculture, Forestry and Fisheries",
    "F4320336122": "Korea Institute of Planning and Evaluation for Technology in Food, Agriculture and Forestry",
    "F4320320671": "National Research Foundation (혼재: 남아공·싱가포르·한국)",
}

# ---------------------------------------------------------------------------
# 1. 정규화
# ---------------------------------------------------------------------------

DASHES = "‐‑‒–—―−－­"
_PREFIX_RE = re.compile(
    r"^(?:NRF|KEIT|NTIS|KETEP|KHIDI|IITP|MSIT|MOTIE|NO\.?|GRANT(?:\s+NO\.?)?|#)\s*[-:]?\s*",
    re.I,
)
# 'IITP-2025-RS-2022-00156353' / 'IITP-2022-2018-0-01423' — 앞의 연도는 연차, 뒤가 과제번호
_IITP_ANNUAL_RE = re.compile(r"^IITP-(\d{4})-(.+)$", re.I)


@dataclass
class Norm:
    raw: str
    canonical: str | None
    scheme: str                 # NRF | RS | IITP | NRF_OLD | KEIT8 | KETEP14 | NTIS10 | MOTIE_P | KHIDI | NST | IPET | UNPARSED
    year: int | None = None
    code: str | None = None     # 사업 코드 조각(NRF 'R1A2B4' 등). 이름을 붙이지 않는다
    variants: list[str] = field(default_factory=list)
    needs_funder_filter: bool = False
    notes: list[str] = field(default_factory=list)


def _clean(s: str) -> tuple[str, list[str]]:
    notes = []
    t = s.strip()
    t = unicodedata.normalize("NFKC", t)
    if re.search(r"\\u20[01][0-9a-fA-F]", t):
        t = re.sub(r"\\u(20[01][0-9a-fA-F])", lambda m: chr(int(m.group(1), 16)), t)
        notes.append("이스케이프 문자열(\\u2013 등)을 글자로 복원")
    if any(ch in t for ch in DASHES):
        t = "".join("-" if ch in DASHES else ch for ch in t)
        notes.append("유니코드 대시를 '-'로")
    t = t.strip("()[]{}<>「」『』\"' ")
    if " " in t or "  " in t:
        notes.append("공백 정리")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*-\s*", "-", t)          # 'NRF- 2018…', 'CRC-16-01- KRICT'
    t = re.sub(r"\s*\.\s*", ".", t)
    t = t.upper()
    m = _IITP_ANNUAL_RE.match(t)
    if m:
        notes.append(f"IITP 연차 접두 {m.group(1)} 제거")
        t = m.group(2)
    before = t
    t = _PREFIX_RE.sub("", t)
    if t != before:
        notes.append(f"접두어 '{before[:len(before)-len(t)].strip()}' 제거")
    t = t.replace(" ", "")
    return t, notes


_NRF_RE = re.compile(r"^(\d{4})([A-Z]\d(?:[A-Z]\d)+)(\d{4,})$")       # 2016 R1A2B4 014600
_NRF_HY_RE = re.compile(r"^\d{4}[A-Z0-9-]{8,}$")
_RS_RE = re.compile(r"^RS-?(\d{4})-?([A-Z]{2})?(\d{6,8})$")
_IITP_RE = re.compile(r"^(\d{4})-0-(\d{5})$")
_NRF_OLD_RE = re.compile(r"^(\d{4})-(\d{7})$")
_KHIDI_RE = re.compile(r"^(H[IRUO]\d{2}[A-Z]\d{4,})$")
_MOTIE_P_RE = re.compile(r"^P\d{7}$")
_NST_A_RE = re.compile(r"^([A-Z]{3})(\d{2})(\d{3})-(\d{3})$")          # CAP21034-000
_NST_B_RE = re.compile(r"^([A-Z]{3})-(\d{2})-(\d{2})-([A-Z]{3,6})$")     # CAP-16-01-KIST
_NST_C_RE = re.compile(r"^([A-Z]{3})-(\d{4})-([A-Z]{3,6})$")             # CRC-2001-NFRI
_IPET_RE = re.compile(r"^(\d{5,6})(?:-(\d{1,2}))?(?:-(\d)-([A-Z]{2,3}\d{2,3}))?$")
_DIGITS = {8: "KEIT8", 10: "NTIS10", 14: "KETEP14"}


def normalize(raw: str) -> Norm:
    t, notes = _clean(raw)
    n = Norm(raw=raw, canonical=None, scheme="UNPARSED", notes=notes)
    if not t or len(re.findall(r"\d", t)) < 3:
        n.notes.append("번호가 없음")
        return n

    m = _RS_RE.match(t)
    if m:
        y, letters, num = m.groups()
        if not letters and len(num) < 8:
            num = num.zfill(8)
            n.notes.append("RS 번호 8자리로 보정")
        n.canonical = f"RS-{y}-{letters or ''}{num}"
        n.scheme, n.year, n.code = "RS", int(y), letters
        n.variants = [n.canonical, n.canonical.replace("-", "‐"), n.canonical.replace("-", "–"),
                      f"RS-{y}{letters or ''}{num}"]
        return n

    m = _IITP_RE.match(t)
    if m:
        n.canonical = f"{m.group(1)}-0-{m.group(2)}"
        n.scheme, n.year = "IITP", int(m.group(1))
        n.variants = [n.canonical, n.canonical.replace("-", "‐"), n.canonical.replace("-", "–")]
        return n

    m = _NRF_OLD_RE.match(t)
    if m:
        n.canonical = t
        n.scheme, n.year = "NRF_OLD", int(m.group(1))
        n.variants = [t, f"NRF-{t}", t.replace("-", "‐")]
        return n

    compact = t.replace("-", "")
    m = _NRF_RE.match(compact)
    if m and _NRF_HY_RE.match(t):
        if "-" in t:
            n.notes.append("내부 하이픈 제거")
        n.canonical = compact
        n.scheme, n.year, n.code = "NRF", int(m.group(1)), m.group(2)
        n.variants = [compact, f"NRF-{compact}", f"NRF‐{compact}", f"NRF–{compact}"]
        if "-" in t:
            n.variants.append(t)                      # 입력에 쓰인 하이픈 형도 같이 묻는다
        return n

    m = _KHIDI_RE.match(t)
    if m:
        n.canonical, n.scheme = t, "KHIDI"
        n.year = 2000 + int(t[2:4])
        n.variants = [t]
        return n

    if _MOTIE_P_RE.match(t):
        n.canonical, n.scheme, n.variants = t, "MOTIE_P", [t]
        return n

    for rx, scheme in ((_NST_A_RE, "NST"), (_NST_B_RE, "NST"), (_NST_C_RE, "NST")):
        m = rx.match(t)
        if m:
            n.canonical, n.scheme, n.code = t, scheme, m.group(1)
            n.variants = [t, t.replace("-", "‐"), t.replace("-", "–")]
            return n

    if t.isdigit() and len(t) in _DIGITS:
        n.canonical, n.scheme = t, _DIGITS[len(t)]
        n.needs_funder_filter = True
        n.notes.append("숫자만인 번호 — 한국 펀더 필터와 함께 질의")
        n.variants = [t]
        if n.scheme == "KETEP14":
            n.year = int(t[:4])
        return n

    m = _IPET_RE.match(t)
    if m and len(t) >= 8:
        n.canonical, n.scheme = t, "IPET"
        n.variants = [t, t.replace("-", "‐"), t.replace("-", "–")]
        return n

    n.notes.append("알려진 유형과 맞지 않음")
    return n


# ---------------------------------------------------------------------------
# 2. HTTP (urllib → certifi → curl)
# ---------------------------------------------------------------------------

def _mailto() -> str:
    return os.environ.get("OPENALEX_MAILTO", "")


class OpenAlexHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"OpenAlex HTTP {status}")


def http_get_json(url: str, retries: int = 3) -> dict:
    if _api_key() and "api_key=" not in url:
        url += ("&" if "?" in url else "?") + "api_key=" + urllib.parse.quote(_api_key())
    if _mailto() and "mailto=" not in url:
        url += ("&" if "?" in url else "?") + "mailto=" + urllib.parse.quote(_mailto())
    last = None
    for attempt in range(retries):
        for fn in (_get_urllib, _get_certifi, _get_curl):
            try:
                body = fn(url)
                if body is None:
                    continue
                return json.loads(body)
            except (urllib.error.HTTPError, OpenAlexHTTPError) as e:
                status = e.code if isinstance(e, urllib.error.HTTPError) else e.status
                last = OpenAlexHTTPError(status)
                if status not in (429, 500, 502, 503, 504):
                    raise last from None
                break
            except Exception as e:          # noqa: BLE001
                last = e
        time.sleep(1.5 * (attempt + 1))
    if isinstance(last, OpenAlexHTTPError):
        raise last
    raise RuntimeError(f"OpenAlex 요청 실패 ({last.__class__.__name__}) — 연결·인증서 확인 후 재시도")


def _get_urllib(url, ctx=None):
    req = urllib.request.Request(url, headers={"User-Agent": f"kr-grant-trace/{__version__}"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return r.read().decode("utf-8")


def _get_certifi(url):
    try:
        import certifi, ssl  # noqa: PLC0415
    except ImportError:
        return None
    return _get_urllib(url, ssl.create_default_context(cafile=certifi.where()))


def _get_curl(url):
    p = subprocess.run(["curl", "-sS", "--max-time", "60", "-w", "\n%{http_code}",
                        "-A", f"kr-grant-trace/{__version__}", url], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("curl 연결 실패")
    body, status = p.stdout.rsplit("\n", 1)
    if int(status) >= 400:
        raise OpenAlexHTTPError(int(status))
    return body


WORK_FIELDS = ("id,doi,title,publication_year,type,cited_by_count,fwci,open_access,"
               "authorships,primary_topic,awards,is_retracted")


def _api_key() -> str:
    k = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not k:
        f = Path.home() / ".kr-grant-trace" / "key"
        if f.exists():
            k = f.read_text(encoding="utf-8").strip()
    return k


def _works_page(filter_expr: str, select: str, cursor: str | None = None, extra: dict | None = None) -> dict:
    params = {"filter": filter_expr, "per_page": PAGE_SIZE, "select": select}
    if cursor:
        params["cursor"] = cursor
    if _api_key():
        params["api_key"] = _api_key()
    if extra:
        params.update(extra)
    return http_get_json(f"{API}/works?" + urllib.parse.urlencode(params))


class Truncated(Exception):
    """키 없이 100건을 넘는 구간 — 받은 만큼만 돌려주고 잘렸다고 알린다."""


def iter_works(filter_expr: str, select: str = WORK_FIELDS, max_pages: int = 500, truncated: list | None = None):
    """OpenAlex works를 전부 돈다.

    - API 키(OPENALEX_API_KEY)가 있으면 cursor 페이징
    - 키가 없으면 cursor 2쪽부터 거부되므로(2026-10 변경) 연도별로 쪼개 100건 이하 구간만 받는다.
      한 연도가 100건을 넘으면 받은 만큼만 내고 `truncated`에 (filter, year, count)를 적는다.
    """
    first = _works_page(filter_expr, select, "*")
    total = first.get("meta", {}).get("count", 0)
    yield from first.get("results", [])
    if total <= len(first.get("results", [])):
        return
    if _api_key():
        cursor = first["meta"].get("next_cursor")
        pages = 1
        while cursor and pages < max_pages:
            d = _works_page(filter_expr, select, cursor)
            yield from d.get("results", [])
            cursor = d.get("meta", {}).get("next_cursor")
            pages += 1
        if cursor and truncated is not None:
            truncated.append((filter_expr[:60], "page_limit", total))
        return
    # 키 없음 → 연도 슬라이스. 첫 쪽은 이미 냈으므로 중복은 호출 쪽이 id로 걸러야 한다
    seen = {w["id"] for w in first.get("results", [])}
    years = http_get_json(f"{API}/works?" + urllib.parse.urlencode(
        {"filter": filter_expr, "group_by": "publication_year", "per_page": 200})).get("group_by", [])
    for g in years:
        y, cnt = g.get("key"), g.get("count", 0)
        if not y or y == "unknown":
            continue
        d = _works_page(f"{filter_expr},publication_year:{y}", select, "*")
        got = 0
        for w in d.get("results", []):
            got += 1
            if w["id"] in seen:
                continue
            seen.add(w["id"])
            yield w
        if cnt > got:
            if truncated is not None:
                truncated.append((filter_expr[:60], y, cnt))
            print(f"  ! {y}년 구간 {cnt}건 중 {got}건만 받음 — OPENALEX_API_KEY를 주면 전부 받습니다", file=sys.stderr)


# ---------------------------------------------------------------------------
# 3. 캐시 (SQLite)
# ---------------------------------------------------------------------------

def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS works(
      id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INT, type TEXT,
      cited_by INT, fwci REAL, is_oa INT, oa_status TEXT, intl INT, n_countries INT,
      kr_inst TEXT, topic TEXT, field TEXT, awards_json TEXT, retracted INT, fetched TEXT);
    CREATE TABLE IF NOT EXISTS matches(
      canonical TEXT, scheme TEXT, work_id TEXT, matched_variant TEXT, run TEXT,
      PRIMARY KEY(canonical, work_id));
    CREATE TABLE IF NOT EXISTS queries(
      canonical TEXT PRIMARY KEY, scheme TEXT, variants_json TEXT, n_works INT, fetched TEXT);
    CREATE INDEX IF NOT EXISTS ix_matches_work ON matches(work_id);
    """)
    if "truncations_json" not in {r[1] for r in con.execute("PRAGMA table_info(queries)")}:
        con.execute("ALTER TABLE queries ADD COLUMN truncations_json TEXT")
        con.commit()
    return con


def _flatten(w: dict) -> dict:
    auth = w.get("authorships") or []
    countries = set()
    kr_inst = []
    for a in auth:
        for c in a.get("countries") or []:
            countries.add(c)
        for inst in a.get("institutions") or []:
            if inst.get("country_code") == "KR" and inst.get("display_name"):
                kr_inst.append(inst["display_name"])
    oa = w.get("open_access") or {}
    topic = w.get("primary_topic") or {}
    return dict(
        id=w["id"].rsplit("/", 1)[-1], doi=(w.get("doi") or "").replace("https://doi.org/", "").lower() or None,
        title=w.get("title"), year=w.get("publication_year"), type=w.get("type"),
        cited_by=w.get("cited_by_count"), fwci=w.get("fwci"),
        is_oa=int(bool(oa.get("is_oa"))), oa_status=oa.get("oa_status"),
        intl=int(len(countries) > 1), n_countries=len(countries),
        kr_inst="; ".join(sorted(set(kr_inst))[:5]) or None,
        topic=topic.get("display_name"), field=((topic.get("field") or {}).get("display_name")),
        awards_json=json.dumps([{"id": a.get("funder_award_id"), "funder": (a.get("funder_id") or "").rsplit("/", 1)[-1]}
                                for a in (w.get("awards") or [])], ensure_ascii=False),
        retracted=int(bool(w.get("is_retracted"))), fetched=date.today().isoformat(),
    )


# ---------------------------------------------------------------------------
# 4. trace
# ---------------------------------------------------------------------------

def _read_ids(args) -> list[str]:
    ids = list(args.ids or [])
    if args.file:
        p = Path(args.file)
        if p.suffix.lower() == ".csv":
            with p.open(encoding="utf-8-sig", newline="") as f:
                rd = csv.DictReader(f)
                col = args.column or _guess_col(rd.fieldnames, ("과제번호", "과제고유번호", "award", "grant", "project"))
                if not col:
                    sys.exit(f"CSV에서 과제번호 열을 못 찾았습니다. --column 지정: {rd.fieldnames}")
                ids += [r[col] for r in rd if r.get(col)]
        else:
            ids += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return ids


def _guess_col(names, keys):
    for nme in names or []:
        low = nme.lower()
        if any(k.lower() in low for k in keys):
            return nme
    return None


def _funder_filter() -> str:
    return "funders.id:" + "|".join(KR_FUNDERS)


def trace(args):
    if args.batch < 1:
        raise SystemExit("--batch 는 1 이상이어야 합니다")
    ids = _read_ids(args)
    if not ids:
        sys.exit("과제번호를 주세요 (인자 또는 --file)")
    con = open_db(args.db)
    run = date.today().isoformat()
    norms = {}
    for raw in ids:
        n = normalize(raw)
        if n.scheme == "UNPARSED":
            norms.setdefault("__unparsed__", []).append(n)
        else:
            norms.setdefault(n.canonical, n)

    unparsed = norms.pop("__unparsed__", [])
    todo = [n for n in norms.values() if args.refresh or not con.execute(
        "SELECT 1 FROM queries WHERE canonical=?", (n.canonical,)).fetchone()]
    print(f"번호 {len(ids)}개 → 정규형 {len(norms)}개 (미해석 {len(unparsed)}), 새로 질의 {len(todo)}개", file=sys.stderr)

    # 변형 전개 → 묶음 질의. 펀더 필터가 필요한 것은 따로 묶는다
    truncated_all: list = []
    for n in norms.values():
        if n not in todo:
            cached = con.execute("SELECT truncations_json FROM queries WHERE canonical=?", (n.canonical,)).fetchone()
            if cached and cached[0] is not None:
                truncated_all.extend(json.loads(cached[0]))
            else:
                print(f"! {n.canonical}: 이전 버전 캐시는 수집 완전성 정보가 없음 — --refresh 권장", file=sys.stderr)
    groups = {False: [], True: []}
    for n in todo:
        groups[n.needs_funder_filter].append(n)
    if not _api_key() and args.batch > 1:
        print("키 없음 → 연도 슬라이스 경로를 위해 과제 1건씩 질의(--batch 1)", file=sys.stderr)
        args.batch = 1
    for needs_f, bucket in groups.items():
        chunks, chunk, variant_count = [], [], 0
        for n in bucket:
            if chunk and (len(chunk) >= args.batch or variant_count + len(n.variants) > 100):
                chunks.append(chunk)
                chunk, variant_count = [], 0
            chunk.append(n)
            variant_count += len(n.variants)
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            variant_to_norm = {}
            for n in chunk:
                for v in n.variants:
                    variant_to_norm[v] = n
            expr = "awards.funder_award_id:" + "|".join(variant_to_norm)
            if needs_f:
                expr += "," + _funder_filter()
            found = defaultdict(set)
            trunc: list = []
            with con:
                for n in chunk:
                    con.execute("DELETE FROM matches WHERE canonical=?", (n.canonical,))
                for w in iter_works(expr, truncated=trunc):
                    row = _flatten(w)
                    con.execute("INSERT OR REPLACE INTO works VALUES (:id,:doi,:title,:year,:type,:cited_by,:fwci,:is_oa,"
                                ":oa_status,:intl,:n_countries,:kr_inst,:topic,:field,:awards_json,:retracted,:fetched)", row)
                    for a in w.get("awards") or []:
                        fid = a.get("funder_award_id") or ""
                        hit = variant_to_norm.get(fid)
                        if hit is None:
                            # 응답의 표기가 우리 변형 목록과 달라도 정규화하면 같은 과제일 수 있다
                            nn = normalize(fid)
                            hit = next((n for n in chunk if n.canonical == nn.canonical), None)
                        if hit and hit.needs_funder_filter and (a.get("funder_id") or "").rsplit("/", 1)[-1] not in KR_FUNDERS:
                            hit = None
                        if hit:
                            found[hit.canonical].add(row["id"])
                            con.execute("INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?)",
                                        (hit.canonical, hit.scheme, row["id"], fid, run))
                for n in chunk:
                    con.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?,?)",
                                (n.canonical, n.scheme, json.dumps(n.variants, ensure_ascii=False), len(found[n.canonical]), run, json.dumps(trunc)))
                if trunc:
                    truncated_all.extend(trunc)
            print(f"  질의 {len(chunk)}개 → 논문 {sum(len(v) for v in found.values())}건", file=sys.stderr)

    # 출력
    canon = list(norms)
    rows = con.execute(f"""
      SELECT m.canonical, m.scheme, m.matched_variant, w.* FROM matches m JOIN works w ON w.id=m.work_id
      WHERE m.canonical IN ({','.join('?'*len(canon))}) ORDER BY m.canonical, w.year, w.id""", canon).fetchall() if canon else []
    cols = ["canonical", "scheme", "matched_variant"] + [c[1] for c in con.execute("PRAGMA table_info(works)")] + ["year_flag"]
    # 과제 연도보다 2년 이상 앞선 논문은 OpenAlex 오귀속일 가능성 — 지우지 않고 표시한다
    def _flag(r):
        y = norms[r[0]].year if r[0] in norms else None
        py = r[cols.index("year")]
        return 1 if (y and isinstance(py, int) and py < y - 1) else 0
    rows = [tuple(r) + (_flag(r),) for r in rows]
    out = Path(args.out) if args.out else None
    if args.format == "json" or (out and out.suffix == ".json"):
        data = {"run": run, "credit_rule": "full (각 과제에 논문 1건씩 — summarize --credit 로 변경)",
                "truncations": truncated_all, "unparsed": [asdict(n) for n in unparsed], "rows": [dict(zip(cols, r)) for r in rows]}
        text = json.dumps(data, ensure_ascii=False, indent=1)
    else:
        import io
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(cols)
        wr.writerows(rows)
        text = buf.getvalue()
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"→ {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)

    # 번호별 건수·정직성 보고
    per = Counter(r[0] for r in rows)
    flagged = sum(r[-1] for r in rows)
    if flagged:
        print(f"\n! 과제 연도보다 앞선 논문 {flagged}건(year_flag=1) — 오귀속 의심, 지우지 않고 표시만 함", file=sys.stderr)
    print("\n번호별 논문 건수 (0건 ≠ 성과 없음 — 사사 미기재·비색인은 못 잡는다):", file=sys.stderr)
    for c in canon:
        n = norms[c]
        print(f"  {c:28s} {n.scheme:8s} {per.get(c, 0):5d}건", file=sys.stderr)
    for n in unparsed:
        print(f"  {n.raw!r:28s} UNPARSED  질의 안 함 — {'; '.join(n.notes)}", file=sys.stderr)
    if truncated_all:
        print(f"! 잘린 구간 {len(truncated_all)}개 — 위 건수는 하한보다 더 낮다. OPENALEX_API_KEY(무료, openalex.org/settings/api)를 주고 --refresh", file=sys.stderr)
    if not _api_key():
        print("키 없음: 공용 일일 예산을 씀. 많이 돌리려면 OPENALEX_API_KEY 설정", file=sys.stderr)
    print(f"조회일 {run} · OpenAlex 스냅샷은 매주 바뀜 · 캐시 {args.db}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 5. summarize
# ---------------------------------------------------------------------------

def _load_rows(path: str) -> list[dict]:
    p = Path(path)
    if p.suffix == ".json":
        return json.loads(p.read_text(encoding="utf-8"))["rows"]
    with p.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _num(x):
    try:
        return float(x) if x not in (None, "", "None") else None
    except ValueError:
        return None


def summarize(args):
    rows = _load_rows(args.input)
    # 계수 규칙
    by_work = defaultdict(list)
    for r in rows:
        by_work[r["id"]].append(r)
    weighted = []
    for wid, rs in by_work.items():
        if args.credit == "first":
            rs = [sorted(rs, key=lambda r: r["canonical"])[0]]
        wgt = 1.0 / len(rs) if args.credit == "split" else 1.0
        for r in rs:
            weighted.append((r, wgt))

    def key_of(r):
        if args.by == "award":
            return r["canonical"]
        if args.by == "code":
            n = normalize(r["canonical"])
            return f"{n.scheme}:{n.code or '-'}"
        if args.by == "year":
            return str(r.get("year"))
        return "ALL"

    groups = defaultdict(list)
    for r, wgt in weighted:
        groups[key_of(r)].append((r, wgt))

    med = lambda xs: (statistics.median(xs) if xs else None)  # noqa: E731
    print(f"# 집계 ({args.by}) · 계수 규칙 {args.credit} · 논문(고유) {len(by_work)}건 · 행 {len(rows)}")
    print("| 묶음 | 논문(가중) | 인용 중앙값 | FWCI 중앙값 | 국제공저율 | OA율 | 연도 범위 |")
    print("|---|---|---|---|---|---|---|")
    for k in sorted(groups):
        g = groups[k]
        n = sum(w for _, w in g)
        cit = [_num(r["cited_by"]) for r, _ in g if _num(r["cited_by"]) is not None]
        fw = [_num(r["fwci"]) for r, _ in g if _num(r["fwci"]) is not None]
        intl = sum(w for r, w in g if str(r.get("intl")) == "1")
        oa = sum(w for r, w in g if str(r.get("is_oa")) == "1")
        yrs = [int(r["year"]) for r, _ in g if str(r.get("year")).isdigit()]
        print(f"| {k} | {n:g} | {med(cit)} | {round(med(fw),2) if fw else None} | "
              f"{intl/n:.0%} | {oa/n:.0%} | {min(yrs) if yrs else ''}–{max(yrs) if yrs else ''} |")
    print("\n주의: 사업 코드는 조각만 보여 준다(이름은 사용자가 붙임). 인용·FWCI는 조회 시점 값.")


# ---------------------------------------------------------------------------
# 6. coverage
# ---------------------------------------------------------------------------

def coverage(args):
    """NTIS 행 전체를 분모로 삼고 미색인·조회 실패·DOI 누락을 구분한다."""
    con = open_db(args.db)
    with Path(args.ntis).open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        doi_col = args.doi_column or _guess_col(rd.fieldnames, ("doi",))
        id_col = args.column or _guess_col(rd.fieldnames, ("과제번호", "과제고유번호", "award", "grant"))
        if doi_col not in (rd.fieldnames or []) or id_col not in (rd.fieldnames or []):
            raise SystemExit(f"DOI/과제번호 열을 못 찾았습니다: {rd.fieldnames}")
        ntis = [((r.get(doi_col) or "").strip().lower().replace("https://doi.org/", ""),
                 (r.get(id_col) or "").strip()) for r in rd]
    total = len(ntis)
    if not total:
        raise SystemExit("NTIS 성과 목록이 비어 있어 매칭률을 계산하지 않습니다")
    reasons, detail = Counter(), []
    for doi, raw in ntis:
        n = normalize(raw)
        verdict = None
        awards = []
        if not doi:
            verdict = "DOI 누락"
        elif n.scheme == "UNPARSED":
            verdict = "번호 미해석"
        else:
            w = con.execute("SELECT awards_json FROM works WHERE doi=?", (doi,)).fetchone()
            if w:
                awards = json.loads(w[0])
            else:
                try:
                    d = http_get_json(f"{API}/works/https://doi.org/{urllib.parse.quote(doi)}?select=id,awards")
                    awards = [{"id": a.get("funder_award_id"), "funder": (a.get("funder_id") or "").rsplit("/", 1)[-1]}
                              for a in d.get("awards") or []]
                except OpenAlexHTTPError as e:
                    verdict = "DOI가 OpenAlex에 없음" if e.status == 404 else "조회 실패(미색인 여부 미확인)"
                except RuntimeError:
                    verdict = "조회 실패(미색인 여부 미확인)"
            if verdict is None:
                if not awards:
                    verdict = "OpenAlex에 있으나 사사 정보 없음"
                elif any(normalize(a["id"]).canonical == n.canonical and
                         (not n.needs_funder_filter or a.get("funder") in KR_FUNDERS)
                         for a in awards if a.get("id")):
                    verdict = "매칭"
                else:
                    verdict = "사사는 있으나 다른 번호·펀더"
        reasons[verdict] += 1
        detail.append((doi, raw, verdict))
    matched = reasons["매칭"]
    print(f"# 커버리지 — NTIS 성과 {total}행 중 OpenAlex 사사 매칭 {matched}행 ({matched/total:.1%}) **하한**")
    print("분모는 입력 CSV의 모든 데이터 행(DOI 누락 포함, 중복행 자동 제거 안 함). 조회 실패는 미색인으로 단정하지 않습니다.\n")
    print("| 사유 | 건수 |\n|---|---|")
    for k, v in reasons.most_common():
        print(f"| {k} | {v} |")
    if args.out:
        with Path(args.out).open("w", encoding="utf-8", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["doi", "ntis_award", "판정"])
            wr.writerows(detail)
        print(f"\n→ 상세 {args.out}")


# ---------------------------------------------------------------------------
# 7. normalize 명령 · selftest
# ---------------------------------------------------------------------------

def cmd_normalize(args):
    ids = _read_ids(args)
    rows = [asdict(normalize(i)) for i in ids]
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print("| 입력 | 정규형 | 유형 | 연도 | 코드 | 메모 |\n|---|---|---|---|---|---|")
    for r in rows:
        print(f"| `{r['raw']}` | {r['canonical'] or '—'} | {r['scheme']} | {r['year'] or ''} | {r['code'] or ''} | {'; '.join(r['notes'])} |")


SELFTEST_CASES = [
    # (입력, 정규형, 유형)
    ("2016R1A2B4014600", "2016R1A2B4014600", "NRF"),
    ("NRF-2014R1A6A1030419", "2014R1A6A1030419", "NRF"),
    ("NRF‐2012M3A9C4048762", "2012M3A9C4048762", "NRF"),
    ("NRF- 2018R1A5A1024127", "2018R1A5A1024127", "NRF"),
    ("2019R1-A6A1-A10073437", "2019R1A6A1A10073437", "NRF"),
    ("2020M3‐A9G7‐103933", "2020M3A9G7103933", "NRF"),
    ("2012S1A5A2A03034254", "2012S1A5A2A03034254", "NRF"),
    ("RS-2025-18362970", "RS-2025-18362970", "RS"),
    ("RS‐2024‐00392705", "RS-2024-00392705", "RS"),
    ("RS-2023\\u201300261088", "RS-2023-00261088", "RS"),
    ("RS-202300301792", "RS-2023-00301792", "RS"),
    ("RS-2019-II190075", "RS-2019-II190075", "RS"),
    ("RS-2023-KH141546", "RS-2023-KH141546", "RS"),
    ("IITP-2025-RS-2022-00156353", "RS-2022-00156353", "RS"),
    ("2019-0-01842", "2019-0-01842", "IITP"),
    ("2020‐0‐00594", "2020-0-00594", "IITP"),
    ("IITP-2022-2018-0-01423", "2018-0-01423", "IITP"),
    ("2011-0023612", "2011-0023612", "NRF_OLD"),
    ("20018494", "20018494", "KEIT8"),
    ("NO. 10052980", "10052980", "KEIT8"),
    ("KEIT 20018560", "20018560", "KEIT8"),
    ("20173010012940", "20173010012940", "KETEP14"),
    ("No. 20174030201460", "20174030201460", "KETEP14"),
    ("NTIS 1415184668", "1415184668", "NTIS10"),
    ("P0022336", "P0022336", "MOTIE_P"),
    ("HI14C1324", "HI14C1324", "KHIDI"),
    ("HI22C132200", "HI22C132200", "KHIDI"),
    ("CAP21034-000", "CAP21034-000", "NST"),
    ("GTL24041‐000", "GTL24041-000", "NST"),
    ("CAP-16-01-KIST", "CAP-16-01-KIST", "NST"),
    ("CRC-16-01- KRICT", "CRC-16-01-KRICT", "NST"),
    ("CRC-2001-NFRI", "CRC-2001-NFRI", "NST"),
    ("117043-3", "117043-3", "IPET"),
    ("(716002-7)", "716002-7", "IPET"),
    ("316033-04-2-SB030", "316033-04-2-SB030", "IPET"),
    ("311030‐03‐1‐HD120", "311030-03-1-HD120", "IPET"),
    ("2022-", None, "UNPARSED"),
    ("RS-2023", None, "UNPARSED"),
    ("", None, "UNPARSED"),
    ("Gravitational Waves", None, "UNPARSED"),
]


def selftest(_args=None) -> int:
    fails = 0
    for raw, canon, scheme in SELFTEST_CASES:
        n = normalize(raw)
        ok = n.canonical == canon and n.scheme == scheme
        if not ok:
            fails += 1
            print(f"FAIL {raw!r}: got {n.canonical} {n.scheme}, want {canon} {scheme}")
    # 변형 전개가 정규형을 포함하고, 미해석은 변형이 없어야 한다
    for raw, canon, scheme in SELFTEST_CASES:
        n = normalize(raw)
        if scheme != "UNPARSED" and canon not in n.variants:
            fails += 1; print(f"FAIL variants {raw!r}: {n.variants}")
        if scheme == "UNPARSED" and n.variants:
            fails += 1; print(f"FAIL unparsed has variants {raw!r}")
    # 숫자만인 번호는 펀더 필터 필요
    for raw in ("20018494", "1415184668", "20173010012940"):
        if not normalize(raw).needs_funder_filter:
            fails += 1; print(f"FAIL funder filter {raw}")
    # 멱등성
    for raw, canon, scheme in SELFTEST_CASES:
        if canon and normalize(canon).canonical != canon:
            fails += 1; print(f"FAIL idempotent {canon}")
    print(f"selftest: {len(SELFTEST_CASES)} cases, {fails} fail")
    return 1 if fails else 0


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="grant_trace", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("KR_GRANT_TRACE_DB", str(Path.home() / ".kr-grant-trace" / "cache.db")))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_id_args(p):
        p.add_argument("ids", nargs="*", help="과제번호")
        p.add_argument("--file", help="번호 목록(txt 한 줄 하나 / csv)")
        p.add_argument("--column", help="csv의 과제번호 열 이름")

    p = sub.add_parser("normalize", help="번호 정규화"); add_id_args(p)
    p.add_argument("--format", choices=["md", "json"], default="md"); p.set_defaults(fn=cmd_normalize)

    p = sub.add_parser("trace", help="OpenAlex에서 논문 추적"); add_id_args(p)
    p.add_argument("--out", help="출력 파일(.csv/.json)")
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--batch", type=int, default=20, help="한 질의에 묶는 과제 수")
    p.add_argument("--refresh", action="store_true", help="캐시 무시하고 다시 질의")
    p.set_defaults(fn=trace)

    p = sub.add_parser("summarize", help="trace 결과 집계")
    p.add_argument("input"); p.add_argument("--by", choices=["award", "code", "year", "all"], default="award")
    p.add_argument("--credit", choices=["full", "split", "first"], default="full",
                   help="한 논문에 과제가 여럿일 때: full=각 1 / split=1/n / first=정렬 첫 과제만")
    p.set_defaults(fn=summarize)

    p = sub.add_parser("coverage", help="NTIS 성과 목록과 대조(하한)")
    p.add_argument("ntis", help="NTIS 성과 CSV (DOI·과제번호 열)")
    p.add_argument("--doi-column"); p.add_argument("--column"); p.add_argument("--out")
    p.set_defaults(fn=coverage)

    p = sub.add_parser("selftest"); p.set_defaults(fn=lambda a: sys.exit(selftest(a)))
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    main()
