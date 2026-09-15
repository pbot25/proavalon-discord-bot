FROM python:3.11-bookworm

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    VIDEO_OUTPUT_DIR=/data/browser \
    AUDIO_OUTPUT_DIR=/data/audio \
    FINAL_OUTPUT_DIR=/data/final

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY . ./
CMD ["python", "worker.py"]
