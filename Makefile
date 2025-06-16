.PHONY: setup build publish compile up down

VERSION := $(shell grep '^version =' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
FAPP := cspawn.app:app 

ver:
	@echo $(VERSION)

compile:
	echo "__version__ = '$(VERSION)'" > cspawn/__version__.py
	uv pip compile --refresh pyproject.toml -o requirements.txt

push: compile
	git commit --allow-empty -a -m "Release version $(VERSION)"
	git push
	git tag v$(VERSION) 
	git push --tags

setup:
	uv venv --link-mode symlink


# Docker 

DOCKER_CONTEXT := swarm1
DOCKER_STACK_FILE := docker/docker-stack.yaml
DOCKER_COMPOSE_FILE := docker/docker-compose.yaml

build: compile
	docker compose -f $(DOCKER_STACK_FILE) build --no-cache
	docker tag codeserv codeserv:$(VERSION)

up:
	docker stack deploy --detach=false -c $(DOCKER_STACK_FILE) codeserv 

down:
	docker stack rm codeserv

shell:
	docker --context $(DOCKER_CONTEXT) compose -f $(DOCKER_STACK_FILE)   run --rm codeserv /bin/bash

flask:
	docker compose -f $(DOCKER_STACK_FILE)   run --rm codeserv flask -A cspawn.app:app shell 

logs:
	docker service   logs --tail "1000" -f codeserv_codeserv


dbinfo:
	docker compose -f $(DOCKER_STACK_FILE)   run --rm codeserv cspawnctl db info

tunnel:
	ssh   -R 5000:0.0.0.0:5000 -p 2222 tunnel@swarm1.dojtl.net -N


# for development database
dbshell:
	PGPASSWORD=password psql -h localhost -p 5432 -U codeserv -d codeserv 

routes:
	@flask --app $(FAPP) routes

# Make a migration

init:
	@flask --app $(FAPP) db init

migrate:
	@flask --app $(FAPP) db migrate -m'.'

# Run the migration
upgrade:
	@flask --app $(FAPP) db upgrade
