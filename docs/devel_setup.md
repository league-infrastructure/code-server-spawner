# Setting up for Development


# Docker

You will need to install Docker or Orbstack on your development machine. 

Create a single node docker swarm with `docker swarm init`

Create a network:

``` bash 
docker network create --driver=overlay caddy
docker network create --driver=overlay jtlctl
```

# Database

Run the `docker-postgres-pgadmin` docker service to get a Postgres database and
PQAdmin administration. 

In the data directory, run the make `create` target to create the database. 

