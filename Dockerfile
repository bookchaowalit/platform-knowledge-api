# Build from the repository root; copy only declared catalog metadata.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# Dependencies come from uv.lock (--frozen), so every build installs the versions CI tested.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KNOWLEDGE_API_HOST=0.0.0.0 KNOWLEDGE_API_ENV=production KNOWLEDGE_API_CATALOG_PATH=/app/data/document-catalog.json \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY vendor/service-auth /app/vendor/service-auth
COPY pyproject.toml uv.lock server.py _sentry.py /app/
RUN uv sync --frozen --no-dev --no-editable --no-cache --python /usr/local/bin/python3
COPY data/document-catalog.json /app/data/document-catalog.json
RUN chmod -R a+rX /app/data

RUN useradd --system --uid 10001 app
USER app
EXPOSE 3430
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import os, urllib.request; port=os.environ.get('KNOWLEDGE_API_PORT', os.environ.get('PORT', '3430')); urllib.request.urlopen('http://127.0.0.1:'+port+'/ready', timeout=2)"
CMD ["python", "/app/server.py"]
