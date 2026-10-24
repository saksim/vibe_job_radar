# Build from the reviewed release source. Override with an approved immutable digest.
ARG PYTHON_BASE=python:3.12-slim
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

WORKDIR /opt/vibe-radar
RUN groupadd --gid 10001 radar \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp radar \
    && mkdir /data \
    && chown 10001:10001 /data
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir ".[service]"

USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "vibe_job_radar.server"]
CMD ["--workspace", "/data", "--host", "0.0.0.0", "--port", "8080"]
