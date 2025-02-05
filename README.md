#  Code Server Spawner

This application prodives a web application for users to create code-server
instances on a server. These containers are tailored with 
the League's Python-Apprentice curiculum. 

## Development

### Setup. 

After cloning, follow the [instructions for configuring secrets](https://github.com/league-infrastructure/league-infrastructure/wiki/Repository-Secrets)



## NFS Volumes


docker volume create \
  --driver local \
  --opt type=nfs \
  --opt o=addr=10.124.0.9,rw \
  --opt device=:/mnt/student_repos \
  student_repos

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