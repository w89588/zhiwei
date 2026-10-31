FROM python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753
WORKDIR /app
COPY requirements-server.txt requirements-server-lock.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements-server.txt -c requirements-server-lock.txt
COPY backend ./backend
COPY helper/__init__.py helper/exporting.py ./helper/
COPY server_main.py VERSION ./
RUN useradd --create-home helper && mkdir /data && chown helper:helper /data
USER helper
ENV WECHAT_PLATFORM_VERSION=2 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WECHAT_STARTUP_TIMEOUT=60 \
    WECHAT_MIGRATION_LOCK_TIMEOUT=60
EXPOSE 8787
VOLUME ["/data"]
HEALTHCHECK --interval=20s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/readyz', timeout=8)"
CMD ["python", "server_main.py", "--host", "0.0.0.0", "--port", "8787"]
