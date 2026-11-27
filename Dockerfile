FROM gradle:8.13-jdk17 AS extractor-build
WORKDIR /workspace
COPY extractor/ ./extractor/
RUN cd extractor && gradle --no-daemon clean installDist

FROM denoland/deno:bin AS deno-bin

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DENO_DIR=/tmp/deno XDG_CACHE_HOME=/tmp/.cache
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates openjdk-17-jre-headless && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "yt-dlp[default]>=2025.10.14,<2027" "mega-v2.py==2.1.0"
COPY --from=deno-bin /deno /usr/local/bin/deno
COPY --from=extractor-build /workspace/extractor/build/install/nova-extractor /opt/nova-extractor
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nova && mkdir -p /downloads && chown -R nova:nova /downloads /opt/nova-extractor
COPY --chown=nova:nova bot.py /app/bot.py
WORKDIR /app
USER nova
CMD ["python", "bot.py"]
