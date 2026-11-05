FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONPATH=/app \
    PATH="/app/vendor/memseek/.venv/bin:$PATH"
COPY vendor/memseek/pyproject.toml vendor/memseek/uv.lock vendor/memseek/
RUN cd vendor/memseek && uv sync --frozen --no-dev --no-install-project
COPY . .
RUN cd vendor/memseek && uv sync --frozen --no-dev
WORKDIR /app/vendor/memseek
