# Builds the AI-server image for the local demo topology. Build context is the
# repository root (see deploy/local/docker-compose.yml) so pyproject.toml, uv.lock,
# and ai_server/ are all reachable.
FROM ghcr.io/astral-sh/uv:0.11.33-python3.13-trixie-slim

WORKDIR /app

# Pinned local Tesseract OCR engine and English trained data (ARCHITECTURE.md OCR
# component); required for the /health OCR dependency probe to report "ok".
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY ai_server ./ai_server
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["uv", "run", "--locked", "--no-dev", "uvicorn", "ai_server.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
