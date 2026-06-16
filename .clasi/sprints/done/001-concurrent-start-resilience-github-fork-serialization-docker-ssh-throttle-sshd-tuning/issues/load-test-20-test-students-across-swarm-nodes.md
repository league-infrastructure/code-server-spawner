---
status: in-progress
sprint: '001'
tickets:
- 001-001
---

# Load Test: 20 Test Students Across Swarm Nodes

## Context

We need to validate that the code-server-spawner correctly distributes student
code-server hosts across multiple Docker Swarm nodes and that startup performance
is acceptable under classroom-like load (many students starting at once). Today
there is no repeatable way to spin up a cohort of students, start their hosts,
measure how they spread across nodes, and tear everything down cleanly.

This adds a `cspawnctl test` command group that can **set up** a test class +
20 students, **start** their hosts in parallel while timing each, **report**
performance/distribution metrics, and **tear down** everything (hosts, students,
class) so the test is idempotent and leaves no residue.

The main thing under test: hosts land on different swarm nodes (Docker Swarm
spread scheduling — placement constraints are explicitly removed in
[manager.py:319](cspawn/cs_docker/manager.py#L319)) and start with good latency.

## Approach

Add a new CLI group `test` mirroring the existing pattern in
[cspawn/cli/node.py](cspawn/cli/node.py) and [cspawn/cli/host.py](cspawn/cli/host.py).
All commands use `@click.pass_context`, `get_app(ctx)`, and run inside
`with app.app_context():`. Reuse `app.csm` (the `CodeServerManager`) for host
lifecycle — do **not** reimplement Docker logic.

Naming constants: usernames `teststudent01`..`teststudent20`, emails
`teststudentNN@students.jointheleague.org` (matches `STUDENT_EMAIL_REGEX` so
`set_role_from_email` sets `is_student=True`), class name `Load Test Class`,
class_code `loadtest`.

### New file: `cspawn/cli/test.py`

```python
@cli.group()
def test():
    """Create and tear down load-test fixtures (students, class, hosts)."""
```

**Shared helpers (module-level):**
- `TEST_USERNAME_FMT = "teststudent{:02d}"`, `N_STUDENTS = 20`,
  `TEST_CLASS_CODE = "loadtest"`, `TEST_CLASS_NAME = "Load Test Class"`,
  `TEST_EMAIL_FMT = "teststudent{:02d}@students.jointheleague.org"`
- `_get_or_create_proto(app)` — find the Python Apprentice `ClassProto` by
  `name="Python Apprentice"` (or `repo_uri` match); if missing, create it from the
  values in [data/protos.json](data/protos.json) (image_uri
  `ghcr.io/league-infrastructure/.../docker-codeserver-python:v1.20250916.2`,
  repo_uri `https://github.com/league-curriculum/Python-Apprentice`), calling
  `ClassProto.set_hash(None, None, proto)` then committing — pattern from
  [util.py:58-90](cspawn/cli/util.py#L58-L90).
- `_get_or_create_class(app, proto)` — find `Class` by `class_code="loadtest"`;
  else construct with `name`, `proto_id=proto.id`,
  `start_date=now-1h`, `end_date=now+1d` (tz-aware, so `can_register`/`is_current`
  are true), `active=True`, `class_code="loadtest"`. Commit.
- `_iter_test_users()` — yields `(username, email)` for NN in 1..20.

**`test setup`** — idempotent fixtures, no hosts yet:
1. Get/create proto and class.
2. For each of 20: find `User` by username; if missing, construct
   `User(user_id=str(uuid4()), username=..., email=..., password="password",
   is_student=True)`, call `set_role_from_email(app, user)`, `db.session.add`.
3. Enroll each user into the class via `class_.students.append(user)` if not
   already enrolled. Single `db.session.commit()` at the end.
4. Echo summary (created vs. already-present counts).

**`test start`** — parallel host startup with timing:
- Options: `--concurrency/-c` (default 20), `--no-wait` (skip readiness poll),
  `--timeout` (readiness seconds, default 90).
- Load the 20 users + proto + class inside app context. Capture their **ids**
  (ints) up front — do NOT pass ORM objects into threads.
- Use `concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)`. Each
  worker opens its own `with app.app_context():`, re-fetches `User`/`ClassProto`/
  `Class` by id (fresh session per thread — avoids cross-thread ORM sharing
  flagged in exploration), then:
  - skip if `app.csm.get_by_username(username)` already exists;
  - `t0`; `s, ch = app.csm.new_cs(user, proto, class_)`; `t_created`;
  - if not `--no-wait`: `s.wait_until_ready(timeout=...)` (or poll `s.is_ready`);
    `t_ready`.
  - return a result dict: `{username, ok, err, create_s, ready_s, node_name}`
    (read `ch.node_name` after a `s.sync_to_db()` so the node is populated).
- Collect results; print the same metrics block as `test report`.
- Note in output: SQLite session-per-thread caveat handled; Postgres (prod/
  local-prod) supports concurrent sessions fine.

**`test report`** — read current state, no mutation:
- `app.csm.sync(check_ready=True)` to refresh DB from swarm.
- Query `CodeHost` rows whose `service_name` matches `teststudent%`
  (or join via the class's students). Compute and print, using `tabulate`:
  - **Latency**: min/max/mean/p95 of create and ready seconds (from `test start`
    results when chained; otherwise from `created_at`→`last_heartbeat` deltas).
  - **Node distribution**: count of hosts grouped by `node_name`.
  - **Failures**: hosts in state `mia`/`unknown` or not ready, with reason.
  - **Memory/utilization**: per-host `memory_usage` and per-node aggregate sum
    + `user_activity_rate`.

**`test teardown`** — remove everything (idempotent):
- Options: `--keep-students` (only stop hosts), `--keep-repos` (skip GitHub repo
  deletion), `-N/--dry-run`.
- For each test user: `s = app.csm.get_by_username(username)`; if `s`, `s.stop()`
  (removes swarm service). Delete matching `CodeHost` rows
  (`db.session.delete(ch)`), pattern from
  [hosts.py:38-51](cspawn/main/routes/hosts.py#L38-L51) and
  [host.py purge:188-217](cspawn/cli/host.py#L188-L217).
- **Delete the student's GitHub fork** (unless `--keep-repos`): build the org
  client once via `GithubOrg.new_org(app)`, then for each user call
  `gorg.remove(proto.repo_uri, username)` — this resolves the `{base}-{username}`
  fork name and calls `repo.delete()`, and is idempotent (returns False if the
  repo is already gone). See
  [repo.py:389-407](cspawn/cs_github/repo.py#L389-L407). This pairs with the
  `gorg.fork(proto.repo_uri, username)` that `new_cs` runs at start, so the 20
  forks created by `test start` are cleaned up rather than accumulating in the
  org. Note: repo deletion requires the `GITHUB_ORG_TOKEN` to have Administration
  write on `League-Students`.
- Unless `--keep-students`: remove each user from `class_.students`, delete the
  `User` rows, and delete the `Class` (leave the shared `ClassProto`). Single
  commit. Dry-run prints the plan only (services to stop, repos to delete, rows
  to delete).

### Register the group
Add `from .test import test  # noqa: W0611` to
[cspawn/cli/ctl.py](cspawn/cli/ctl.py) alongside the other group imports.

## Files
- **New:** `cspawn/cli/test.py` — the `test` group + `setup`/`start`/`report`/`teardown`.
- **Edit:** `cspawn/cli/ctl.py` — one import line to register the group.

## Reused functions (do not reinvent)
- `get_app`, `get_logger` — [cspawn/cli/util.py:193-205](cspawn/cli/util.py#L193-L205)
- `cast_app` — [cspawn/init.py](cspawn/init.py) (for `.csm`, `.db` typing)
- `set_role_from_email` — [cspawn/util/app_support.py:275-292](cspawn/util/app_support.py#L275-L292)
- `CodeServerManager.new_cs/get_by_username/stop_cs/get/sync` —
  [cspawn/cs_docker/csmanager.py:502-663](cspawn/cs_docker/csmanager.py#L502-L663)
- `CSMService.stop/wait_until_ready/is_ready/sync_to_db` —
  [cspawn/cs_docker/csmanager.py:37-203](cspawn/cs_docker/csmanager.py#L37-L203)
- `ClassProto.set_hash` — [cspawn/models.py:540](cspawn/models.py#L540)
- `GithubOrg.new_org` / `GithubOrg.remove(upstream_url, username)` —
  [cspawn/cs_github/repo.py:315-407](cspawn/cs_github/repo.py#L315-L407) (deletes
  the student fork on teardown; idempotent)

## Verification
Run against prod infra from the local app using the `local-prod` deploy
(requires a valid `DO_TOKEN`/`DOCKER_URI`; the swarm currently has node `swarm1`
— add a second node first to actually test distribution):

```bash
cspawnctl -d local-prod -v test setup        # creates class + 20 students
cspawnctl -d local-prod -v test start         # parallel start, prints latency+nodes
cspawnctl -d local-prod test report           # re-read distribution/memory
cspawnctl -d local-prod test teardown         # remove hosts + students + class
cspawnctl -d local-prod test teardown -N      # dry-run shows nothing left
```

Success criteria:
- `setup` is idempotent (re-running creates nothing new).
- `start` reports 20 hosts created, latency stats, and a node-distribution table;
  with ≥2 swarm nodes the 20 hosts are spread across nodes (not all on one).
- `report` shows per-node memory aggregates and zero failures for healthy hosts.
- `teardown` removes all swarm services + DB rows; a follow-up `report` shows none
  and `-N` teardown shows an empty plan.

## FOLLOW-UP (after first 20-wide run): app must absorb concurrent starts

The first `test start` (concurrency=20) confirmed the test works but exposed two
**application** bottlenecks — only 2/20 hosts started. Per stakeholder, the fix
is in the **app** (so 20 students clicking Start at once works in production),
not by watering down the test. Test stays 20-wide.

### Fix 1 — Serialize GitHub forks of the same upstream
`GithubOrg.fork` ([repo.py:358](cspawn/cs_github/repo.py#L358)) does fork→rename.
20 concurrent forks of the *same* `Python-Apprentice` →
`403 "Repository is already being forked"` + `429` secondary rate limit.
- Add a **process-wide lock + retry/backoff** around the upstream fork POST so
  concurrent `new_cs` calls queue on the same upstream and retry the transient
  403/429 until the first fork completes (then each rename is unique per student).
- Reuse the existing `_wait_repo_ready`/`_rename_with_retry` helpers; add a
  `threading.Lock` keyed by upstream URL and a bounded retry loop on the
  "already being forked" / 429 responses.

### Fix 2 — Don't overrun SSH to the swarm manager
All host ops go over one `ssh://root@swarm1` Docker transport
([csmanager.py:415](cspawn/cs_docker/csmanager.py#L415)). 20 threads hammering it
concurrently → `kex_exchange_identification: Connection reset` / BrokenPipe.
- Add an **app-level semaphore** (e.g. `BoundedSemaphore(4)`) around Docker
  manager calls in `CodeServerManager` (`new_cs`, `list`, `get`,
  `get_by_username`) so the spawner self-throttles concurrent SSH/Docker use
  regardless of how many students click at once. This is the "rate throttle in
  our application" the stakeholder asked for.
- Keep the single shared manager client; do not create per-call clients on the
  hot path.

### Fix 3 — Raise sshd MaxStartups on the manager node
Even with app throttling, bump the manager's tolerance:
- On `swarm1` (and via the node-provisioning cloud-init so new nodes inherit it):
  set `sshd_config` `MaxStartups 30:60:100` (from default `10:30:100`) and review
  the UFW port-22 rate limit referenced in [node.py](cspawn/cli/node.py) comments.
- Files: the host-scripts / cloud-init under
  [config/host-scripts/](config/host-scripts/) and
  [config/cloud-init/](config/cloud-init/) (`swarm-node-init-v2.yaml`).

### Verification for the follow-up
- Re-run `cspawnctl -d local-prod test setup` then `test start` (concurrency=20):
  expect all 20 to reach `ok` (forks serialized, SSH throttled), with the latency
  table showing the queueing effect.
- Confirm the **web path** still works (single student Start) — the throttle/lock
  must not deadlock or noticeably slow a lone start.
- `test teardown` cleans up all 20 hosts + forks + students.

## Notes / risks
- **Node distribution requires ≥2 nodes.** The swarm currently has only `swarm1`;
  with one node all 20 land there (expected). Use `cspawnctl node expand` to add a
  node before the real distribution test.
- **Each `new_cs` forks a GitHub repo** under `GITHUB_ORG` using `GITHUB_ORG_TOKEN`
  — 20 forks of Python-Apprentice. `test teardown` deletes these via
  `GithubOrg.remove` by default (`--keep-repos` to skip), so forks don't
  accumulate in the org. Requires the org token to have repo Administration write.
- **`get_unused_port` random collision** under heavy concurrency
  ([csmanager.py:429-450](cspawn/cs_docker/csmanager.py#L429-L450)) is a known
  contention point the parallel `start` will exercise — surface any failures in
  the report rather than papering over them.
