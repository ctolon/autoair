# syntax=docker/dockerfile:1
# AutoAir on the official Apache Airflow image: the Airflow plugin (api-server), and the
# `autoair serve` / `autoair ai-serve` commands, from one image. Published to GHCR by
# .github/workflows/release.yml as ghcr.io/ctolon/autoair.
#
#   make build                                   # frontend bundles + dist/autoair-*.whl
#   docker build -f deploy/docker/Dockerfile -t my-airflow-autoair:3.3.2 .
#
# Runs as the image's unprivileged "airflow" user (uid 50000, gid 0), like the base image.
ARG AIRFLOW_VERSION=3.3.2
FROM apache/airflow:${AIRFLOW_VERSION}

ARG AIRFLOW_VERSION
ARG AUTOAIR_VERSION=0.0.0+local
# For AIRFLOW_VERSION=3.1.x drop `mcp`: FastMCP 4 needs Starlette 1.x, which Airflow 3.1's
# FastAPI pin excludes (website/docs/installation/compatibility.md).
ARG AUTOAIR_EXTRAS=git,mcp,ai,ai-anthropic,ai-openai,format,postgres,collab,collab-redis,otel,yaml,standalone
LABEL org.opencontainers.image.title="autoair" \
      org.opencontainers.image.description="AutoAir no-code DAG builder on Apache Airflow ${AIRFLOW_VERSION}" \
      org.opencontainers.image.version="${AUTOAIR_VERSION}" \
      org.opencontainers.image.source="https://github.com/ctolon/autoair" \
      org.opencontainers.image.url="https://github.com/ctolon/autoair" \
      org.opencontainers.image.documentation="https://ctolon.github.io/autoair/" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="AutoAir" \
      org.opencontainers.image.base.name="docker.io/apache/airflow:${AIRFLOW_VERSION}"

# Providers your DAGs use go here as well (they also feed AutoAir's operator palette).
# apache-airflow is pinned so pip never changes the base image's Airflow.
USER airflow
COPY --chown=airflow:0 dist/autoair-*.whl /tmp/autoair/
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" \
        "$(ls /tmp/autoair/autoair-*.whl)[${AUTOAIR_EXTRAS}]" \
        apache-airflow-providers-standard apache-airflow-providers-git \
        apache-airflow-providers-common-messaging \
    && rm -rf /tmp/autoair

# Precompute the operator catalog for the providers baked into this image, so every
# api-server replica starts warm and serves an identical palette.
RUN autoair catalog build --output /opt/airflow/autoair-catalog.json
ENV AUTOAIR_CATALOG_FILE=/opt/airflow/autoair-catalog.json \
    AUTOAIR_CACHE_DIR=/tmp/autoair-cache

COPY --chown=airflow:0 --chmod=0555 deploy/docker/healthcheck.py /opt/autoair/healthcheck.py
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
    CMD ["python", "/opt/autoair/healthcheck.py"]

EXPOSE 8080 8765 8790
USER airflow
