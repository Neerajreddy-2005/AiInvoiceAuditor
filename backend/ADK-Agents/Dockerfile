# adk-agents: Google ADK agents (translation, reporting) served over A2A. Port 8002.
# Stateless: no database driver, no OCR. Calls Gemini through google-genai.
FROM python:3.11.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GOOGLE_GENAI_USE_VERTEXAI=FALSE \
    CONFIG_DIR=/app/configs

# Security fixes newer than the base image.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

# Dependencies first: editing app/ does not reinstall them.
# requirements.lock pins every transitive package (reproducible build).
COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.lock

COPY app ./app
RUN mkdir -p /app/configs

USER app
EXPOSE 8002

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=2).status == 200 else 1)"

# The Gemini key arrives from the environment at runtime; none is baked into the image.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
