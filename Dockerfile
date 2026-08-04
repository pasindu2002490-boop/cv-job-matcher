FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    OLLAMA_FALLBACK_ENABLED=0 \
    UPLOAD_ROOT=/tmp/cv-job-matcher/uploads \
    OUTPUT_ROOT=/tmp/cv-job-matcher/results

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m pip install --no-deps . \
    && mkdir -p /tmp/cv-job-matcher/uploads /tmp/cv-job-matcher/results

EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 600 --bind 0.0.0.0:${PORT:-8080} cv_job_matcher.web:app"]
