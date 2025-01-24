# Dockerfile

# Use the official Nginx base image
FROM nginx:latest 

RUN mkdir -p /opt/app 

# Copy website files into the container. This will get
# copied into the /opt/data/html, which is on a volume
COPY ./html /opt/app/html

# Copy website files into the container, into the 
# directory served by /local/ 
COPY ./html  /usr/share/nginx/html/

# Copy the .htpasswd file into the container
COPY .htpasswd /etc/nginx/.htpasswd

# Copy the custom Nginx configuration file into the container
COPY nginx.conf /etc/nginx/nginx.conf

# Copy the setup script into the container
COPY entrypoint.sh /opt/app/entrypoint.sh
RUN chmod +x /opt/app/entrypoint.sh

# Set up the entrypoint to run the setup script
ENTRYPOINT ["/bin/bash", "/opt/app/entrypoint.sh"]

# Set the default command to start Nginx
CMD ["nginx", "-g", "daemon off;"]
