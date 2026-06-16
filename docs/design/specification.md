---
type: specification
project: code-server-spawner
---

# Specification

Documents existing system behavior by area.

## Authentication

- **Google OAuth** is the default login path; new users are auto-created on
  first OAuth callback; role is inferred from email domain.
- **Username + password or class code** login is supported via a form at
  `/auth/login`; a student may use any class-code for a class they are
  enrolled in in place of their personal password.
- **Registration** — Google path: student supplies a class code, is redirected
  to Google, then enrolled on callback. Username/password path: student
  chooses a username, sets a password, supplies a class code.
- Users have roles: `student`, `instructor`, `admin`. Roles are flags on the
  `User` model; `is_admin` and `is_instructor` can be set by an admin.
- Session management via Flask-Login; Google tokens revoked on logout.

## Class and Student Management

- **ClassProto** records define a reusable template: Docker image URI, GitHub
  repo URI, branch, and startup script. A proto can be shared (`is_public`).
- **Class** records reference a ClassProto and carry scheduling fields
  (`start_date`, `end_date`, `recurrence_rule`), a unique `class_code`, and
  status flags (`active`, `running`, `hidden`, `public`).
- Instructors are assigned to a class via a `class_instructors` join table;
  students via `class_students`.
- A class transitions through states: `can_register` (within date range),
  `can_start` (active, not yet running), `running` (instructor has started
  the session), closed (past `end_date`).
- Instructors view a roster of enrolled students and their host states.
- Admins can create/edit/delete classes, protos, and users.

## Host Provisioning and Lifecycle

- A **CodeHost** record represents one running code-server Docker Swarm
  service for a user in a class.
- Provisioning sequence:
  1. Fork the class's GitHub repo into the League-Students org for the student.
  2. Create a Docker Swarm service from the proto's image URI; Docker's spread
     scheduler places it on an available worker node.
  3. A Caddy reverse-proxy entry is configured with a per-host password for
     basic auth.
  4. The host polls `/host/is_ready` until `app_state == "ready"`, then
     redirects the student to the public URL.
- Host states: `unknown`, `starting`, `running`, `ready`, `mia`.
- **Stop**: removes the Docker service and deletes the CodeHost record.
- **Reap / purge**: a host is considered quiescent if the last heartbeat is
  >20 minutes ago or last file edit >15 minutes ago; purgeable if MIA or
  inactive >50 minutes. Bulk purge is available via admin UI and CLI.
- Telemetry heartbeats from the running container update `last_heartbeat`,
  `last_utilization`, `memory_usage`, and keystroke-rate fields.

## Node and Cluster Management (CLI)

- `cspawnctl node expand` provisions a new DigitalOcean droplet, waits for it
  to become active, SSHes in to set the hostname, then joins it to the Docker
  Swarm as a worker.
- `cspawnctl node info` shows a combined view of swarm nodes and DO droplets
  (in-swarm vs cloud-only, purgeable status).
- `cspawnctl node stop` drains a node and removes it from the swarm.
- Node names follow a configurable template (e.g., `swarm{serial}.example.com`);
  DNS A records are synced via the DigitalOcean API.
- `cspawnctl host ls/start/stop/purge` manage hosts from the CLI.
- `cspawnctl db` supports export, import, and recreation of the database.

## Load Testing

- `cspawnctl test setup` creates a load-test class and 20 test student
  accounts enrolled in it.
- `cspawnctl test start` starts code-server hosts for all test students in
  parallel (via `ThreadPoolExecutor`), recording per-host timings.
- `cspawnctl test report` reports timing distribution and host state.
- `cspawnctl test teardown` stops hosts, deletes GitHub forks, removes test
  students and the class.
