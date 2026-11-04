# agent: LangGraph workflow + RAG + API on port 8000.
FROM python:3.11.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    CONFIG_DIR=/app/configs \
    INCOMING_DIR=/data/incoming \
    REPORTS_DIR=/data/reports

# 1) System packages (change rarely). Tesseract with the dataset's languages
#    (English, German, Spanish) and Poppler for rendering scanned PDF pages.
#    The apt cache is removed in the same layer that creates it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu tesseract-ocr-spa \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

# 2) Python dependencies (change occasionally). Copied alone so that
#    editing application code does not reinstall them. requirements.lock
#    pins every transitive package for a reproducible build.
COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.lock

# 3) Application code (changes often).
COPY app ./app

# Report files go to a named volume mounted here; create it owned by the
# unprivileged user so the volume inherits that ownership.
RUN mkdir -p /data/reports /data/incoming /app/configs && chown app:app /data/reports

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Credentials arrive at runtime from env_file; none are baked into the image.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
