# 작업 기록 (WorkHistory) — Claude 작업 지침

사용자에게는 항상 한국어로 답한다.

## 세션 시작 시 (작업 전에 반드시)

1. [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)를 읽고 전체 구조를 파악한다.
2. [docs/history/](docs/history/)에서 파일 이름순으로 **가장 최근 기록 5개 정도**를 읽고
   지금까지의 흐름, 마지막 상태, 남은 일을 파악한다. 필요하면 더 거슬러 올라간다.
3. 작업 대상 영역의 상세 문서(아래 "문서 지도")를 필요한 만큼만 읽는다.
4. 기록 내용과 실제 코드가 다르면 코드를 기준으로 하고, 그 차이를 이번 작업 기록에 적는다.

## 작업을 마칠 때마다 (커밋 전에)

1. **히스토리 파일을 새로 하나 추가한다.** 기존 파일은 고치지 않는다 (오타 수정만 예외).
   - 경로: `docs/history/YYYY-MM-DD-NN-짧은-영문-슬러그.md`
     (`NN`은 그날의 순번 01, 02, …; 예: `2026-10-14-01-history-system.md`)
   - 형식: [docs/history/README.md](docs/history/README.md)의 템플릿을 따른다.
   - 한 작업(사용자 요청 하나 또는 의미 있는 변경 묶음) = 파일 하나.
     질문에 답만 하고 코드를 바꾸지 않았으면 만들지 않는다.
2. **구조가 바뀌었으면** [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)를 함께 고친다
   (모듈 추가/삭제/역할 변경, 스키마 버전, 실행·빌드·테스트 명령, 주요 결정).
3. 상세 설계가 바뀌었으면 해당 docs 문서(ARCHITECTURE, AI, PRIVACY 등)도 고친다.
4. **작업이 한 번 끝날 때마다 커밋하고 `origin main`에 푸시한다** (사용자가 상시 허락함, 따로 묻지 않는다).
   - 코드 변경, 히스토리 파일, 구조 문서 변경을 같은 커밋에 넣는다.
   - 커밋 전에 테스트를 돌리고, 실패하면 커밋하지 말고 사용자에게 알린다.
   - 히스토리 파일의 `커밋:` 항목은 "이 파일이 포함된 커밋"으로 적는다 (해시는 커밋 후에야 정해지므로).
   - 비밀값·개인 데이터(.db, 키 등)가 스테이징되지 않았는지 `git status`로 확인한다.
   - 푸시가 실패하면(충돌, 인증 등) 강제 푸시하지 말고 사용자에게 알린다.

## 문서 지도

| 문서 | 내용 |
|---|---|
| [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) | 한눈에 보는 구조, 모듈 지도, 명령, 주요 결정 |
| [docs/history/](docs/history/) | 작업 히스토리 (작업 1건 = 파일 1개) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 파이프라인, 데이터 모델(스키마), 세션 규칙, 보안 |
| [docs/PRODUCT.md](docs/PRODUCT.md) | 제품 목표와 사용자 질문 |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | 단계별 계획과 진행 상태 |
| [docs/AI.md](docs/AI.md) | AI 보고서/질문 (Claude Code 헤드리스), 동의, 참조 검증 |
| [docs/PRIVACY.md](docs/PRIVACY.md) | 기록 범위, 제외, 마스킹, 삭제 |
| [docs/RESULTS_TRACKING.md](docs/RESULTS_TRACKING.md) | 작업 폴더 스냅샷 (파일 변경 기록) |
| [docs/VALIDATION.md](docs/VALIDATION.md) | 실사용 검증 방법 |

## 지켜야 할 원칙

- 런타임 의존성은 최소로. 새 패키지는 상업적 판매가 가능한 라이선스(MIT/BSD/Apache 등)만,
  추가 전에 사용자에게 묻는다. GPL 계열·git.exe 의존 금지 (스냅샷은 dulwich).
- 로컬 전용: 서버는 127.0.0.1에만 바인딩, 실제 업무 데이터는 사용자 동의 없이 외부로 보내지 않는다.
- OS 독립 유지 (Windows 우선이지만 macOS/Linux에서도 동작).
- 사용자가 앱 기능을 쓸 때 터미널 단계가 필요 없게 한다 (설정은 앱 안에서).
- 아키텍처·라이선스·개인정보·범위 결정은 사용자에게 묻고, 나머지는 작은 단위로 검증하며 진행한다.
- 테스트: `.venv/Scripts/python.exe -m unittest discover -s tests -t .` (macOS/Linux는 `.venv/bin/python`)
