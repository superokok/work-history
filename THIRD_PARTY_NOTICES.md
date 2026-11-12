# Third-party notices

작업 기록(WorkHistory)은 아래 오픈소스 구성요소를 포함합니다. 모두 상업적 사용과 재배포를 허용하는
라이선스이며, 각 라이선스 전문은 포터블 패키지의 `runtime/Lib/site-packages/<패키지>.dist-info/`
(Python 자체는 `runtime/LICENSE.txt`)에 들어 있습니다.

This product includes the open-source components below, all under licenses that permit commercial
use and redistribution. Full license texts ship in the portable package under
`runtime/Lib/site-packages/<package>.dist-info/` (Python itself: `runtime/LICENSE.txt`).

| Component | Version | License | Source |
|---|---|---|---|
| Python (CPython runtime) | 3.12.5 | PSF-2.0 (bundled OpenSSL, SQLite, libffi, zlib, bzip2, xz: see runtime/LICENSE.txt) | https://www.python.org |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio |
| attrs | 26.1.0 | MIT |  |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click/ |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| dulwich | 1.2.15 | Apache-2.0 OR GPL-2.0-or-later — used under Apache-2.0 | https://www.dulwich.io/ |
| h11 | 0.16.0 | MIT License | https://github.com/python-hyper/h11 |
| httpcore2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| httpx2 | 2.13.1 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| mcp | 2.2.0 | MIT License | https://modelcontextprotocol.io |
| mcp-types | 2.2.0 | MIT License | https://modelcontextprotocol.io |
| opentelemetry-api | 1.45.0 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pydantic | 2.13.5 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.46.5 | MIT | https://github.com/pydantic |
| PyJWT | 2.15.1 | MIT | https://github.com/jpadilla/pyjwt |
| python-multipart | 0.0.32 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| pywin32 | 312 | Python Software Foundation License | https://github.com/mhammond/pywin32 |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| rpds-py | 2026.6.3 | MIT | https://github.com/crate-py/rpds |
| sse-starlette | 3.5.0 | BSD-3-Clause | https://github.com/sysid/sse-starlette |
| starlette | 1.7.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| truststore | 0.10.4 | MIT | https://github.com/sethmlarson/truststore |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| urllib3 | 2.8.0 | MIT |  |
| uvicorn | 0.54.0 | BSD-3-Clause | https://uvicorn.dev/ |

## Not included (installed separately by the user, optional)

| Component | License | Use |
|---|---|---|
| Claude Code | Anthropic Commercial Terms / Consumer Terms | AI features, run as a separate program |

## Local AI (portable package, `llm/` folder)

| Component | License | Use | License text |
|---|---|---|---|
| llama.cpp / ggml (`llm/bin`) | MIT | runs the local model on this PC | `llm/LICENSE-llama.cpp.txt` |
| LLVM OpenMP runtime (`llm/bin/libomp.dll`) | Apache-2.0 WITH LLVM-exception | used by llama.cpp | `llm/bin/LICENSE-LLVM-OpenMP` |
| Microsoft Visual C++ runtime (`llm/bin/msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll`) | Microsoft Visual Studio redistributable files (app-local deployment) | used by llama.cpp; copied from the build PC's Visual C++ Redistributable | https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files |
| Qwen3.5 models (`llm/models`, if included) | Apache-2.0 | local AI model | `llm/LICENSE-Qwen3.5.txt` |
