# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.11
ARG VERSION=latest

FROM python:${PYTHON_VERSION}-slim AS base

LABEL version=$VERSION

ENV JTL_APP_DIR=/app
ENV JTL_DEPLOYMENT='prod'

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Keeps Python from buffering stdout and stderr to avoid situations wheredocker ps 
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
COPY crontab /etc/crontab
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
COPY . /app

RUN python -m pip install /app

RUN mkdir /root/.ssh
RUN chmod 700 /root/.ssh

RUN cp  /app/node-config/id_rsa /root/.ssh/id_rsa
RUN cp  /app/node-config/id_rsa.pub /root/.ssh/id_rsa.pub
RUN cp /app/config/known_hosts /root/.ssh/known_hosts

RUN chmod 600 ~/.ssh/id_rsa                                                                       
RUN chmod 644 ~/.ssh/id_rsa.pub                                                                                                                              
RUN chmod 644 ~/.ssh/known_hosts

# Expose the port that the application listens on.
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the application.
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]
