# docker-flask-gunicorn

This repo is a template for applications that employ: 

* A flask application, running on gunicorn
* An nginx file server
* A flask application 

You can use this as a starting point for a new project, deleting 
the components you don't need.

The containers share a volume, `/opt/data`.

It's based on https://github.com/ericlincn/docker-flask-gunicorn


# Getting started

* Create a new repository using "Use this template" button, rather than cloning,
  then clone the repository to your local machine.
* Set up secrets, [using these instructions](https://github.com/league-infrastructure/league-infrastructure/wiki/Repository-Secrets)
* Add the token from the note in last pass, "jointheleague-it Github Clone Only Token" to the file `secrets/github-token.txt`
* Remove any of the components you don't need, such as the flask app or the nginx server. Delete the 
  component's director, then remove it from the `docker-compose.yaml` file.


## Usage

First you will need to unlock the secrets, 
[using these instructions](https://github.com/league-infrastructure/league-infrastructure/blob/master/Repo_Secrets.md)


You can start this stack with `docker-compose`:

```bash
docker-compose up
```

The applications are designed to be run behind Caddy reverse proxy. 
The `labels.caddy`  configuration is the name of the service in the `docker-compose.yml` file. 
If you are deploying to the League proxy on digital ocean, just change the first part of the
domain name and the Cady proxy will auto detect the service and create a route. 

If you run it locally on docker:

* Flask is running at: `http://localhost:8090/`
* NGINX is running at: `http://localhost:8091/`

### cron




### Flask

From docker, test the flask app with a call to `http://localhost:8090/`


### nginx

The nginx container is configured to serve files from :

* `/opt/data/html` via `/`, which is on the `/opt/data` volume that is available to
  other containers.
* `/usr/share/nginx/html/` via `/local/` which is the default nginx html directory.

Additionally, `/local/` is a fallback for '/' so that if a file is not found in
`/opt/data/html` it will look in `/usr/share/nginx/html/`. Here 'local' means
local to the nginx container, the static files that are includes in `./nginx/html`.


Some of the examples paths are:

* `http://localhost:8091/`. 
* `http://localhost:8091/last-cron-time.txt` A file from the `/opt/data` volume, written by the cron job.
* `http://localhost:8091/local/figures.html` Files from the root of the `nginx/html` directory
* `http://localhost:8091/private/index.html`. A private file uses, using basic auth and the passwrods in `.htpasswd`
* `http://localhost:8091/custom-block`. A custom block configured in `nginx.conf`
* 
