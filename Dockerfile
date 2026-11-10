# circverify: website, API, WhatsApp webhook, crawler and bots from one image.
# The index (several GB) lives in the /data volume, never in the image.
FROM python:3.12-slim

# Tesseract with the Hindi pack: the fast OCR engine, used automatically when present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[all]" \
    && useradd --create-home --uid 10001 circverify \
    && mkdir -p /data && chown circverify /data

USER circverify
ENV CIRCVERIFY_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/stats', timeout=8)" || exit 1

CMD ["circverify", "serve", "--host", "0.0.0.0", "--port", "8000"]
