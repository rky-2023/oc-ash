# openclaw-core — multi-stage build.
#
# Anchors: ADR-001 D2 (FastAPI/Python 3.13), ADR-002 D8 (no env-baked secrets;
# vault-agent renders into /run/secrets/openclaw/* at runtime).
#
# Phase 10 hardening will:
#   - Replace `python:3.13-slim-bookworm` with a digest-pinned base image.
#   - Add `--platform=$BUILDPLATFORM` for reproducible builds.
#   - Sign with cosign and generate an SBOM with syft.

# ============================================================================
# Stage 1: builder
# ============================================================================
FROM python:3.13-slim-bookworm AS builder

WORKDIR /build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip hatchling

# Copy project files
COPY pyproject.toml README.md ./
COPY app ./app

# Build wheel
RUN pip wheel --wheel-dir /build/dist .

# ============================================================================
# Stage 2: runtime
# ============================================================================
FROM python:3.13-slim-bookworm AS runtime

# Non-root user (uid > 10000 per ADR-001 D5 / ADR-002 D8 spirit)
RUN groupadd -r -g 10001 oc \
    && useradd -r -u 10001 -g oc -d /app -s /sbin/nologin oc

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/home/oc/.local/bin:${PATH}"

# Install runtime libpq (needed by asyncpg for some paths)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/dist/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

# Drop privileges
USER oc:oc

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/health').raise_for_status()" \
        || exit 1

ENTRYPOINT ["uvicorn"]
CMD ["app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
