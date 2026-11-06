FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && scrapling install

COPY . .

VOLUME ["/data"]

# Reap orphaned Chromium children instead of leaving that job to Python as PID 1.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "ns_signer.main"]
