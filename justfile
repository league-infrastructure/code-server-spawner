FAPP := "cspawn.app:app"

# Extract version from pyproject.toml
version := `grep '^version =' pyproject.toml | sed 's/version = "\(.*\)"/\1/'`

# List available recipes
default:
    @just --list

# Print the current version
ver:
    @echo {{version}}

# Write __version__.py and compile requirements
compile:
    echo "__version__ = '{{version}}'" > cspawn/__version__.py
    uv pip compile --refresh pyproject.toml -o requirements.txt

# Commit, tag, and push a release
push: compile
    git commit --allow-empty -a -m "Release version {{version}}"
    git push
    git tag v{{version}}
    git push --tags

# Create the virtual environment
setup:
    uv venv --link-mode symlink

# Connect to the development database
dbshell:
    PGPASSWORD=password psql -h localhost -p 5432 -U codeserv -d codeserv

# Run the dev server with the devel config
dev:
    dotconfig load -d devel -o .env
    flask --app {{FAPP}} run --debug --host 0.0.0.0 --port 5000

# Run the dev server with the local-prod config
dev-pl:
    dotconfig load -d local-prod -o .env
    flask --app {{FAPP}} run --debug --host 0.0.0.0 --port 5000

# List application routes
routes:
    @flask --app {{FAPP}} routes

# Initialize migrations
init:
    @flask --app {{FAPP}} db init

# Generate a migration
migrate:
    @flask --app {{FAPP}} db migrate -m'.'

# Apply migrations
upgrade:
    @flask --app {{FAPP}} db upgrade
