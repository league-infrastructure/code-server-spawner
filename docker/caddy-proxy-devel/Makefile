
.PHONY:	up down build caddyfile

up:
	docker stack deploy -c docker-stack.yml caddy

down:
	docker stack rm caddy

data:
	docker run --rm -v caddy_caddy_data:/data alpine sh -c "apk add --no-cache tree && tree /data"

caddyfile:
	docker run --rm -v caddy_caddy_config:/config alpine cat /config/caddy/Caddyfile.autosave


logs: 
	docker service logs -f caddy_caddy