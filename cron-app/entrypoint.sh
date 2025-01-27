#!/bin/bash
# setup.sh

# Perform any necessary setup tasks here
echo "Running entrypoint script..."


# Execute the CMD passed arguments (default: nginx -g "daemon off;")
exec "$@"
