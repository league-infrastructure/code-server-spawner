# Many ideas from https://blog.thesparktree.com/cron-in-docker

# Use a lightweight Python base image
FROM python:3.11-slim

# Install required packages including cron and git
RUN apt-get update && \
    apt-get install -y procps build-essential git cron curl tzdata && \
    which cron && \
    rm -rf /etc/cron.*/* && \
    apt-get clean 

ENV TZ=America/Los_Angeles

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN mkdir -p /app
    
# Copy and install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Uncomment these lines if you are installing a package from a wheel file
# COPY leaguesync.whl /opt/leaguesync-0.0.0-py3-none-any.whl
# RUN pip install /opt/leaguesync-0.0.0-py3-none-any.whl

# Copy environment variables and scripts
COPY cron-app.env /etc/cron-app.env

# Copy the crontab file into the appropriate location
COPY crontab /etc/crontab
RUN crontab /etc/crontab

# Copy the entrypoint script to the specified location
COPY entrypoint.sh app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Set the entrypoint to run the setup script
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]

# Default command to start cron in the foreground with logging level 15
CMD ["cron", "-f", "-l", "15"]
