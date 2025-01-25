
.PHONY: setup build publish compile

compile:
	uv pip compile pyproject.toml -o requirements.txt

publish: build compile
	uv publish --token $$UV_PUBLISH_TOKEN

build:
	uv build

setup:
	uv venv --link-mode symlink

