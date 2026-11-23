FROM python:3.12-slim-bookworm
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DENO_DIR=/tmp/deno XDG_CACHE_HOME=/tmp/.cache
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "yt-dlp[default]>=2025.10.14,<2027"
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nova && mkdir -p /downloads && chown nova:nova /downloads
COPY --chown=nova:nova bot.py /app/bot.py
WORKDIR /app
USER nova
CMD ["python", "bot.py"]
