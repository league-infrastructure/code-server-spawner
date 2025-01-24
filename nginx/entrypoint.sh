#!/bin/bash
# setup.sh

# Perform any necessary setup tasks here
echo "Running entrypoint script"

# Copy files from the container into the mounted volume
# We have to do this here because the volume is mounted 
# after the container is created
if [ ! -f  /opt/data/html/index.html ]; then
    cp  /opt/app/html/index.html /opt/data/html/index.html
fi

chown -R www-data:www-data /opt/data/html
chmod -R 755 /opt/data/html




# Execute the CMD passed arguments (default: nginx -g "daemon off;")
exec "$@"
