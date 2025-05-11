-- Create user 'codeserver' with password 'codeserverpw'
CREATE USER codeserver WITH PASSWORD 'codeserverpw';

-- Create database 'codeserver' owned by 'codeserver' user
CREATE DATABASE codeserver OWNER codeserver;

-- Connect to the newly created database
\c codeserver

-- Grant all privileges on all tables in the database to 'codeserver'
GRANT ALL PRIVILEGES ON DATABASE codeserver TO codeserver;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO codeserver;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO codeserver;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO codeserver;

-- Allow 'codeserver' to create new schema objects
ALTER USER codeserver CREATEDB;

-- Make sure user has access to future tables as well
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT ALL PRIVILEGES ON TABLES TO codeserver;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT ALL PRIVILEGES ON SEQUENCES TO codeserver;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT ALL PRIVILEGES ON FUNCTIONS TO codeserver;
