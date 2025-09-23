# caddy-proxy

Run this on a docker server to implement a reverse proxy. You can then configure other docker containers to 
use this proxy, automatically getting HTTPS certificates. 

To run the proxy on a single docker server:

```
docker compose up -d
```

For docker swarm, you can use the `docker-stack.yml` file. 

```
docker stack deploy -c docker-stack.yml caddy
```

Here is an example of configuration for a proxied service:

```
version: '3'

services:

  whoami:
    image: traefik/whoami
    networks:
      - caddy
    labels:
      caddy: whoami.do.jointheleague.org
      caddy.reverse_proxy: "{{upstreams 80}}"

      caddy.basicauth: "*"
      caddy.basicauth.code: ${PASSWORD}

volumes:
  leaguesync-data:

networks:
  caddy:
    external: true
```

Important points: 

* Services that will get proxied must also be on the `cady` network, using both the external networks
  definition at the end of the file and the `networks: caddy` entry in the service definition
* The first caddy label, `caddy`, sets the domain name. This sould be set up in DNS as an A record
  that points to the server where caddy is running.
* The second caddy label, `caddy.reverse_proxy: "{{upstreams 80}}"` configures the port on the service to proxy to.
* The third and fourth labels, `caddy.basicauth` are only necessary if you want to set up basic auth.  


## Hostnames

The wildcard `*.do.jointheleague.org` is currently assigned to a Digital Ocean server, so if you use the
Digital Ocean docker host, you can just create a name with a  `.do.jonitheleague.org` suffix in 
your services definition.

## Basic Auth

To create a password, run:

```
caddy hash-password --plaintext '4life'
```

If you don't have caddy installed locally, you can run it from docker ( assuming that you have started the 
cady proxy already and the container is named `caddy-caddy-1` ):

```
docker exec caddy-caddy-1 caddy hash-password --plaintext '4life'
```

Of, if the proxy is not running, 

```
docker run 'lucaslorentz/caddy-docker-proxy:ci-alpine'  hash-password --plaintext '4life' 
```


