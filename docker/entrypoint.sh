#!/bin/sh
set -e

# Decode SSH private key from base64 env var at container start time.
# The key is never baked into the image — it arrives via the runtime env-file.
if [ -n "$ID_RSA" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    printf '%s' "$ID_RSA" | base64 -d > /root/.ssh/id_rsa
    chmod 600 /root/.ssh/id_rsa
    unset ID_RSA
else
    echo "WARNING: ID_RSA is not set — outbound SSH to worker nodes will fail." >&2
fi

# Export the runtime environment to a file so cron jobs can source it. Cron runs
# with a stripped environment, so `cspawnctl` invoked from crontab otherwise
# can't see DATABASE_URI / DO_TOKEN / AUTOSCALE_* etc. (ID_RSA was already unset
# above, so it is not written here). Root-only.
export -p | grep -vE "^export (PWD|OLDPWD|SHLVL|_)=" > /app/cron.env
chmod 600 /app/cron.env

exec "$@"
