  ## Clean up Docker

Check docker use: `docker system df`

```bash
  # Remove all stopped containers
docker container prune -f

# Remove all unused images (including dangling and unreferenced ones)
docker image prune -a -f

# Remove all unused volumes
docker volume prune -f

# Remove all unused networks
docker network prune -f

# Remove all build cache
docker builder prune -f

# Remove everything unused (images, stopped containers, networks, volumes)
docker system prune -a --volumes -f
```