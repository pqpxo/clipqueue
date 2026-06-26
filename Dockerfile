# version 1.2.0
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY docker/entrypoint.sh /usr/local/bin/clipqueue-entrypoint.sh
RUN chmod 0755 /usr/local/bin/clipqueue-entrypoint.sh

EXPOSE 8097

ENTRYPOINT ["/usr/local/bin/clipqueue-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097", "--proxy-headers"]
