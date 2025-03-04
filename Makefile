
.PHONY: setup build publish compile


VERSION := $(shell grep '^version =' pyproject.toml | sed 's/version = "\(.*\)"/\1/')

FAPP := cspawn.app:app 

ver:
	@echo $(VERSION)

compile:
	uv pip compile --refresh pyproject.toml -o requirements.txt

push: compile
	echo "__version__ = '$(VERSION)'" > cspawn/__version__.py
	git commit --allow-empty -a -m "Release version $(VERSION)"
	git push
	git tag v$(VERSION) 
	git push --tags

setup:
	uv venv --link-mode symlink

build:
	docker compose build 


# for development database
dbshell:
	PGPASSWORD=password psql -h localhost -p 5432 -U pguser -d pguser_db 

routes:
	@flask --app $(FAPP) routes

migrate:
	@flask --app $(FAPP) db migrate -m'.'

upgrade:
	@flask --app $(FAPP) db upgrade
