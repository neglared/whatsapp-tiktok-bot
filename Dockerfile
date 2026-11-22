FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg est présent en secours (rarement utilisé : TikTok fournit du H.264).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/work

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "gunicorn -w 1 -k gthread --threads 8 --timeout 600 -b 0.0.0.0:${PORT} bot:app"]
