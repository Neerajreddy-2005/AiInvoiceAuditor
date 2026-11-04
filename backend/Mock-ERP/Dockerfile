# mock-erp: FastAPI over the erp schema. No OCR or document dependencies.
FROM python:3.11.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

# Dependencies first: editing app/ does not reinstall them.
# requirements.lock pins every transitive package (reproducible build).
COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.lock

COPY app ./app

USER app
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# Database credentials arrive from the environment at runtime.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
