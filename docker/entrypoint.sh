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

exec "$@"
