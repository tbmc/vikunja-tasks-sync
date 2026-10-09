# syntax=docker/dockerfile:1

# ---------- Stage 1: build the virtualenv with uv ----------
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.10 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install runtime dependencies only (no dev group), from the lockfile
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project

# ---------- Stage 2: minimal runtime image ----------
FROM python:3.14-slim AS runtime

RUN useradd --create-home --uid 1000 app

COPY --from=builder /opt/venv /opt/venv
COPY vikunja_sync /app/vikunja_sync

# OPENSSL_armcap=0: skip OpenSSL's ARM CPU feature probing, which crashes (SIGILL)
# in the cryptography wheels on Apple M4 hosts (SME) under Docker. Ignored on x86_64.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    OPENSSL_armcap=0

# Relative paths from the env (token.json, credentials.json, .state/, .out/) resolve here
WORKDIR /data
RUN chown app:app /data
VOLUME ["/data"]

USER app

CMD ["python", "-m", "vikunja_sync"]
