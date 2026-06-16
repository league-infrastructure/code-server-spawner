---
type: overview
project: code-server-spawner
---

# code-server-spawner

A Flask web application that provisions and manages per-student VS Code
(code-server) containers for The League (jointheleague.org). Instructors
start a class session; students log in and launch a browser-based coding
environment pre-loaded with the class curriculum.

## Actors

- **Student** — registers via class code, logs in, starts and uses a
  code-server host.
- **Instructor** — manages classes, starts class sessions, monitors
  student hosts.
- **Admin / Operator** — manages the fleet via the `cspawnctl` CLI:
  provisions swarm nodes, purges stale hosts, runs load tests.

## Core Capabilities

- **Authentication** — Google OAuth or username + password/class-code login;
  class-code registration for new students.
- **Class & Student Management** — CRUD for Classes (scheduled sessions with
  a join code) and ClassProtos (templates: Docker image + GitHub repo).
  Instructors view rosters; admins manage all users.
- **Host Provisioning** — on demand, the app forks the class GitHub repo into
  the League-Students org, creates a Docker Swarm service running the
  code-server image, and routes it via a Caddy reverse proxy with basic auth.
  Docker spread scheduling distributes hosts across available swarm nodes.
- **Host Lifecycle** — hosts can be stopped (service removed), purged (bulk),
  or reaped automatically when quiescent (no heartbeat or file edits for
  configurable thresholds).
- **CLI Operations (`cspawnctl`)** — manages swarm nodes (DigitalOcean droplet
  provisioning + swarm join), host bulk operations, database management,
  telemetry, and load-test fixtures.

## Main Components

| Component | Purpose |
|---|---|
| `cspawn/auth/` | Login, registration, Google OAuth, session management |
| `cspawn/main/` | Web routes: index, hosts, classes, telemetry, cron |
| `cspawn/admin/` | Admin routes: user and host management |
| `cspawn/models.py` | SQLAlchemy models: User, Class, ClassProto, CodeHost |
| `cspawn/cs_docker/` | Docker Swarm orchestration (CSManager, CSMService) |
| `cspawn/cs_github/` | GitHub org fork management |
| `cspawn/cli/` | `cspawnctl` CLI: node, host, db, telem, test groups |
| `cspawn/util/` | Auth helpers, config, logging, host utilities |

## Infrastructure

- **Docker Swarm** on DigitalOcean droplets
- **Caddy** reverse proxy for per-host routing and basic auth
- **PostgreSQL** database
- **GitHub org** (League-Students) for student repo forks
- Deployed via `prod` / `local-prod` config environments
