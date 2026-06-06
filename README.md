# fc-inventory

> A FastAPI web service for collecting inventory from Huawei FusionCompute VRM and exporting a RVTools-style multi-sheet Excel workbook.

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org) [![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/)

`fc-inventory` connects to a **Huawei FusionCompute VRM** REST API, fetches sites / clusters / hosts / VMs / datastores / networks, and writes a 10-sheet `.xlsx` workbook modelled on RVTools for VMware. The v3.0.0 release is a clean FastAPI rewrite: a single Python web service with `async` httpx I/O, structured logging, OpenAPI docs, and zero reliance on the legacy Flask + threading design.

## Features

- **FastAPI service** — single Python process; `uvicorn` for ASGI; `BackgroundTasks` for the collection pipeline.
- **Async httpx** — real concurrency on the resource fetches via `asyncio.gather` (clusters + hosts + datastores + dvswitches all run in parallel per site).
- **Auto-detect login** — 6-API-version × 3-auth-method × 3-port matrix (matches v1.0.0).
- **Hybrid field mapping** — 8 column → candidate-paths tables, the first non-empty value wins; raw extras captured for forward-compat.
- **Web UI preserved** — same localhost web UI as v1.0.0, modernised to `async`/`await` + `fetch()`.
- **OpenAPI docs** — `/docs` (Swagger UI) and `/redoc` are auto-generated.
- **Pydantic validation** — `SecretStr` for passwords (never logged or echoed in repr).
- **pytest + httpx MockTransport** — full test coverage (90 tests covering FC client, collector, jobs, excel, FastAPI routes, settings).
- **Multi-arch Docker** — `linux/amd64` + `linux/arm64` images published to GHCR.
- **PyInstaller Windows .exe** — single-folder bundle for end users.

## Output sheets

`vSummary`, `vInfo`, `vCPU`, `vMemory`, `vDisk`, `vNetwork`, `vHost`, `vCluster`, `vDatastore`, `vSwitch` — same RVTools convention as v1.0.0, with the same dark-blue (`#2C3E50`) bold-white header, autofilter, and frozen top row.

## Quick start

### 1. Install (editable + dev)

```bash
pip install -e ".[dev]"
```

Or with `uv` (faster):

```bash
uv sync --all-extras --dev
```

### 2. Run the dev server

```bash
make run          # uvicorn app.main:app --reload --host 127.0.0.1 --port 5000
```

Open <http://127.0.0.1:5000> in a browser, or curl the API:

```bash
curl -X POST http://127.0.0.1:5000/api/collect \
     -H "Content-Type: application/json" \
     -d '{"host":"10.0.0.10","port":7443,"username":"readonly","password":"..."}'
# → 202 {"status":"started","job_id":"..."}

curl http://127.0.0.1:5000/api/progress
# poll until status="done"

curl -OJ http://127.0.0.1:5000/api/download
# → FC_Inventory_<timestamp>.xlsx
```

### 3. Swagger UI

<http://127.0.0.1:5000/docs> — all 8 routes + 2 doc pages with schemas, parameters, and example payloads.

### 4. Docker

```bash
make docker                              # build the image
docker run --rm -p 8000:8000 fc-inventory:dev
curl http://127.0.0.1:8000/api/health
# → 200 {"status":"ok"}
```

The multi-arch image is also pushed to GHCR on every tag — see `ghcr.io/kimzhong/fc-inventory:3.0.0`.

## Configuration

All runtime knobs come from environment variables (or a `.env` file). See [`.env.example`](.env.example) for the full list.

| Env var | Default | Description |
|---|---|---|
| `FC_INVENTORY_BIND` | `127.0.0.1` | Bind address; use `0.0.0.0` behind a reverse proxy. |
| `FC_INVENTORY_PORT` | `5000` | TCP port. |
| `FC_INVENTORY_LOG_FILE` | `fc_inventory.log` | Rotating log file path. |
| `FC_INVENTORY_LOG_LEVEL` | `INFO` | `debug` \| `info` \| `warn` \| `error`. |
| `FC_INVENTORY_LOG_MAX_BYTES` | `5242880` | 5 MB. |
| `FC_INVENTORY_LOG_BACKUP_COUNT` | `3` | Number of rotated backups. |
| `FC_INVENTORY_CORS_ORIGINS` | `[]` | Comma-separated origins (CORS off by default). |
| `FC_INVENTORY_OUTPUT_DIR` | `.` | Where the produced `.xlsx` is written. |
| `FC_INVENTORY_REQUEST_TIMEOUT_SECONDS` | `60` | Per-request HTTP timeout. |

Per-request JSON (mirrors v1.0.0):
```json
{ "host": "10.0.0.10", "port": 7443, "username": "readonly", "password": "..." }
```

The password is wrapped in `pydantic.SecretStr`; it never reaches logs, the OpenAPI echo, or the response model.

## HTTP API

| Method | Path | Purpose | Status codes |
|---|---|---|---|
| GET    | `/` | Main page (connect form, progress, result). | 200 |
| GET    | `/changelog` | Changelog page. | 200 |
| POST   | `/api/collect` | Start a collection. | 202 / 400 / 409 |
| GET    | `/api/progress` | Current job progress. | 200 |
| POST   | `/api/cancel` | Cancel the running job. | 200 / 404 |
| GET    | `/api/download` | Download the produced `.xlsx`. | 200 / 404 |
| GET    | `/api/version` | Service version. | 200 |
| GET    | `/api/changelog` | `CHANGELOG.md` as `text/plain`. | 200 / 404 / 500 |
| GET    | `/api/health` | K8s-friendly health probe. | 200 / 503 |
| GET    | `/docs` | Swagger UI. | 200 |
| GET    | `/redoc` | ReDoc. | 200 |

Exit codes (when running as a CLI, future-work):
- `0` success · `1` runtime/collection error · `2` config error · `130` cancelled (Ctrl+C).

## Project layout

```
fc-inventory/
├── app/                        # FastAPI app package
│   ├── __init__.py             # __version__ = "3.0.0"
│   ├── __main__.py             # `python -m app` entrypoint
│   ├── main.py                 # FastAPI app, lifespan, CORS, exception handlers
│   ├── api/                    # route handlers
│   │   ├── deps.py
│   │   ├── routes_pages.py     # GET /, GET /changelog (Jinja2)
│   │   └── routes_api.py       # /api/* JSON routes
│   ├── core/                   # domain logic
│   │   ├── config.py           # pydantic-settings Settings
│   │   ├── logging.py          # structlog + RotatingFileHandler
│   │   ├── fc_client.py        # async httpx FCClient (6×3×3 login matrix)
│   │   ├── field_map.py        # 8 *_FIELDS tables + path helpers
│   │   ├── collector.py        # async InventoryCollector
│   │   ├── jobs.py             # Job + JobManager
│   │   └── excel_builder.py    # openpyxl writer
│   ├── models/                 # Pydantic request/response schemas
│   │   ├── requests.py
│   │   └── responses.py
│   └── templates/              # Jinja2 (bundled in wheel)
├── static/                     # CSS / JS (served at /static)
├── tests/                      # pytest (90 tests)
│   ├── conftest.py
│   ├── fixtures/               # canned FC JSON responses
│   ├── test_fc_client.py
│   ├── test_field_map.py
│   ├── test_collector.py
│   ├── test_jobs.py
│   ├── test_excel_builder.py
│   ├── test_api.py
│   └── test_settings.py
├── docs/                       # screenshots
├── pyproject.toml              # PEP 621 packaging
├── requirements.txt            # runtime pins
├── requirements-dev.txt        # dev pins
├── Dockerfile                  # multi-stage python:3.12-slim
├── .dockerignore
├── Makefile                    # install/run/lint/type/test/cov/docker
├── .env.example
├── .github/workflows/
│   ├── ci.yml                  # lint + type-check + test on 3.10/3.11/3.12
│   └── build-release.yml       # PyInstaller .exe + Docker multi-arch + release
├── README.md
├── CHANGELOG.md
├── AUTHORS.md
└── LICENSE
```

## Development

```bash
make install   # pip install -e ".[dev]" / uv sync
make run       # uvicorn with --reload
make lint      # ruff check .
make type      # mypy app
make test      # pytest
make cov       # pytest --cov=app --cov-report=term-missing
make docker    # build the image
```

The `tests/fixtures/` directory holds canned FC JSON responses so the
test suite runs offline; the test client wires an `httpx.MockTransport`
into the FC client so the 6×3×3 login matrix and the per-resource
pagination are exercised without a real FusionCompute.

## Security model

- **TLS verify disabled by default** because FusionCompute ships with self-signed certificates. Set `fc.insecure_tls: false` to opt out (you'd usually also need a custom CA bundle).
- **Password is never logged.** The YAML loader expands `${ENV}` into the in-memory config; the FC client wraps the password in `pydantic.SecretStr`; structlog + the route handlers never echo it.
- **No open ports by default.** The binary binds to `127.0.0.1`. Set `FC_INVENTORY_BIND=0.0.0.0` (and run behind a reverse proxy) to expose on the LAN.
- **No telemetry.** The only outbound connection is to the configured FC VRM.

## Comparison with v1.0.0 (Python + Flask)

| Concern | v1.0.0 | v3.0.0 |
|---|---|---|
| Runtime | Python 3.9+, Flask, waitress, requests, openpyxl | Python 3.10+, FastAPI, uvicorn, httpx, openpyxl |
| User surface | Flask web UI on `127.0.0.1:5000` | Same web UI + Swagger UI at `/docs` + ReDoc at `/redoc` |
| HTTP client | sync `requests` + `threading.Thread` | async `httpx` + `asyncio.gather` + `BackgroundTasks` |
| Tests | **None** | 90 tests (`pytest` + `httpx.MockTransport`) |
| Type hints | None | Full Pydantic models + type annotations |
| OpenAPI docs | None | `/docs`, `/redoc`, `/openapi.json` |
| Health check | None | `GET /api/health` |
| Input validation | Hand-rolled | Pydantic with `SecretStr` |
| Logging | `logging` stdlib | `structlog` + rotating file + KeyValueRenderer |
| Config | 2 env vars | `pydantic-settings` (10 env vars + `.env`) |
| Packaging | PyInstaller one-dir `.exe` | PyInstaller `.exe` + multi-arch Docker image |
| Hybrid field map | 8 `OrderedDict` tables | 8 `OrderedDict` tables (byte-for-byte) |
| Excel styling | openpyxl `#2C3E50` bold white, autofilter, freeze A2, autosize | Same |
| Login matrix | 6 versions × 3 auths × 3 ports | Same |
| Sheet count | 10 (RVTools) | 10 (RVTools) |

## Authors

See [AUTHORS.md](AUTHORS.md). v3.0.0 is a FastAPI port of the v1.0.0 Python tool by Sukrit Phiboon, with AI pair-programming assistance from Claude.

## License

MIT — see [LICENSE](LICENSE).
