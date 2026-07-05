
# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.11
ARG VERSION=latest

FROM python:${PYTHON_VERSION}-slim AS base

LABEL version=$VERSION

ENV JTL_APP_DIR=/app
ENV JTL_DEPLOYMENT='devel'
ENV DOCKER_SSH_CMD="ssh -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Keeps Python from buffering stdout and stderr to avoid situations where docker ps 
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1

# Install git with proper preparation and cleanup
RUN apt-get update && \
    apt-get install -y \
    build-essential git cron curl tzdata procps vim nano \
    git supervisor net-tools tini && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=America/Los_Angeles

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Copy the crontab file into the appropriate location
COPY docker/crontab /etc/crontab
RUN crontab /etc/crontab

RUN mkdir -p /app

WORKDIR /app

# Create a non-privileged user that the app will run under.
# See https://docs.docker.com/go/dockerfile-user-best-practices/
ARG UID=10001
RUN adduser \
    --disabled-password \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser


# Download dependencies as a separate step to take advantage of Docker's caching.
# Leverage a cache mount to /root/.cache/pip to speed up subsequent builds.
# Leverage a bind mount to requirements.txt to avoid having to copy them into
# into this layer.

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    python -m pip install -r requirements.txt

# Copy the source code into the container.
COPY cspawn /app/cspawn
COPY requirements.txt /app/requirements.txt
COPY pyproject.toml /app/pyproject.toml
COPY migrations /app/migrations

COPY docker/supervisord.conf /app/supervisord.conf
COPY docker/gunicorn_config.py /app/config/gunicorn_config.py
COPY config/cloud-init /app/config/cloud-init
# Build-time self-check: fail the build if the cloud-init templates didn't
# actually land in the image (e.g. a future .dockerignore/COPY regression) —
# _create_droplet in cspawn/cli/node.py depends on this path existing.
RUN ls /app/config/cloud-init/*.yaml
COPY docker/ssh-config /etc/ssh/ssh_config.d/dojtl.conf
RUN chmod 644 /etc/ssh/ssh_config.d/dojtl.conf


COPY data /app/data

RUN python -m pip install /app/

# Prepare SSH directory (key is decoded at runtime by entrypoint.sh)
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

# Copy entrypoint script that decodes ID_RSA from the runtime env-file
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

# Expose the port that the application listens on.
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

# Run the application.
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]


