FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install .

ENV FRIGATE_SIDECAR_CONFIG=/etc/frigate-sidecar/sidecar.yml

EXPOSE 5001

ENTRYPOINT ["python", "-m", "frigate_sidecar"]
CMD ["serve"]
