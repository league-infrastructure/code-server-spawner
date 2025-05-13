# Setting up for Development


# Docker

You will need to install Docker or Orbstack on your development machine. 

Create a single node docker swarm with `docker swarm init`

Create a network:

``` bash 
docker network create --driver overlay --attachable caddy
docker network create --driver overlay --attachable jtlctl
```

# Database

Run the `docker-postgres-pgadmin` docker service to get a Postgres database and
PQAdmin administration. 

In the data directory, run the make `create` target to create the database. 


# SSL and Chrome Security


For local dev on Chrome you might get: 
```

log.ts:460   ERR 'crypto.subtle' is not available so webviews will not work. This is likely because the editor is not running in a secure context (https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts).: Error: 'crypto.subtle' is not available so webviews will not work. This is likely because the editor is not running in a secure context 
```

You will also get a popup in VSCode, and the webviews (LIke the virtual display ) won't work.

You can set this config you treat your development hostname as secure: 

```
chrome://flags/#unsafely-treat-insecure-origin-as-secure
```