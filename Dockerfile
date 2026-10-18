# Build from the repository root; copy only declared catalog metadata.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 KNOWLEDGE_API_HOST=0.0.0.0 KNOWLEDGE_API_ENV=production KNOWLEDGE_API_CATALOG_PATH=/app/data/document-catalog.json
WORKDIR /app
COPY vendor/service-auth /app/vendor/service-auth
COPY pyproject.toml server.py _sentry.py /app/
RUN pip install /app/vendor/service-auth /app
COPY data/document-catalog.json /app/data/document-catalog.json
RUN chmod -R a+rX /app/data

RUN useradd --system --uid 10001 app
USER app
EXPOSE 3430
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import os, urllib.request; port=os.environ.get('KNOWLEDGE_API_PORT', os.environ.get('PORT', '3430')); urllib.request.urlopen('http://127.0.0.1:'+port+'/ready', timeout=2)"
CMD ["python", "/app/server.py"]
