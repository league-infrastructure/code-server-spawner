# Simple helpers for building and deploying the stack

STACK ?= codeserver
FILE  ?= docker-stack.yaml

.PHONY: build up down networks status logs redeploy debug

# Build images referenced by the stack using Compose (Stack ignores build)
build:
	@docker compose -f $(FILE) build codeserver

build-clean:
	@docker compose -f $(FILE) build codeserver --no-cache


# Create required overlay networks (idempotent)
networks:
	@docker network inspect caddy >/dev/null 2>&1 || docker network create --driver overlay --attachable caddy
	@docker network inspect jtlctl >/dev/null 2>&1 || docker network create --driver overlay --attachable jtlctl

# Deploy the stack to Swarm
up: networks
	@docker stack deploy --detach=true -c $(FILE) $(STACK)

# Remove the stack from Swarm
down:
	@docker stack rm $(STACK)

# Convenience
status:
	@docker stack services $(STACK) || true
	@docker stack ps $(STACK) || true

logs:
	@docker service logs -f $(STACK)_codeserver


redeploy: build up

# Debug inside the first running container built from image 'codeserver:latest'
debug:
	@cid=$$(docker ps -q --filter ancestor=codeserver:latest --filter status=running | head -n 1); \
	if [ -z "$$cid" ]; then echo "No running container found for image 'codeserver:latest'."; exit 1; fi; \
	echo "Using container $$cid"; \
	docker exec -it $$cid supervisorctl stop gunicorn; \
	docker exec -it $$cid flask -A cspawn.app:app  run --debug --without-threads -h 0.0.0.0 -p 8000 

shell:
	@cid=$$(docker ps -q --filter ancestor=codeserver:latest --filter status=running | head -n 1); \
	if [ -z "$$cid" ]; then echo "No running container found for image 'codeserver:latest'."; exit 1; fi; \
	echo "Using container $$cid"; \
	docker exec -it $$cid /bin/bash

