FROM ghcr.io/astral-sh/uv:0.11.0 AS uv

FROM python:3.12-slim-bookworm

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    CHROME_BINARY=/usr/bin/chromium \
    CHROME_PROFILE_DIR=/app/chrome-profile

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       chromium curl fonts-noto-cjk x11-utils xauth xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/chrome-profile \
    && chown -R app:app /app

USER app
