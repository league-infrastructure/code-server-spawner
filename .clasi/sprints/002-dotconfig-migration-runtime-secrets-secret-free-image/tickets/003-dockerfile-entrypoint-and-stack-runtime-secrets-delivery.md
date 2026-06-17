---
id: '003'
title: Dockerfile entrypoint and stack runtime secrets delivery
status: open
use-cases: [SUC-002, SUC-003]
depends-on: ['002']
github-issue: ''
issue: dotconfig-migration.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Dockerfile entrypoint and stack runtime secrets delivery

## Description

Remove all secret material from the Docker image layers and deliver secrets
exclusively at runtime via a dotconfig-generated env-file. This involves:

1. Stripping `COPY config/secrets/*` and `COPY ... id_rsa` lines from
   `docker/Dockerfile`.
2. Writing a shell entrypoint wrapper (`docker/entrypoint.sh`) that decodes
   `$ID_RSA` → `/root/.ssh/id_rsa` at container start time.
3. Adding `env_file: .env` to the `codeserver` service in
   `docker/docker-stack.yaml`.
4. Adding a Makefile `env-file` target that generates `.env` via
   `dotconfig load -d prod --no-export -e -o .env` before `make up`.

After this ticket, the prod image contains no credentials in any layer.

**Open question resolved before implementation**: The current Dockerfile appends
`id_rsa.pub` to `/root/.ssh/authorized_keys` (line 90), enabling other swarm
nodes to SSH into the container. Clarify with the stakeholder whether this must
be preserved. If yes, deliver `ID_RSA_PUB` via the env-file and decode it in
the entrypoint alongside `ID_RSA`. If no, remove the `authorized_keys` step
entirely. This decision affects the entrypoint script and the dotconfig config
(whether `ID_RSA_PUB_FILE=id_rsa.pub` is declared).

## Acceptance Criteria

- [ ] `docker/Dockerfile` has no `COPY config/secrets/*` lines (lines 76-79
      and 88-93 in the current file are removed or replaced).
- [ ] `docker/Dockerfile` has no `COPY config/secrets/id_rsa` or
      `COPY config/secrets/id_rsa.pub` lines.
- [ ] `docker/entrypoint.sh` exists, is executable (`chmod +x`), and:
      - Decodes `$ID_RSA` (base64) to `/root/.ssh/id_rsa`.
      - Sets `chmod 600 /root/.ssh/id_rsa`.
      - Unsets `ID_RSA` from the environment (`unset ID_RSA`).
      - (Conditional on open-question resolution) decodes `$ID_RSA_PUB` →
        `/root/.ssh/id_rsa.pub` and appends to `authorized_keys` if needed.
      - Execs the original CMD (`exec "$@"`).
- [ ] `docker/Dockerfile` `ENTRYPOINT` is updated to
      `["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]` (or equivalent
      path to the copied entrypoint script).
- [ ] `docker/docker-stack.yaml` `codeserver` service has `env_file: [.env]`
      (or `env_file: .env`) and the existing `environment:` block is retained
      with `JTL_DEPLOYMENT: "prod"` as an override.
- [ ] `docker/Makefile` has an `env-file` target:
      `dotconfig load -d prod --no-export -e -o .env`
      and `up` depends on `env-file` (i.e. `up: networks env-file`).
- [ ] `make build` produces an image; `docker history <image>` shows no secret
      values in any layer (verified by scanning history output for known secret
      substrings).
- [ ] `make redeploy` (= `build` then `up`) runs end-to-end, generating the
      env-file and deploying the stack, without requiring manual steps.
- [ ] Container starts successfully on the swarm; a real code-server host-start
      works (SSH to a worker node from within the container succeeds — verifies
      that `/root/.ssh/id_rsa` was decoded correctly by the entrypoint).

## Implementation Plan

### Approach

#### 1. Write `docker/entrypoint.sh`

```sh
#!/bin/sh
set -e

# Decode SSH private key from base64 env var
if [ -n "$ID_RSA" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    printf '%s' "$ID_RSA" | base64 -d > /root/.ssh/id_rsa
    chmod 600 /root/.ssh/id_rsa
    unset ID_RSA
fi

# (If authorized_keys must be preserved)
# if [ -n "$ID_RSA_PUB" ]; then
#     printf '%s' "$ID_RSA_PUB" | base64 -d > /root/.ssh/id_rsa.pub
#     chmod 644 /root/.ssh/id_rsa.pub
#     cat /root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys
#     unset ID_RSA_PUB
# fi

exec "$@"
```

#### 2. Update `docker/Dockerfile`

Remove lines 76-93 (all `COPY config/...` and SSH key lines). Add:

```dockerfile
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod +x /app/docker/entrypoint.sh

RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
```

Keep the existing `RUN mkdir -p /root/.ssh` and `RUN chmod 700 /root/.ssh`
if already present (consolidate as needed).

#### 3. Update `docker/docker-stack.yaml`

In the `codeserver` service, add:

```yaml
env_file:
  - .env
```

Retain the existing `environment:` block:

```yaml
environment:
  JTL_DEPLOYMENT: "prod"
```

The `environment:` block takes priority over `env_file:` in Docker Compose/Stack,
so `JTL_DEPLOYMENT` remains the correct override.

#### 4. Update `docker/Makefile`

```makefile
.PHONY: env-file
env-file:
    @dotconfig load -d prod --no-export -e -o .env

up: networks env-file
    @docker stack deploy --detach=true -c $(FILE) $(STACK)
```

### Files to Create

- `docker/entrypoint.sh`

### Files to Modify

- `docker/Dockerfile` — remove secret COPY lines; add entrypoint; update ENTRYPOINT
- `docker/docker-stack.yaml` — add `env_file:`
- `docker/Makefile` — add `env-file` target; update `up` dependencies

### Testing Plan

- Build the image: `make build`.
- Scan layers: `docker history codeserver --no-trunc | grep -i "secret\|id_rsa\|password"` — must produce no output.
- Inspect image filesystem: `docker run --rm --entrypoint sh codeserver -c "ls /app/config/secrets/ 2>/dev/null || echo CLEAN"` — must print `CLEAN`.
- Test entrypoint decoding locally:
  ```sh
  export ID_RSA=$(base64 < config/secrets/id_rsa)
  docker run --rm -e ID_RSA=$ID_RSA codeserver sh -c "ls -la /root/.ssh/"
  ```
  Verify `/root/.ssh/id_rsa` exists with permissions `600`.
- Full deploy test: `make redeploy` on the swarm; confirm the service starts
  and a host-start succeeds.

### Rollback Note

Tag the last git-crypt image as `codeserver:pre-dotconfig` on the registry
before building the new image. If the new deploy fails, re-deploy the tagged
image via `docker service update --image codeserver:pre-dotconfig codeserver_codeserver`.
