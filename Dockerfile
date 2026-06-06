# fc-inventory v3.0.0 — multi-stage Dockerfile.
#
# Stage 1: build the wheel from the current source.
# Stage 2: runtime image with the wheel installed and a non-root user.

# ── Stage 1: builder ─────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /build

# Build dependencies for any C extensions in the dep tree.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY static ./static

RUN pip install --no-cache-dir --upgrade pip build \
 && pip wheel --no-cache-dir --wheel-dir /wheels .

# ── Stage 2: runtime ─────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FC_INVENTORY_BIND=0.0.0.0 \
    FC_INVENTORY_PORT=8000

# Non-root user (uid 1000).
RUN useradd --create-home --uid 1000 --shell /bin/bash fcuser

# Install the wheel built in stage 1.
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels fc-inventory \
 && rm -rf /wheels

# Copy the static assets (templates are bundled in the wheel via
# pyproject.toml's [tool.setuptools.package-data]).
COPY --from=builder /build/static /app/static

USER fcuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import httpx, sys; \
    r = httpx.get('http://127.0.0.1:8000/api/health', verify=False, timeout=4); \
    sys.exit(0 if r.status_code == 200 else 1)"

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--log-level", "info"]
