# Simple helpers for building and deploying the stack

STACK ?= codeserver
FILE  ?= docker-stack.yaml

.PHONY: build up down networks env-file status logs redeploy debug

# Build images referenced by the stack using Compose (Stack ignores build)
build:
	@docker compose -f $(FILE) build codeserver

build-clean:
	@docker compose -f $(FILE) build codeserver --no-cache


# Create required overlay networks (idempotent)
networks:
	@docker network inspect caddy >/dev/null 2>&1 || docker network create --driver overlay --attachable caddy
	@docker network inspect jtlctl >/dev/null 2>&1 || docker network create --driver overlay --attachable jtlctl

# Generate runtime env-file from dotconfig (secrets arrive at runtime, not baked in image).
# dotconfig's config dir is ../config (this Makefile lives in docker/); .env lands here
# in docker/, next to the stack file that references it via env_file:.
env-file:
	@dotconfig -c ../config load -d prod --no-export -e -o .env

# Deploy the stack to Swarm
up: networks env-file
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
	@cid=$$(docker ps -q --filter name=$(STACK)_codeserver --filter status=running | head -n 1); \
	if [ -z "$$cid" ]; then echo "No running container found for $(STACK)_codeserver"; exit 1; fi; \
	docker exec -it $$cid supervisorctl stop gunicorn; \
	docker exec -it $$cid flask -A cspawn.app:app  run --debug --without-threads -h 0.0.0.0 -p 8000 


shell:
	@cid=$$(docker ps -q --filter name=$(STACK)_codeserver --filter status=running | head -n 1); \
	if [ -z "$$cid" ]; then echo "No running container found for $(STACK)_codeserver"; exit 1; fi; \
	docker exec -it $$cid /bin/bash

cspawnctl:
	docker compose -f $(DOCKER_STACK_FILE) run --rm codeserver cspawnctl $(ARGS)

migrate:
	@cid=$$(docker ps -q --filter name=$(STACK)_codeserver --filter status=running | head -n 1); \
	if [ -z "$$cid" ]; then echo "No running container found for $(STACK)_codeserver"; exit 1; fi; \
	docker exec -it $$cid flask -A cspawn.app:app db migrate
