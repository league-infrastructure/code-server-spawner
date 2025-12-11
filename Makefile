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
