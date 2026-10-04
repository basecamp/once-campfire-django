FROM python:3.14.7-slim-trixie
ARG REVISION=local
LABEL org.opencontainers.image.revision=$REVISION
RUN apt-get update && apt-get install -y --no-install-recommends libvips42 ffmpeg poppler-utils ca-certificates gcc libffi-dev pkg-config libvips-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /rails
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python bin/build-assets && useradd --uid 1000 --create-home campfire && mkdir -p storage && chown -R campfire:campfire storage && chmod +x bin/server hooks/*
ENV HTTP_PORT=80 CAMPFIRE_STORAGE_PATH=/rails/storage PYTHONUNBUFFERED=1
USER campfire
EXPOSE 80
ENTRYPOINT ["/rails/bin/server"]
