#!/bin/bash
# setup.sh

set -e  # Exit immediately if a command exits with a non-zero status
set -o pipefail  # Return the exit code of the last command in the pipeline that failed

# Perform any necessary setup tasks here
echo "Running entrypoint script..."

mkdir -p /opt/data/html/

# Execute the CMD passed arguments (default: nginx -g "daemon off;")
exec "$@"
