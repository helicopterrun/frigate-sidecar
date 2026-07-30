FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# wildlife.py and the scrub-cache generator both shell out to ffmpeg/ffprobe;
# the image was missing it (confirmed live 2026-07-30, M7 in
# docs/scrub-cache-and-proxy-spec.md) -- this was already broken for
# wildlife.py, not just a new requirement for scrub-cache.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install .

ENV FRIGATE_SIDECAR_CONFIG=/etc/frigate-sidecar/sidecar.yml

EXPOSE 5001

ENTRYPOINT ["python", "-m", "frigate_sidecar"]
CMD ["serve"]
