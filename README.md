# kr-grant-trace — 한국 R&D 과제번호 → 논문 추적

한국 정부 R&D **과제번호**(한국연구재단·RS 통합번호·IITP·KEIT·KETEP·KHIDI·NST·IPET 등)를 넣으면 OpenAlex에서 그 과제를 사사(acknowledgement)로 단 논문을 모아 CSV로 내고, 과제·사업코드·연도별로 집계합니다. NTIS 성과 목록과 대조해 **매칭률을 하한으로** 보고합니다.

표준 라이브러리만 씁니다. 설치 없음, API 키 없이 기본 조회가 가능하며, 대량 수집에는 무료 키를 권장합니다. 키가 있어도 페이지 상한에 걸리면 부분 수집으로 표시합니다.

```bash
python3 grant_trace.py trace 2016R1A2B4014600 RS-2022-00155911 2019-0-01842 --out papers.csv
python3 grant_trace.py summarize papers.csv --by award --credit split
```

## 언제 쓰면 좋은가 · 강점

- **과제 단위 논문 목록을 만들 때**: 여러 표기의 과제번호를 한 정규형으로 묶고 논문·DOI·사사 표기를 함께 확보합니다.
- **사업 성과를 사전 점검할 때**: 연도·과제·사업코드 조각별 집계를 만들고 실제 논문으로 되돌아갈 근거를 남깁니다.
- **NTIS 제출 성과를 외부 서지와 대조할 때**: 매칭된 행과 DOI 누락·사사 누락·미색인·조회 실패를 구분합니다.

강점은 **한국 과제번호의 표기 변형 처리와 귀속 근거 보존**입니다. 일반 키워드 검색보다 과제와 논문을 연결하는 이유가 명확합니다. 다만 사사에 적혔다는 사실은 그 과제가 논문 성과를 단독으로 만들었다는 인과적 증거가 아닙니다.

## 처음 실행하기

Python 3.10 이상을 권장합니다. 정규화는 오프라인, 논문 조회는 인터넷 연결이 필요합니다.

```bash
git clone https://github.com/parkjui92/kr-grant-trace.git
cd kr-grant-trace
python3 grant_trace.py selftest
python3 grant_trace.py normalize "NRF-2016R1A2B4014600" "RS‐2024‐00392705"
```

첫 번호가 `2016R1A2B4014600`, 두 번째가 `RS-2024-00392705`로 나오는지 확인하세요. 그다음 자신의 과제번호로 실행합니다.

```bash
python3 grant_trace.py --db project-cache.db trace 2016R1A2B4014600 --out papers.csv
python3 grant_trace.py summarize papers.csv --by award --credit split
# 같은 과제를 최신 자료로 다시 수집
python3 grant_trace.py --db project-cache.db trace 2016R1A2B4014600 --refresh --out papers-latest.csv
```

`--db`는 하위 명령 **앞**에 둡니다. CSV의 한 행은 과제–논문 연결 하나이므로, 여러 과제를 함께 조회하면 같은 논문이 여러 행에 나올 수 있습니다. `split`의 분모는 **이번 입력에 포함된 매칭 과제 수**이고, `first`는 과제번호 정렬상 첫 과제에 귀속합니다(논문의 주 사사 과제를 뜻하지 않음). `--refresh`는 해당 과제의 이전 매칭을 새 조회 결과로 교체합니다. 부분 수집 여부는 캐시에도 보존됩니다.

NTIS CSV에는 최소 `doi,과제번호` 열을 준비한 뒤 실행합니다.

```bash
python3 grant_trace.py --db project-cache.db coverage ntis.csv --out coverage-detail.csv
```

분모는 **입력 CSV의 모든 데이터 행**이며 DOI 누락도 포함합니다. 중복 행은 자동 제거하지 않으므로 분석 단위에 맞춰 먼저 정리하세요. 404만 미색인으로 분류하고, 인증·호출 한도·연결 오류는 `조회 실패`로 남깁니다. 빈 입력에는 비율을 만들지 않습니다.

## 왜 번호 정규화가 핵심인가

OpenAlex에는 한국연구재단 과제번호가 달린 논문이 33만 건 넘게 있지만(2026-10 실측) 같은 과제가 이렇게 제각각 적혀 있습니다.

| 같은 과제의 표기 | 비고 |
|---|---|
| `2014R1A6A1030419` · `NRF-2014R1A6A1030419` · `NRF‐2014R1A6A1030419` · `NRF- 2014R1A6A1030419` | 접두어·유니코드 하이픈(U+2010)·공백 |
| `2019R1A6A1A10073437` · `2019R1-A6A1-A10073437` | 내부 하이픈 |
| `RS-2024-00392705` · `RS‐2024‐00392705` · `RS-202400392705` · `RS-2024–00392705` | 통합번호(2022~), 이스케이프 문자열이 그대로 들어간 것도 있음 |
| `2018-0-01423` · `IITP-2022-2018-0-01423` | IITP 연차 접두 |
| `10052980` · `NO. 10052980` · `KEIT 10052980` | 숫자만인 번호는 다른 나라 번호와 충돌 → 한국 펀더 필터 필수 |

OpenAlex는 표기마다 별도 award 엔티티를 만들기 때문에 정규화 없이 세면 한 과제가 서너 조각으로 쪼개집니다. 실측(과제 `2014R1A6A1030419`): 정규형 13 + `NRF-` 21 + `NRF‐` 22건 등 129건이 세 표기에 나눠 있었습니다.

이 도구는 **정규화 → 변형 전개 → OR 질의 → 응답 쪽 표기도 다시 정규화해 귀속**합니다. 실측에서 추출한 표기 패턴은 [docs/design.md](docs/design.md)에 있습니다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `normalize 번호… [--file f] [--format md\|json]` | 정규형·유형·연도·사업코드 조각·질의 변형·무엇을 벗겼는지 |
| `trace 번호… [--file f --column 열] --out x.csv` | 논문 표(DOI·연도·유형·인용·FWCI·OA·국제공저·한국 기관·주제·매칭된 표기·`year_flag`) + 번호별 건수. SQLite 캐시 |
| `summarize x.csv --by award\|code\|year\|all --credit full\|split\|first` | 집계표(논문 수·인용 중앙값·FWCI 중앙값·국제공저율·OA율·연도 범위) |
| `coverage ntis.csv [--doi-column --column] --out detail.csv` | NTIS 성과 목록 대비 매칭률(하한)과 미매칭 사유 4종 |
| `selftest` | 정규화 회귀 40건 |

`--file`은 한 줄에 번호 하나인 txt, 또는 과제번호 열이 있는 CSV(열 이름은 자동 추정, `--column`으로 지정).

## 정직성 규칙 (출력에 그대로 찍힙니다)

1. **매칭률은 하한** — 사사를 쓰지 않은 논문, 비색인 학술지는 이 방법으로 영영 못 잡습니다. `coverage`는 DOI 누락·번호 미해석·미색인·사사 없음·다른 번호/펀더·조회 실패를 구분합니다. 조회 실패를 성과 부재로 해석하지 않습니다.
2. **0건 ≠ 성과 없음.** 해석 못 한 번호(`UNPARSED`)는 질의하지 않고 따로 셉니다.
3. **계수 규칙을 머리에 적습니다.** 한 논문에 과제 n개가 달렸을 때 `full`(각 1) / `split`(1/n) / `first`.
4. **사업 코드에 이름을 붙이지 않습니다.** NRF `R1A2B4` 같은 조각만 보여 주고 사업명은 사용자가 붙입니다.
5. **지우지 않고 표시합니다.** 과제 연도보다 2년 이상 앞선 논문(OpenAlex 오귀속 의심)은 `year_flag=1`.
6. **조회일을 같이 씁니다.** OpenAlex는 매주 바뀝니다.
7. **잘렸으면 잘렸다고 씁니다.** 키 없이 한 연도 100건을 넘는 과제는 받은 만큼만 내고 경고합니다.

## OpenAlex API 키

[OpenAlex 공식 페이징 안내](https://help.openalex.org/api/paging/) 기준으로 목록 페이지는 최대 100건을 지원하고, 커서의 두 번째 페이지부터 무료 키가 필요합니다(2026-10-21 확인). 키 없이도 돌아가도록 **과제 1건씩·연도별로 쪼개** 100건 이하 구간만 받습니다(요청 수가 늘고, 한 연도 100건 초과분은 잘림). 많이 돌리려면 키를 받아 환경변수로 주세요.

```bash
export OPENALEX_API_KEY=...        # 또는 ~/.kr-grant-trace/key 파일 (https://openalex.org/settings/api, 무료)
export OPENALEX_MAILTO=you@example.org
export KR_GRANT_TRACE_DB=~/.kr-grant-trace/cache.db   # 기본값
```

python.org 배포 파이썬의 인증서 문제에 대비해 HTTPS는 urllib → certifi → curl 순으로 시도합니다.

## 지원 번호 유형

NRF(`2016R1A2B4014600`, 구형 `2011-0023612`) · RS 통합번호(`RS-2024-00392705`, 문자 접두 `RS-2019-II190075`) · IITP(`2019-0-01842`) · KEIT 8자리 · KETEP 14자리 · NTIS 10자리 · MOTIE `P0022336` · KHIDI(`HI14C1324`) · NST(`CAP21034-000`, `CAP-16-01-KIST`) · IPET(`117043-3`, `316033-04-2-SB030`). 유형을 더하려면 `grant_trace.py`의 정규식과 `SELFTEST_CASES`에 한 줄씩 추가합니다.

## 하지 않는 것

- Scopus·Dimensions 백엔드(키·구독 필요) — 필요하면 2차
- NTIS API 직접 호출 — NTIS에서 내려받은 CSV를 받습니다
- 특허·기술료 — 논문만

## 검증 상태

- 정규화 회귀 40건 통과(실측 표본에서 뽑은 변형 포함)
- 과제 6건 실추적: 번호별 건수가 OpenAlex 직접 조회와 일치(7·129·129·154·330·23)
- `coverage`는 합성 입력으로만 시험 — 실제 NTIS 성과 CSV 1건 완주는 미검증. 2026-10-21에는 조회 실패·DOI 누락·빈 입력·새로고침 회귀 검사를 추가했습니다.

## R&D 정책평가 도구 묶음에서의 위치

필요한 단계만 골라 사용할 수 있습니다. 아래 순서는 업무 흐름이며 자동으로 연결된 파이프라인은 아닙니다. 도구 사이에 자료를 옮길 때 식별자·단위·연도·분류를 확인하세요.

| 필요 | 도구 |
|---|---|
| 공식 통계 수집 | [kr-sti-data](https://github.com/parkjui92/kr-sti-data) |
| 과제번호로 논문 추적 | [kr-grant-trace](https://github.com/parkjui92/kr-grant-trace) |
| 기술 분야의 논문·국내 특허 동향 | [sci-concentration](https://github.com/parkjui92/sci-concentration) |
| 논리모형·성과지표 설계 점검 | [rnd-program-logic](https://github.com/parkjui92/rnd-program-logic) |
| 인과효과 추정 전 설계 점검 | [policy-did](https://github.com/parkjui92/policy-did) |
| AI와 평가 문서 검토 | [rnd-eval-skills](https://github.com/parkjui92/rnd-eval-skills) |
| 예산표 금액 대조 | [won-diff](https://github.com/parkjui92/won-diff) |

## 라이선스

MIT. 데이터는 OpenAlex(CC0).
