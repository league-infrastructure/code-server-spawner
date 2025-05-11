.PHONY: create drop recreate help

# Default PostgreSQL connection parameters matching .env file
POSTGRES_CONTAINER := docker-postgres-pgadmin-postgres-1
POSTGRES_USER := pguser
POSTGRES_DB := pguser_db

# Target to create the database using the setup script
create:
	@echo "Creating database using Docker..."
	docker exec -i $(POSTGRES_CONTAINER) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) < setup-database.sql
	@echo "Database created successfully."

# Additional useful targets
drop:
	@echo "Dropping database..."
	docker exec -i $(POSTGRES_CONTAINER) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c "DROP DATABASE IF EXISTS codeserver;"
	docker exec -i $(POSTGRES_CONTAINER) psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c "DROP USER IF EXISTS codeserver;"
	@echo "Database and user dropped."

recreate: drop create
	@echo "Database recreated successfully."

# Display usage information
help:
	@echo "Available targets:"
	@echo "  create    - Create the codeserver database and user"
	@echo "  drop      - Drop the codeserver database and user"
	@echo "  recreate  - Drop and recreate the database"
	@echo "  help      - Show this help message"
