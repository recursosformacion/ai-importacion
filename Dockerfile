FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AI_IMPORTACION_DB=/app/data/ai-importacion.sqlite3 \
    AI_IMPORTACION_DOWNLOAD_DIR=/app/downloads \
    AI_IMPORTACION_OCR_LANG=eng+spa

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY README.md .
RUN mkdir -p /app/data /app/downloads

EXPOSE 9500
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9500"]
