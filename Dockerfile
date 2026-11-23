FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "yt-dlp>=2025.10.14,<2027"
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nova && mkdir -p /downloads && chown nova:nova /downloads
COPY --chown=nova:nova bot.py /app/bot.py
WORKDIR /app
USER nova
CMD ["python", "bot.py"]
