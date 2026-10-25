# openanalytics — privacy-first web analytics
# ---------------------------------------------------------------
# One container, one volume, one port.
#
#   docker build -t openanalytics .
#   docker run -d --name openanalytics -p 8000:8000 \
#       -v "$PWD/data:/data" openanalytics
#
# -> http://localhost:8000
# ---------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENANALYTICS_DB=/data/analytics.db \
    OPENANALYTICS_HOST=0.0.0.0 \
    OPENANALYTICS_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY sdk.py ./
COPY static/ ./static/
COPY templates/ ./templates/

# SQLite wants a writable dir; keep it out of the image so you can back it up.
RUN mkdir -p /data && chmod 0777 /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('OPENANALYTICS_PORT','8000'),timeout=2).status==200 else 1)"

CMD ["python", "server.py"]
