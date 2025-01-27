# syntax=docker/dockerfile:1

# Comments are provided throughout this file to help you get started.
# If you need more help, visit the Dockerfile reference guide at
# https://docs.docker.com/engine/reference/builder/

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS base

# Install git with proper preparation and cleanup
RUN apt-get update && \
    apt-get install -y \
    build-essential git cron curl tzdata \
    git supervisor net-tools tini && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=America/Los_Angeles

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Keeps Python from buffering stdout and stderr to avoid situations where
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /app

WORKDIR /app

# Create a non-privileged user that the app will run under.
# See https://docs.docker.com/go/dockerfile-user-best-practices/
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
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

# Switch to the non-privileged user to run the application.
# USER appuser

# Copy the source code into the container.
COPY . /app

# Expose the port that the application listens on.
EXPOSE 8000

# Copy the entrypoint script to the specified location
# Don't really need to do this b/c of the copy . ., but
# it's summetric with the other Dockerfiles

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the application.
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
