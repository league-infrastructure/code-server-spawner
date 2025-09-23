# Simple helpers for building and deploying the stack

STACK ?= codeserver
FILE  ?= docker-stack.yaml

.PHONY: build up down networks status logs redeploy

# Build images referenced by the stack using Compose (Stack ignores build)
build:
	@docker compose -f $(FILE) build codeserver

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
	@docker service logs $(STACK)_codeserver

redeploy: build up
