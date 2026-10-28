# 프로젝트 구조 한눈에 보기

새 세션에서 가장 먼저 읽는 문서. 상세 설계는 [ARCHITECTURE.md](ARCHITECTURE.md)를 본다.
구조가 바뀌면 이 문서를 함께 고친다.

_마지막 갱신: 2026-10-28_

## 무엇인가

**작업 기록 (WorkHistory)**: 개발자용 로컬 전용 업무 기록 앱.

- 주 기록 = **결과물**: 등록한 작업 폴더의 파일 변경을 5분마다 숨김 Git 저장소(dulwich)에 스냅샷.
- 보조 기록 = **창 활동**: 앱/창 제목/유휴 상태를 작업 세션으로 묶음. Windows는 앱이 직접 기록
  (내장 수집기), macOS/Linux는 ActivityWatch에서 가져옴.
- 그 위에 타임라인, 일일 리뷰, 이어서 하기, 검색, 프로젝트, 개인정보 설정,
  AI 보고서·"AI에게 묻기"(Claude Code 헤드리스), Claude Code용 읽기 전용 MCP 서버.
- 스택: Python 3.11+ (표준 라이브러리 위주) + 바닐라 HTML/JS, SQLite. 빌드 단계 없음.
- 저장소: https://github.com/superokok/work-history (private), 브랜치 `main`.
- 라이선스: 자체 코드는 PROPRIETARY. 의존성은 상업적 판매 가능한 것만.

## 디렉터리

```
work-history/
├─ CLAUDE.md               Claude 작업 지침 (세션 시작/종료 규칙)
├─ README.md               설치·사용법·명령행 옵션
├─ pyproject.toml          패키지 정의 (dulwich 필수; extras: claude=mcp, desktop=pywebview)
├─ start-workhistory.cmd   Windows 실행기 (.venv 우선)
├─ start-workhistory.sh    macOS/Linux 실행기
├─ install.sh              macOS/Linux 최초 설치
├─ workhistory/            앱 패키지 (아래 모듈 지도)
│  └─ static/              UI: index.html, app.js, style.css (한국어)
├─ tests/                  unittest (helpers.py 공용 픽스처)
├─ evals/report_eval.py    데모 시나리오로 AI 보고서 품질 실측 (실제 데이터 미사용)
├─ tools/build_portable.py Windows 포터블 zip 빌드 → dist/ (git 제외)
├─ llm/                    (git 제외) 로컬 AI: bin/llama-server.exe + DLL, models/*.gguf, 라이선스 전문
├─ ppt/                    사용자의 발표 파일(.pptx, git 포함). 작업 중 만든 초안 덱은 docs/presentation/(git 제외)
└─ docs/
   ├─ PROJECT_STRUCTURE.md 이 문서
   └─ SELF_REVIEW / ARCHITECTURE / PRODUCT / IMPLEMENTATION_PLAN / AI / PRIVACY / RESULTS_TRACKING / VALIDATION
```

## 모듈 지도 (workhistory/)

데이터 흐름 순서. 상세는 [ARCHITECTURE §3](ARCHITECTURE.md).

| 계층 | 모듈 | 역할 |
|---|---|---|
| 진입 | `__main__.py` | 명령행 옵션, 단일 인스턴스, 서버·동기화 시작 |
| 설정 | `config.py` | 상수 (세션 묶기 기준 등), 데이터 폴더 경로 |
| 수집 | `collector.py` | Windows 내장 창·유휴 수집기 (ctypes, 2초마다) + `LocalSource`(awclient와 같은 호출로 제공) |
| | `awclient.py` | ActivityWatch REST 클라이언트 (macOS/Linux) |
| | `sync.py` | 증분 가져오기, 중복 제거, 수집 시 개인정보 필터 |
| | `snapshots.py` | 작업 폴더 스냅샷 (dulwich), 변경 기록, 하위 폴더별 프로젝트 (앱은 추천만, 사람이 확인) |
| | `extract.py` | 코드/텍스트, HWP, DOCX/XLSX/PPTX 텍스트 추출 |
| 저장 | `db.py` | SQLite 스키마 + 마이그레이션 (`PRAGMA user_version`, 현재 v9) |
| | `store.py` | 공용 로더, 하루 경계, 세션 재계산 표시 |
| 처리 (순수 함수) | `normalize.py` | 창 − AFK − 차단 구간 − 제외, 하루 단위 자르기 |
| | `classify.py` | 수동 지정 > 키워드 규칙 > 없음 |
| | `sessions.py` | 결정적 세션 묶기 + 자동 제목 |
| | `review.py` | 일일 리뷰 템플릿, 후속 작업 추출 |
| | `redact.py` | 비밀값·개인 식별자 마스킹 |
| 세션 저장 | `sessionstore.py` | 재묶기, 편집, 병합, 분할 |
| AI | `ai_client.py` | 공급자 중립 `Model` 프로토콜 |
| | `ai_claude.py` | Claude Code 헤드리스(`claude -p`) 구현, 도구 잠금 |
| | `ai_openai.py` | OpenAI 호환 API 공통 (보고서 JSON 단계적 대체, 도구 루프, 생각 출력 제거) |
| | `ai_local.py` | 내 PC의 AI: llama.cpp `llama-server`를 켜고 끄며 연결 (외부 전송 없음, 자동 등록) |
| | `ai_remote.py` | 주소로 등록한 AI 서버 (행망·사내 LLM, 어떤 모델이든 OpenAI 호환이면 연결) |
| | `ai_context.py` | AI에 보낼 정확한 자료 + 검증 가능한 참조(C/S/N/P) |
| | `ai_report.py` | 보고서 스키마, 프롬프트, 참조 검증, 렌더링 |
| | `ai_agent.py` | "AI에게 묻기": 읽기 전용 기록 도구 + 인용 확인 |
| | `ai_service.py` | AI 설정, 동의, 실행 로그, 저장된 보고서 |
| | `usage.py` | 효과 측정: 보고서 작성 시간, AI 초안 그대로 쓴 줄, 검색 후 여는 시간 (숫자만, 이 PC에만) |
| | `mcp_server.py` | 내부용 읽기 전용 MCP 서버: Claude Code 모델로 "AI에게 묻기"할 때만 앱이 띄움 (외부 연결 거부) |
| 외부 연동 | `autostart.py` | 로그온 시 시작, 바로가기 생성 |
| 서비스 | `service.py` | 타임라인, 검색, 메모, 프로젝트, 리뷰, 이어서 하기 쿼리 |
| | `server.py` | JSON API + 정적 UI, 127.0.0.1 전용 |
| 기타 | `stats.py` | 실사용 보정률 통계 (`python -m workhistory.stats`) |
| | `demo.py`, `demo_files.py` | `--demo`용 가상 2주 시나리오 (별도 demo.db) |

## 자주 쓰는 명령

```bash
# 개발 환경
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -e .[claude]

# 실행 (기본 http://127.0.0.1:5730/)
.venv/Scripts/python.exe -m workhistory            # 또는 start-workhistory.cmd
.venv/Scripts/python.exe -m workhistory --demo     # 데모 데이터

# 테스트 (2026-10-28 기준 238개 통과)
.venv/Scripts/python.exe -m unittest discover -s tests -t .

# AI 보고서 품질 평가 (Claude Code 로그인 필요)
.venv/Scripts/python.exe -m evals.report_eval

# Windows 포터블 zip
python tools/build_portable.py                 # llm/bin이 있으면 로컬 AI 실행기도 포함 (모델은 제외)
python tools/build_portable.py --with-models   # 모델 파일까지 넣은 큰 zip (-local-ai)

# 로컬 모델로 품질 평가 (기기 고정: WORKHISTORY_LLM_DEVICE=none 은 CPU만)
.venv/Scripts/python.exe -m evals.report_eval --model local:Qwen3.5-4B-Q4_K_M
```

데이터 폴더: Windows `%LOCALAPPDATA%\WorkHistory`, macOS `~/Library/Application Support/WorkHistory`,
Linux `~/.local/share/WorkHistory`. Windows는 ActivityWatch를 쓰지 않는다
(`--activity-source activitywatch`로만 사용; 개발 PC에 포터블 설치가 남아 있음).

주의 (Windows, Claude 데스크톱 앱): Claude 안에서 띄운 프로세스는 `%LOCALAPPDATA%`가
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Local`로 가상화되어 **실제 앱과 다른 DB**를 본다.
실제 앱 데이터는 실행 중인 앱의 API(`http://127.0.0.1:5730/api/...`)로 확인·변경한다.
실제 앱은 Claude 밖에서 띄운다 (`explorer.exe "<시작프로그램>\작업 기록.lnk"`).

## 주요 결정 (바꾸기 전에 사용자에게 확인)

- 스택: Python 표준 라이브러리 + 바닐라 JS, 프레임워크 없음.
- 주 기록은 창 시간이 아니라 **파일 변경(결과물)**.
- 파일 변경의 프로젝트는 앱이 추측해서 넣지 않는다: 하위 폴더별로 추천하고 사람이 확인한 것만 쓴다 (2026-10-28).
- 스냅샷은 dulwich(Apache-2.0)로, git.exe·GPL 의존 금지.
- 창 기록은 Windows에서 앱의 내장 수집기로 (ActivityWatch 별도 설치 없이, 2026-10-28). macOS/Linux는 ActivityWatch.
- AI는 셋 중 선택 (모두 `ai_client.Model` 구현): 내 PC의 로컬 모델(llama.cpp + Qwen3.5, 외부 전송 없음, 기본·추천),
  주소로 등록한 AI 서버(OpenAI 호환, 행망·사내 LLM), Claude Code(헤드리스). 로컬 모델 파일은 `llm/`(git 제외), 따로 배포.
  모델을 고르기 전(=동의 전)에는 AI 꺼짐.
- Claude Code는 AI 설정의 모델 선택지로만 씀 (로컬 모델과 비교용). Claude Code에 앱을 등록하는 "Claude Code 연결"은
  2026-10-27에 없앰.
- OS 독립, 로컬 전용, 배포는 Windows 포터블 zip + macOS/Linux `install.sh`.
