import base64
import re
import hashlib
import socket
import time
import logging
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass, field

import click
import digitalocean
import docker
import paramiko

from .root import cli
from .util import get_config, get_logger
from cspawn.util.config import find_parent_dir
from cspawn.cs_docker.tiers import Tier, load_tiers, default_tier, tier_by_name

# Suppress Paramiko's verbose host key logging
logging.getLogger("paramiko.transport").setLevel(logging.WARNING)


@cli.group()
def node():
    """Manage nodes in the cluster."""
    pass


@node.command()
@click.option("-d", "--drain", required=True, help="Drain the node named <node-name>.")
def drain(drain):
    pass


@node.command()
@click.option("-a", "--add", required=True, help="Add a new node to the cluster.")
def add(add):
    pass


@node.command()
@click.option("-r", "--rm", required=True, help="Remove a node from the cluster.")
def rm(rm):
    pass


# ---------------------------------------------------------------------------
# Shared helpers — used by both CLI commands and cspawn/cs_docker/autoscale.py
# ---------------------------------------------------------------------------

def count_hosts_per_node(client: docker.DockerClient) -> dict[str, int]:
    """Return {short_node_name: running_host_count} for all swarm nodes.

    Counts running tasks for services labeled jtl.codeserver=true.
    Shared between the 'hosts' command, 'contract' candidate selection,
    and the autoscale control loop (autoscale.py).
    """
    from collections import defaultdict

    node_name_map: dict[str, str] = {}
    for n in client.nodes.list():
        hn = n.attrs.get("Description", {}).get("Hostname", "") or n.id
        node_name_map[n.id] = hn.split(".")[0]

    per_node: dict[str, int] = defaultdict(int)
    for svc in client.services.list(filters={"label": "jtl.codeserver=true"}):
        # A codehost service can be removed (host stop/purge) between the
        # services.list() above and this svc.tasks() call. That makes tasks()
        # raise 404 NotFound; swallow it so one vanishing service never breaks
        # the whole count (and the admin Nodes page that renders it).
        try:
            tasks = svc.tasks(filters={"desired-state": "running"})
        except Exception:
            continue
        for t in tasks:
            if (t.get("Status", {}) or {}).get("State") != "running":
                continue
            nid = t.get("NodeID")
            short = node_name_map.get(nid, nid or "?")
            per_node[short] += 1

    return dict(per_node)


# Backward-compatible alias — existing callers (tests, CLI) can use either name.
_running_hosts_by_node = count_hosts_per_node


@node.command(name="hosts")
@click.option("-s", "--summary", is_flag=True,
              help="Only show the count of hosts per node, not the full list.")
@click.pass_context
def hosts(ctx, summary):
    """List running code-server hosts grouped by the swarm node they run on.

    Placement is read live from Swarm (where each task actually runs), so it
    reflects reschedules/drains rather than possibly-stale DB node_name values.
    """
    from collections import defaultdict

    cfg = get_config()
    docker_uri = cfg.get("DOCKER_URI")
    if not docker_uri:
        raise click.ClickException("Missing required config: DOCKER_URI")
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    # node id -> short hostname
    node_name = {}
    for n in client.nodes.list():
        hn = n.attrs.get("Description", {}).get("Hostname", "") or n.id
        node_name[n.id] = hn.split(".")[0]

    # Use _running_hosts_by_node for counts; build per_node with usernames for listing
    running_counts = _running_hosts_by_node(client)

    per_node = defaultdict(list)
    for svc in client.services.list(filters={"label": "jtl.codeserver=true"}):
        labels = svc.attrs.get("Spec", {}).get("Labels", {})
        uname = labels.get("jtl.codeserver.username") or svc.name
        # A service can be removed between list() and tasks() — skip it rather
        # than letting a 404 abort the whole listing.
        try:
            tasks = svc.tasks(filters={"desired-state": "running"})
        except Exception:
            continue
        for t in tasks:
            if (t.get("Status", {}) or {}).get("State") != "running":
                continue
            nid = t.get("NodeID")
            per_node[node_name.get(nid, nid or "?")].append(uname)

    total = sum(len(v) for v in per_node.values())
    if summary:
        click.echo(f"{'NODE':<12} HOSTS")
        for node in sorted(per_node):
            click.echo(f"{node:<12} {len(per_node[node])}")
        click.echo(f"{'TOTAL':<12} {total}")
    else:
        for node in sorted(per_node):
            click.echo(f"\n{node} ({len(per_node[node])}):")
            for u in sorted(per_node[node]):
                click.echo(f"  {u}")
        click.echo(f"\nTotal: {total} hosts on {len(per_node)} node(s)")


def _service_constraints(svc) -> list[str]:
    """Read the current placement constraints from a docker service spec."""
    return list(
        (((svc.attrs.get("Spec", {}) or {}).get("TaskTemplate", {}) or {})
         .get("Placement", {}) or {}).get("Constraints", []) or []
    )


def _pin_service_to_node(svc, node_fqdn: str) -> None:
    """Force a service onto a specific node via a node.hostname constraint.

    Preserves any pre-existing constraints (e.g. 'node.role != manager' from
    PLACEMENT_CONSTRAINTS) but replaces any prior 'node.hostname==' pin so the
    host doesn't accumulate conflicting pins across repeated rebalances.
    Calling update() with new constraints makes Swarm reschedule the task,
    which recreates the container on the target node. The /workspace data is
    on a shared NFS mount, so it travels with the user automatically.
    """
    kept = [c for c in _service_constraints(svc)
            if not c.replace(" ", "").startswith("node.hostname==")]
    kept.append(f"node.hostname=={node_fqdn}")
    svc.update(constraints=kept)


def _unpin_services_from_node(client, node_fqdn: str, *, log=None, dry_run: bool = False) -> int:
    """Strip any `node.hostname==<node_fqdn>` placement constraint from code-server services.

    Prevents permanently-orphaned tasks: a service hard-pinned to a node via
    `_pin_service_to_node()` (e.g. by `node rebalance`) cannot be rescheduled
    once that node is removed, so the pin must be cleared before the node is
    drained or destroyed. Matches both the fully-qualified and short-hostname
    forms of the pin (mirroring `_pin_service_to_node`'s own normalization).
    Only the matching `node.hostname==` constraint is removed — any other
    constraints on the service (e.g. `node.role != manager`) are preserved.
    Services with no pin, or a pin to a different node, are left untouched.
    Per-service failures are caught and logged as warnings; they do not
    prevent unpinning the remaining services. Returns the count unpinned.

    When `dry_run` is True, no `svc.update()` call is made at all — matching
    services are only counted, so the return value reports what *would* be
    unpinned without mutating any cluster state.
    """
    short = node_fqdn.split(".")[0]
    targets = {f"node.hostname=={node_fqdn}".replace(" ", ""),
               f"node.hostname=={short}".replace(" ", "")}

    unpinned = 0
    for svc in client.services.list(filters={"label": "jtl.codeserver=true"}):
        constraints = _service_constraints(svc)
        matching = [c for c in constraints if c.replace(" ", "") in targets]
        if not matching:
            continue
        if dry_run:
            unpinned += 1
            continue
        kept = [c for c in constraints if c not in matching]
        try:
            svc.update(constraints=kept)
            unpinned += 1
        except Exception as e:
            svc_name = getattr(svc, "name", None) or getattr(svc, "id", "?")
            if log:
                log.warning(f"[stop] Failed to clear node pin on service {svc_name}: {e}")
    return unpinned


@node.command()
@click.option("-N", "--dry-run", is_flag=True,
              help="Show the planned moves without changing anything.")
@click.option("--no-push", is_flag=True,
              help="Skip the safety git-push to GitHub before moving each host.")
@click.option("--max-moves", type=int, default=None,
              help="Cap the number of hosts relocated in this run.")
@click.pass_context
def rebalance(ctx, dry_run, no_push, max_moves):
    """Level code-host load across swarm nodes by relocating hosts.

    Docker Swarm never rebalances on its own: adding a node only attracts NEW
    hosts, leaving existing load on the original nodes. This command moves
    running hosts off the most-loaded nodes onto the least-loaded ones until
    the per-node counts differ by no more than one.

    Each move re-pins the service to its target node (a Swarm reschedule, so
    the container is recreated there). Workspace data lives on a shared NFS
    mount, so it follows the user automatically. By default each host is also
    pushed to GitHub first as a safety snapshot; --no-push skips that.
    """
    from collections import defaultdict

    cfg = get_config()
    docker_uri = cfg.get("DOCKER_URI")
    if not docker_uri:
        raise click.ClickException("Missing required config: DOCKER_URI")
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    # node id -> (short, fqdn); track which nodes may RECEIVE hosts (eligible:
    # worker role + active availability). Manager / drained / paused nodes are
    # valid sources but never targets.
    short_of: dict[str, str] = {}
    fqdn_of_short: dict[str, str] = {}
    eligible: list[str] = []
    for n in client.nodes.list():
        attrs = n.attrs
        hn = attrs.get("Description", {}).get("Hostname", "") or n.id
        short = hn.split(".")[0]
        short_of[n.id] = short
        fqdn_of_short[short] = hn
        spec = attrs.get("Spec", {}) or {}
        role = (spec.get("Role") or "").lower()
        availability = (spec.get("Availability") or "").lower()
        if role == "worker" and availability == "active":
            eligible.append(short)

    # Build live per-node membership: {short: [(username, docker_service)]}.
    placement: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for svc in client.services.list(filters={"label": "jtl.codeserver=true"}):
        labels = svc.attrs.get("Spec", {}).get("Labels", {})
        uname = labels.get("jtl.codeserver.username") or svc.name
        for t in svc.tasks(filters={"desired-state": "running"}):
            if (t.get("Status", {}) or {}).get("State") != "running":
                continue
            short = short_of.get(t.get("NodeID"), "?")
            placement[short].append((uname, svc))

    if not eligible:
        raise click.ClickException("No eligible (active worker) nodes to rebalance onto.")

    # Index services by username so the planner can work on names alone.
    svc_by_user = {u: s for hosts_ in placement.values() for (u, s) in hosts_}
    per_node_names = {n: [u for (u, _) in hosts_] for n, hosts_ in placement.items()}

    moves = plan_rebalance(per_node_names, eligible, max_moves=max_moves)

    if not moves:
        click.echo("Already balanced — no moves needed.")
        # Still show the distribution so the operator can confirm.
        for n in sorted(set(list(per_node_names) + eligible)):
            click.echo(f"  {n:<14} {len(per_node_names.get(n, []))}")
        return

    click.echo(f"Planned moves ({len(moves)}):")
    for user, src, tgt in moves:
        click.echo(f"  {user:<24} {src} -> {tgt}")

    if dry_run:
        click.echo("\nDry run — nothing changed.")
        return

    moved = 0
    failed = 0
    for user, src, tgt in moves:
        svc = svc_by_user.get(user)
        if svc is None:
            click.echo(f"  {user}: service vanished, skipping")
            failed += 1
            continue

        if not no_push:
            try:
                from cspawn.cs_github.repo import CodeHostRepo
                from cspawn.cli.util import get_app
                app = get_app(ctx)
                with app.app_context():
                    # Direct CodeHostRepo.push() call, not stop_host() — a
                    # rebalance re-pins the service and keeps the DB row, it
                    # doesn't stop it. Shares CodeHostRepo.push()'s ticket-001
                    # timeout hardening automatically since it's the same method.
                    CodeHostRepo.new_codehostrepo(app, user).push()
                click.echo(f"  {user}: pushed", nl=False)
            except Exception as e:
                # Data is safe on NFS regardless, so a push failure must not
                # block the move — just warn and proceed.
                click.echo(f"  {user}: push failed ({e}) — moving anyway", nl=False)
        else:
            click.echo(f"  {user}:", nl=False)

        target_fqdn = fqdn_of_short.get(tgt, tgt)
        try:
            _pin_service_to_node(svc, target_fqdn)
            click.echo(f" moved {src} -> {tgt}")
            moved += 1
        except Exception as e:
            click.echo(f" MOVE FAILED ({e})")
            failed += 1

    click.echo(f"\nRebalance complete: {moved} moved, {failed} failed.")


def plan_rebalance(per_node: dict[str, list], eligible: list[str],
                   max_moves: int | None = None) -> list[tuple[str, str, str]]:
    """Compute a list of host moves that levels load across eligible nodes.

    Greedy: repeatedly move one host from the most-loaded node to the
    least-loaded eligible node until the spread is at most 1 (so counts
    differ by no more than one — the best any integer split can do), or
    until max_moves is reached.

    Args:
        per_node: {short_node_name: [usernames]} live placement.
        eligible: short names of nodes that may RECEIVE hosts (workers that
            are not drained/paused). Hosts on non-eligible nodes are still
            counted as sources so a drained node can be emptied here too.
        max_moves: optional cap on the number of moves returned.

    Returns:
        List of (username, source_node, target_node) tuples, in order.
    """
    # Work on a mutable copy of the counts/membership.
    members = {n: list(v) for n, v in per_node.items()}
    # Every eligible node must appear even if it currently has zero hosts,
    # otherwise a brand-new empty node would never be chosen as a target.
    for n in eligible:
        members.setdefault(n, [])

    eligible_set = set(eligible)
    moves: list[tuple[str, str, str]] = []

    def _capped() -> bool:
        return max_moves is not None and len(moves) >= max_moves

    def _least_loaded_target() -> str | None:
        return min(eligible_set, key=lambda n: len(members[n]), default=None)

    # Phase 1: fully evacuate every non-eligible node (drained/paused/manager).
    # Their hosts must leave regardless of balance, spreading onto the
    # currently least-loaded eligible node one at a time.
    for src in list(members):
        if src in eligible_set:
            continue
        while members[src]:
            if _capped():
                return moves
            tgt = _least_loaded_target()
            if tgt is None:
                return moves
            user = members[src].pop()
            members[tgt].append(user)
            moves.append((user, src, tgt))

    # Phase 2: level load among eligible nodes until the spread is at most one
    # (the best any integer split can do).
    while not _capped():
        src = max(eligible_set, key=lambda n: len(members[n]))
        tgt = _least_loaded_target()
        if tgt is None or src == tgt:
            break
        if len(members[src]) - len(members[tgt]) <= 1:
            break
        user = members[src].pop()
        members[tgt].append(user)
        moves.append((user, src, tgt))

    return moves


def _compute_fingerprint(pub_key_str: str) -> str:
    """Compute MD5 fingerprint for an OpenSSH public key string."""
    try:
        parts = pub_key_str.strip().split()
        if len(parts) < 2:
            return ""
        raw = base64.b64decode(parts[1])
        md5 = hashlib.md5(raw).hexdigest()  # nosec - standard SSH fingerprint
        return ":".join(md5[i : i + 2] for i in range(0, len(md5), 2))
    except Exception:
        return ""


def _get_next_serial(manager_client: docker.DockerClient, name_template: str) -> tuple[int, str, str]:
    """Inspect swarm nodes, infer prefix from DO_NAMES template, and return next serial.

    Returns (next_serial, fqdn_prefix, short_prefix)
    """
    # Short prefix like 'swarm'
    short_pattern = name_template.split(".")[0]  # e.g., 'swarm{serial}'
    short_prefix = short_pattern.format(serial="")

    # List existing node short names and extract numeric suffixes
    max_serial = 0
    try:
        for node in manager_client.nodes.list():
            node_name = node.attrs.get("Description", {}).get("Hostname") or ""
            short = node_name.split(".")[0]
            if not short.startswith(short_prefix):
                continue
            suffix = short[len(short_prefix) :]
            if suffix.isdigit():
                max_serial = max(max_serial, int(suffix))
    except Exception:
        pass

    return max_serial + 1, name_template, short_prefix


def _wait_for_droplet_active(
    manager: digitalocean.Manager,
    droplet: digitalocean.Droplet,
    timeout: int = 600,
    log=None,
) -> str:
    """Wait until droplet is active and return its public IPv4 address."""
    start = time.time()
    deadline = start + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        droplet.load()
        status = getattr(droplet, "status", None)
        if log:
            log.info(f"[expand] Waiting for droplet to be active (attempt {attempt}) status={status}")
        if status == "active":
            # Retrieve public IPv4
            networks = getattr(droplet, "networks", {}) or {}
            for ip in networks.get("v4", []):
                if ip.get("type") == "public":
                    return ip.get("ip_address")
        time.sleep(5)
    raise TimeoutError("Droplet did not become active in time")


def _wait_for_ssh(
    host: str,
    port: int = 22,
    timeout: int = 900,
    log=None,
    key_path: Path | None = None,
    username: str = "root",
) -> None:
    """Wait for SSH by attempting a real handshake when a key is provided.

    Uses exponential backoff to avoid triggering UFW rate limiting on port 22.
    """
    start = time.time()
    deadline = start + timeout
    attempt = 0
    delay = 5.0
    while time.time() < deadline:
        attempt += 1
        try:
            if log:
                log.info(f"[expand] Probing SSH on {host}:{port} (attempt {attempt})")
            if key_path and key_path.exists():
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                pkey = paramiko.RSAKey.from_private_key_file(str(key_path))
                try:
                    ssh.connect(host, port=port, username=username, pkey=pkey, look_for_keys=False, timeout=8)
                    if log:
                        log.info("[expand] SSH handshake successful")
                    ssh.close()
                    return
                finally:
                    try:
                        ssh.close()
                    except Exception:
                        pass
            else:
                # Fallback to TCP port check
                with socket.create_connection((host, port), timeout=5):
                    if log:
                        log.info("[expand] SSH port is open")
                    return
        except Exception:
            # Ignore and backoff
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 30.0)
    raise TimeoutError(f"SSH not available on {host}:{port} in time (timeout={int(timeout)}s)")


def _wait_for_cloud_init(host: str, key_path: Path, *, username: str = "root",
                         timeout: int = 600, log=None) -> None:
    """Block until the node's cloud-init has finished its first-boot run.

    `_wait_for_ssh` returns as soon as sshd accepts ONE connection, but on the
    DO base image cloud-init is still running in the background for minutes:
    it installs/pins docker-ce, installs do-agent, reconfigures UFW, and finally
    restarts sshd. Running introspection/join commands during that window races
    the sshd restart and the UFW reconfigure, producing intermittent
    "Unable to connect to port 22" failures mid-sequence.

    `cloud-init status --wait` blocks until the run is complete (exit 0) or
    errored (exit 2; we proceed anyway and let later steps surface real
    problems). Best-effort: if the command is unavailable or the node never
    settles within `timeout`, we log and continue rather than abort — the
    downstream `_ssh_exec_retry` calls still guard the actual work.
    """
    if log:
        log.info(f"[expand] Waiting for cloud-init to finish on {host} (timeout={timeout}s)")
    try:
        # `cloud-init status --wait` prints dots while running and blocks until
        # done. We bound it with our own connect; a single long-lived SSH exec
        # is also gentler on UFW's SSH rate-limit than many short connections.
        code, out, err = _ssh_exec(
            host, username, key_path,
            f"timeout {int(timeout)} cloud-init status --wait 2>/dev/null; "
            "cloud-init status 2>/dev/null || true",
            connect_timeout=20,
        )
        status_line = (out or err or "").strip()
        if "status: done" in status_line:
            if log:
                log.info("[expand] cloud-init finished (status: done)")
        elif status_line:
            if log:
                log.warning(f"[expand] cloud-init not 'done' after wait: {status_line!r}; proceeding")
        else:
            if log:
                log.info("[expand] cloud-init status unavailable; proceeding")
    except Exception as e:
        # sshd may be mid-restart from cloud-init itself; give it a moment and
        # let the caller's _wait_for_ssh/_ssh_exec_retry recover.
        if log:
            log.warning(f"[expand] cloud-init wait could not complete ({e}); proceeding")


_DOCKER_PIN_RE = re.compile(r'DOCKER_PIN="5:(\d+\.\d+\.\d+)-')


def _expected_docker_version(cfg: dict) -> str | None:
    """Best-effort fallback: parse a hardcoded docker-ce pin literal from cloud-init.

    Resolves the configured cloud-init file via `_resolve_cloud_init_path` and
    regex-parses the `DOCKER_PIN="5:X.Y.Z-..."` pattern. This only matches when
    the file has a literal version baked in — the shipped
    `config/cloud-init/swarm-node-init-v2.yaml` instead uses the
    `__DOCKER_VERSION__` placeholder (resolved live from the manager by
    `_manager_docker_version` in `_create_droplet`, so this regex won't match
    it and this function returns `None`). Callers should prefer
    `_manager_docker_version(manager_client)` and only fall back to this
    function when the manager can't be queried live — this exists purely for
    backward compatibility with an operator-supplied cloud-init that still
    hardcodes a literal pin.

    Returns `None` — never raises — when cloud-init is unconfigured, the file
    can't be read, or the pattern isn't found. Callers treat `None` as "skip
    the version check," not as an error: this is a best-effort lookup of an
    expected value, not a required configuration input.
    """
    cip = _resolve_cloud_init_path(cfg)
    if cip is None:
        return None
    try:
        text = cip.read_text()
    except OSError:
        return None
    match = _DOCKER_PIN_RE.search(text)
    if not match:
        return None
    return match.group(1)


def _manager_docker_version(manager_client: docker.DockerClient) -> str | None:
    """Query the swarm manager's own docker-ce version, live, via the Engine API.

    Calls `manager_client.version()` (the daemon's `/version` endpoint) and
    returns its `Version` field — e.g. `"29.6.1"` — the docker-ce version of
    the daemon `manager_client` is connected to (the swarm manager). Returns
    `None` on any failure (connection error, missing/malformed response) —
    never raises.

    This is the single code path for "what docker-ce version is the manager
    running right now," shared by:
      - `_join_swarm`'s pre-join major-version preflight,
      - `_create_droplet`'s cloud-init `__DOCKER_VERSION__` substitution, and
      - `expand`'s post-join `_verify_node_provisioning` call.
    Do not reintroduce a second, drifting inline `.version()` read — callers
    that used to inline this (the join preflight) now call this instead.

    Callers treat `None` as "the manager's version could not be determined,"
    not as an error to propagate on its own: each caller decides whether
    that's fatal (`_create_droplet` fails fast when a placeholder needs
    filling) or a reason to fall back to a file-literal
    (`_expected_docker_version`).
    """
    try:
        return (manager_client.version() or {}).get("Version")
    except Exception:
        return None


def _major(v: str | None) -> int | None:
    """Extract the major-version integer from a docker version string.

    Searches for the first `X.Y.Z`-shaped dotted version number anywhere in
    the input and returns its leading integer `X`. This handles both a bare
    pinned version (`"29.6.1"` -> `29`) and free-form `docker --version`/
    `docker version --format` output (e.g. `"Docker version 29.6.0, build
    fb59821"` -> `29`), since in both cases the version number is the same
    shape — only what precedes it differs. Returns `None` on no match, a
    falsy input, or any exception — never raises.

    Shared by `_join_swarm`'s pre-join preflight and
    `_verify_node_provisioning`'s post-join docker-version check, so there
    is exactly one definition of "what is this docker version's major
    number." Do not reintroduce a second, drifting copy of this logic.
    """
    try:
        if not v:
            return None
        m = re.search(r"(\d+)\.\d+\.\d+", str(v))
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _verify_node_provisioning(ip: str, key_path: Path, *, expected_docker_version: str | None,
                              ssh_checks: int = 3, retry_delay: float = 2.0, log=None) -> list[str]:
    """Hard-fail verification that a just-joined node was actually provisioned.

    Distinct from (and run after) `_wait_for_cloud_init`'s best-effort wait:
    where that function logs-and-proceeds on an unclear outcome so a
    slow-but-fine node isn't blocked forever, this function aggregates three
    checks into a verdict the caller can act on.

    Runs three checks over SSH via the existing `_ssh_exec` helper:
      (a) `ssh_checks` consecutive SSH connect attempts (`retry_delay` seconds
          apart) — appends a failure naming how many of `ssh_checks` succeeded
          if fewer than all of them did.
      (b) `docker --version` — skipped entirely when `expected_docker_version`
          is `None`; otherwise appends a failure naming expected vs. actual
          when the node's docker major version (parsed via the shared
          `_major()` helper) doesn't match the expected major. Docker Swarm
          only requires major-version compatibility, so a patch/minor-only
          difference passes.
      (c) `cloud-init status` — appends a failure with the actual status text
          when the output doesn't contain `"status: done"`.

    Returns a list of human-readable failure strings (empty = healthy). Never
    raises for an expected failure mode (SSH down, version mismatch,
    cloud-init not done) — only a truly unexpected error (e.g. an invalid key
    file) may propagate, matching `_ssh_exec`'s own behavior.
    """
    failures: list[str] = []

    # Check (a): ssh_checks consecutive connect attempts via a trivial command.
    successes = 0
    for attempt in range(1, ssh_checks + 1):
        try:
            _ssh_exec(ip, "root", key_path, "true")
            successes += 1
        except Exception as e:
            if log:
                log.warning(f"[expand] verify: SSH attempt {attempt}/{ssh_checks} failed: {e}")
        if attempt < ssh_checks:
            time.sleep(retry_delay)
    if successes < ssh_checks:
        failures.append(
            f"SSH reachability: {successes}/{ssh_checks} consecutive connects succeeded"
        )

    # Check (b): docker --version's major matches the expected pin's major
    # (skipped when unknown). Swarm only requires major-version compatibility,
    # so a patch/minor difference is not a failure.
    if expected_docker_version is not None:
        try:
            _code, out, err = _ssh_exec(ip, "root", key_path, "docker --version")
            docker_version_output = (out or err or "").strip()
        except Exception as e:
            docker_version_output = ""
            if log:
                log.warning(f"[expand] verify: docker --version failed: {e}")
        expected_major = _major(expected_docker_version)
        actual_major = _major(docker_version_output)
        if expected_major is None or actual_major is None or expected_major != actual_major:
            failures.append(
                f"docker version mismatch: expected {expected_docker_version!r}, "
                f"got {docker_version_output!r}"
            )

    # Check (c): cloud-init reports done.
    try:
        _code, out, err = _ssh_exec(ip, "root", key_path, "cloud-init status")
        cloud_init_output = (out or err or "").strip()
    except Exception as e:
        cloud_init_output = ""
        if log:
            log.warning(f"[expand] verify: cloud-init status failed: {e}")
    if "status: done" not in cloud_init_output:
        failures.append(f"cloud-init not done: status={cloud_init_output!r}")

    return failures


def _check_docker_staleness(ip: str, key_path: Path, *, expected_docker_version: str | None,
                            log=None) -> None:
    """Purely diagnostic: warn an operator when a node's docker-ce major
    differs from the manager's, naming golden-snapshot staleness as the
    likely cause.

    This complements -- and must never replace or alter -- the hard-fail
    verdict of `_verify_node_provisioning` (sprint 009/012), which already
    blocks a real major mismatch. That function's failure message doesn't
    explain *why* a node that should have been correctly baked ended up
    wrong; this function exists solely to add that explanation as a WARNING,
    called independently, alongside the hard gate -- never feeding into its
    pass/fail verdict, failure message, or drain behavior in any way.

    Skips entirely (no-op, no log line at all, not even at debug level) when
    `expected_docker_version` is `None` -- matching
    `_verify_node_provisioning`'s own skip condition for "nothing to compare
    against."

    Otherwise runs `docker --version` over SSH (best-effort, via the shared
    `_ssh_exec`): an SSH failure here is treated as "can't compare" and is
    *not* itself escalated to WARNING, since `_verify_node_provisioning`'s own
    SSH-reachability check already surfaces node unreachability loudly.
    Computes both majors via the existing shared `_major()` helper (no
    second, divergent parsing definition) and, only when both are resolvable
    and differ, logs a WARNING naming the node's docker-ce version, the
    manager's, golden-snapshot staleness as the likely cause, and
    `scripts/build-golden-node-snapshot.sh` as the remedy.

    Never raises.
    """
    if expected_docker_version is None:
        return

    try:
        _code, out, err = _ssh_exec(ip, "root", key_path, "docker --version")
        docker_version_output = (out or err or "").strip()
    except Exception as e:
        if log:
            log.debug(f"[expand] staleness check: docker --version failed on {ip}: {e}")
        return

    expected_major = _major(expected_docker_version)
    actual_major = _major(docker_version_output)
    if expected_major is None or actual_major is None or expected_major == actual_major:
        return

    if log:
        log.warning(
            f"[expand] Node {ip} docker-ce major {actual_major} "
            f"({docker_version_output!r}) differs from manager major "
            f"{expected_major} ({expected_docker_version!r}); if this node was "
            f"provisioned from a golden snapshot, its baked docker-ce may have "
            f"drifted -- rebuild it via scripts/build-golden-node-snapshot.sh "
            f"(see docs/golden-node-snapshot.md)"
        )


def _ssh_exec(host: str, username: str, key_path: Path, cmd: str, *, connect_timeout: int = 15,
              command_timeout: float | None = None) -> tuple[int, str, str]:
    """Run `cmd` over a fresh SSH connection and return (exit_code, stdout, stderr).

    `connect_timeout` bounds only the initial connection (unchanged, existing
    behavior). `command_timeout`, when given, additionally bounds the command's
    own execution: it is applied via `.settimeout(command_timeout)` on the
    channel returned by `ssh.exec_command(cmd)`, *before* reading the exit
    status/output, so a wedged remote command (e.g. a hung `docker pull`)
    raises `socket.timeout` instead of blocking indefinitely. `command_timeout
    =None` (the default) is a complete no-op — every existing call site
    (`_verify_node_provisioning`, `_wait_for_cloud_init`, etc.) doesn't pass it
    and keeps today's behavior exactly.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = paramiko.RSAKey.from_private_key_file(str(key_path))
    try:
        ssh.connect(host, username=username, pkey=pkey, look_for_keys=False, timeout=connect_timeout)
        stdin, stdout, stderr = ssh.exec_command(cmd)
        if command_timeout is not None:
            stdout.channel.settimeout(command_timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode()
        err = stderr.read().decode()
        return exit_code, out, err
    finally:
        ssh.close()


def _ssh_exec_retry(host: str, username: str, key_path: Path, cmd: str, *, retries: int = 6, initial_delay: float = 1.5, log=None) -> tuple[int, str, str]:
    """Execute an SSH command with small retry/backoff to tolerate transient drops/rate-limits."""
    last_err: Exception | None = None
    delay = initial_delay
    for attempt in range(1, retries + 1):
        try:
            if log:
                log.info(f"[ssh] exec attempt {attempt}/{retries}: {cmd}")
            return _ssh_exec(host, username, key_path, cmd)
        except Exception as e:
            last_err = e
            if attempt >= retries:
                break
            if log:
                log.info(f"[ssh] exec failed (attempt {attempt}): {e}; retrying in {delay:.1f}s")
            time.sleep(delay)
            delay = min(delay * 1.7, 10.0)
    # If we reach here, we've exhausted retries
    raise click.ClickException(f"SSH error connecting to {host} with key {key_path} while running: {cmd}\nlast error: {last_err}")


def _get_prepull_images(cfg: dict) -> list[str]:
    """Resolve the set of code-server image URIs to pre-pull onto a new node.

    Must be called from within an active Flask app context (established by
    the caller — e.g. via `get_app(ctx)` + `with app.app_context():`, the
    same convention `rebalance()` already uses).

    Returns the UNION, order-stable and de-duplicated, of:
      - every DISTINCT `ClassProto.image_uri` in the database (always
        included), and
      - the optional `NODE_PREPULL_IMAGES` config value: a comma/whitespace
        -separated string of additional image URIs.

    `NODE_PREPULL_IMAGES` can only ADD images on top of the DB-derived list —
    it is never used to drop or replace any of them (an explicit team-lead
    decision to avoid a foot-gun where narrowing pre-pull coverage below what
    `class_proto` implies happens silently; see architecture-update.md Design
    Rationale). A DB query failure is caught, logged as a WARNING, and the
    function falls back to just the configured allowlist (or an empty list)
    — this function never raises.
    """
    db_images: list[str] = []
    try:
        from cspawn.models import ClassProto, db
        rows = db.session.query(ClassProto.image_uri).distinct().all()
        db_images = [row[0] for row in rows if row and row[0]]
    except Exception as e:
        logging.getLogger("cspawn.cli").warning(
            f"[expand] Failed to query class_proto.image_uri for pre-pull: {e}"
        )
        db_images = []

    configured_raw = cfg.get("NODE_PREPULL_IMAGES") or ""
    configured_images = configured_raw.replace(",", " ").split()

    seen: set[str] = set()
    result: list[str] = []
    for image in db_images + configured_images:
        if image and image not in seen:
            seen.add(image)
            result.append(image)
    return result


def _prepull_images(ip: str, key_path: Path, images: list[str], *, timeout: float = 300.0,
                     log=None) -> dict[str, bool]:
    """Best-effort pre-pull of `images` onto the node at `ip`, over SSH.

    For each image, runs `docker pull <image>` on the node itself via
    `_ssh_exec(..., command_timeout=timeout)` — the spawner has SSH+key
    access to nodes but ships no local `docker` CLI, so this mirrors the
    node-local `ssh <node> docker ...` pattern `CodeHostRepo.push()` already
    uses for the identical reason. Every code-server image is public on
    ghcr, so no registry auth is involved.

    Any per-image failure, non-zero exit, or timeout is caught, logged as a
    WARNING naming the image, and the loop continues to the next image —
    this never raises and never aborts the batch over one bad image. The
    caller (`expand()`/`apply_plan()`) activates the node regardless of the
    outcome here — pre-pull is best-effort, not a hard gate.

    Returns `{image: success}` so callers/tests can inspect per-image
    outcome without depending on log-scraping.
    """
    results: dict[str, bool] = {}
    for image in images:
        try:
            code, out, err = _ssh_exec(
                ip, "root", key_path, f"docker pull {image}", command_timeout=timeout,
            )
            success = code == 0
            results[image] = success
            if not success and log:
                log.warning(
                    f"[expand] docker pull {image} on {ip} exited {code}: "
                    f"{(err or out or '').strip()}"
                )
        except Exception as e:
            results[image] = False
            if log:
                log.warning(f"[expand] docker pull {image} on {ip} failed/timed out: {e}")
    return results


def _resolve_ip(hostname: str) -> str | None:
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return None


def _expand_host_with_template(name_template: str | None, host: str) -> str:
    """If host is a shortname and DO_NAMES has a domain suffix, append it."""
    if not host or "." in host:
        return host
    if not name_template or "." not in name_template:
        return host
    domain_suffix = name_template.split(".", 1)[1]
    return f"{host}.{domain_suffix}"


def _looks_like_ip(value: str) -> bool:
    try:
        socket.inet_aton(value)
        return True
    except Exception:
        return False


def _resolve_target_ip_via_do(token: str | None, target: str, name_template: str | None, do_tag: str | None, do_project: str | None, log=None) -> str | None:
    """Resolve an IP for a target spec using DigitalOcean droplet data when DNS fails.

    Tries exact name match against candidates [target, short, short+domain]. Returns public IPv4.
    """
    if not token:
        return None
    try:
        mgr = digitalocean.Manager(token=token)
    except Exception as e:
        if log:
            log.warning(f"[ssh] Could not init DO manager: {e}")
        return None

    short = (target.split(".")[0] if target else target) or ""
    # derive fqdn from template
    fqdn = _expand_host_with_template(name_template, short) if short else None
    # Prefer FQDN first, then short, then literal target
    candidates = [c for c in [fqdn, short, target] if c]

    droplets = []
    try:
        if do_tag:
            droplets = mgr.get_all_droplets(tag_name=do_tag)
        if not droplets:
            droplets = mgr.get_all_droplets()
    except Exception as e:
        if log:
            log.warning(f"[ssh] Failed to list droplets: {e}")
        return None

    # Constrain by project when provided
    target_proj_id = None
    if do_project:
        target_proj_id = _resolve_project_id_by_name_or_id(token, do_project, log=log)
    if target_proj_id:
        proj_map = _map_droplet_to_project_ids(token, log=log)
        droplets = [d for d in droplets if str(getattr(d, "id", "")) in proj_map and proj_map[str(getattr(d, "id", ""))] == target_proj_id]

    # Prefer active droplets and exact name match by candidate priority
    for cand in candidates:
        matches = [d for d in droplets if getattr(d, "name", None) == cand]
        if not matches:
            continue
        # prefer active
        actives = [d for d in matches if getattr(d, "status", None) == "active"] or matches
        # pick the one with max id as a tie-breaker
        pick = sorted(actives, key=lambda d: getattr(d, "id", 0), reverse=True)[0]
        nets = getattr(pick, "networks", {}) or {}
        for v4 in nets.get("v4", []):
            if v4.get("type") == "public":
                return v4.get("ip_address")
    return None


def _resolve_droplet_by_spec(
    *,
    mgr: digitalocean.Manager,
    token: str,
    do_names: str,
    do_tag: str | None,
    do_project: str | None,
    spec: str,
    log=None,
) -> tuple[digitalocean.Droplet, str]:
    """Resolve a droplet and its FQDN from a node spec.

    - If spec is digits only: treat as serial. Compute fqdn from DO_NAMES.
      Enforce that droplet has DO_TAG and belongs to DO_PROJECT.
    - If spec contains a dot: treat as FQDN.
    - Otherwise: treat as shortname; try exact match, then short+domain from DO_NAMES.
    Returns (droplet_obj, fqdn). Raises ClickException if not found or constraints fail.
    """
    # Build helpers from template
    template = do_names
    domain_suffix = template.split(".", 1)[1] if "." in template else None
    short_prefix = template.split(".")[0].split("{serial}")[0]

    droplets = []
    try:
        droplets = mgr.get_all_droplets()
    except Exception as e:
        raise click.ClickException(f"Failed to list droplets: {e}")

    def _find_by_name(candidate: str) -> digitalocean.Droplet | None:
        for d in droplets:
            if getattr(d, "name", None) == candidate:
                return d
        return None

    is_numeric = spec.isdigit()
    is_fqdn = "." in spec and not is_numeric
    fqdn = None
    target = None

    if is_numeric:
        if not do_tag or not do_project:
            raise click.ClickException("Numeric spec requires DO_TAG and DO_PROJECT configured")
        serial = int(spec)
        fqdn = template.format(serial=serial)
        short = f"{short_prefix}{serial}"
        # prefer fqdn match
        target = _find_by_name(fqdn) or _find_by_name(short)
        if not target:
            raise click.ClickException(f"No droplet found for serial {serial} (fqdn={fqdn})")
        # Validate tag
        tags = getattr(target, "tags", []) or []
        if do_tag not in tags:
            raise click.ClickException(f"Droplet {target.name} does not have required tag {do_tag}")
        # Validate project membership
        resolved_proj = _resolve_project_id_by_name_or_id(token, do_project, log=log)
        pid = _find_project_id_for_droplet(token, target.id, log=log)
        if not resolved_proj or pid != resolved_proj:
            raise click.ClickException(
                f"Droplet {target.name} not in required project {do_project}"
            )
    elif is_fqdn:
        fqdn = spec.strip()
        target = _find_by_name(fqdn)
        if not target and domain_suffix:
            # If name in DO might be stored as short only
            short = fqdn.split(".")[0]
            target = _find_by_name(short)
        if not target:
            raise click.ClickException(f"No droplet found matching {fqdn}")
    else:
        short = spec.strip()
        target = _find_by_name(short)
        if not target and domain_suffix:
            fqdn = f"{short}.{domain_suffix}"
            target = _find_by_name(fqdn)
        if not target:
            raise click.ClickException(f"No droplet found matching {short}")
        fqdn = fqdn or (target.name if "." in target.name else (f"{target.name}.{domain_suffix}" if domain_suffix else target.name))

    fqdn = fqdn or target.name
    return target, fqdn


def _find_manager_droplet(mgr: digitalocean.Manager, manager_host: str, do_tag: str | None = None):
    """Locate the droplet object matching the swarm manager hostname."""
    try:
        ip = _resolve_ip(manager_host)
        short = manager_host.split(".")[0] if manager_host else manager_host
        # Prefer filtering by tag when provided
        droplets = []
        try:
            if do_tag:
                droplets = mgr.get_all_droplets(tag_name=do_tag)
        except Exception:
            droplets = []
        if not droplets:
            droplets = mgr.get_all_droplets()
        for d in droplets:
            # If do_tag is provided, skip droplets without it
            if do_tag and (not getattr(d, "tags", None) or do_tag not in getattr(d, "tags", [])):
                continue
            # Exact FQDN match
            if d.name == manager_host:
                return d
            # Shortname match (common DO naming)
            if short and d.name == short:
                return d
            # Prefix match as a last resort (avoid false positives if multiple similar names)
            if short and d.name.startswith(short):
                candidate = d
                # Keep checking IP below; if IP matches, return immediately
            else:
                candidate = None
            # Fallback: match on public IPv4
            if ip:
                nets = getattr(d, "networks", {}) or {}
                for v4 in nets.get("v4", []):
                    if v4.get("type") == "public" and v4.get("ip_address") == ip:
                        return d or candidate
    except Exception:
        pass
    return None


def _find_swarm_node(manager_client: docker.DockerClient, fqdn: str, short: str | None = None):
    """Return the swarm Node object matching the fqdn or short name, or None."""
    try:
        for n in manager_client.nodes.list():
            name = n.attrs.get("Description", {}).get("Hostname", "")
            if name == fqdn:
                return n
            if short and name.split(".")[0] == short:
                return n
    except Exception:
        pass
    return None


def _wait_node_tasks_drained(client: docker.DockerClient, node_id: str, timeout: int = 600, log=None) -> None:
    """Poll tasks on the given node until no active tasks remain or timeout."""
    start = time.time()
    delay = 2.0
    active_states = {"running", "accepted", "assigned", "preparing", "starting", "pending"}
    while time.time() - start < timeout:
        try:
            # Docker SDK 2.0 moved tasks to the low-level API
            active = []
            tasks_list = []
            try:
                tasks_list = client.api.tasks(filters={"node": node_id})  # type: ignore[attr-defined]
            except Exception as e:
                if log:
                    log.warning(f"[stop] Failed to list tasks for node {node_id} via API: {e}")
                tasks_list = []
            for t in tasks_list or []:
                try:
                    st = ((t or {}).get("Status", {}) or {}).get("State", "")
                    if st in active_states:
                        active.append(t)
                except Exception:
                    pass
            if not active:
                if log:
                    log.info("[stop] Node tasks drained")
                return
            if log:
                log.info(f"[stop] Waiting for tasks to drain: remaining={len(active)}")
        except Exception as e:
            if log:
                log.warning(f"[stop] Failed to list tasks for node {node_id}: {e}")
        time.sleep(delay)
        delay = min(delay * 1.5, 15.0)
    raise click.ClickException("Timed out waiting for node tasks to drain")


def _drain_swarm_node(manager_client: docker.DockerClient, node_obj, log=None) -> None:
    """Set node availability to drain (idempotent), compatible with Docker SDK 2.0."""
    try:
        spec = node_obj.attrs.get("Spec", {}) or {}
        availability = (spec.get("Availability") or "").lower()
        if availability == "drain":
            if log:
                log.info("[stop] Node already in drain mode")
            return
        # Try high-level update first (older SDKs)
        try:
            node_obj.update(availability="drain")
            if log:
                log.info("[stop] Node set to drain (high-level)")
            return
        except TypeError:
            # Try with capitalized key (some SDK variants)
            try:
                node_obj.update(Availability="drain")  # type: ignore[arg-type]
                if log:
                    log.info("[stop] Node set to drain (high-level, capitalized)")
                return
            except Exception:
                pass
        # Fallback: low-level API
        info = manager_client.api.inspect_node(node_obj.id)  # type: ignore[attr-defined]
        version = ((info or {}).get("Version", {}) or {}).get("Index")
        node_spec = ((info or {}).get("Spec", {}) or {}).copy()
        node_spec["Availability"] = "drain"
        manager_client.api.update_node(node_obj.id, version, node_spec)  # type: ignore[attr-defined]
        if log:
            log.info("[stop] Node set to drain (low-level)")
    except Exception as e:
        if log:
            log.warning(f"[stop] Failed to drain node: {e}")


def _activate_swarm_node(manager_client: docker.DockerClient, node_obj, *, retries: int = 3,
                          initial_delay: float = 2.0, log=None) -> bool:
    """Set node availability to active (idempotent), retrying with backoff.

    Structurally mirrors `_drain_swarm_node`'s idempotent update chain
    (high-level `.update(availability="active")` -> capitalized-kwarg
    fallback -> low-level `manager_client.api.update_node(...)` fallback,
    compatible with Docker SDK 2.0) — but wraps that whole attempt in a
    bounded retry-with-backoff loop, matching the shape of the existing
    `_ssh_exec_retry` helper's retry/backoff pattern.

    Unlike `_drain_swarm_node`'s single-attempt, log-and-swallow best-effort
    posture, a node that gets warmed but never reactivates is
    silently-wasted capacity — invisible to the scheduler, hiding in plain
    sight. So on final failure (all `retries` attempts exhausted), this
    function logs at **ERROR** (not WARNING), naming the node and the
    manual remedy an operator needs: `docker node update --availability
    active <node>`.

    Returns `True` on confirmed success, `False` once all attempts are
    exhausted. Never raises.
    """
    node_name = "?"
    try:
        node_name = node_obj.attrs.get("Description", {}).get("Hostname") or node_obj.id
    except Exception:
        try:
            node_name = node_obj.id
        except Exception:
            pass

    def _attempt_once() -> bool:
        nonlocal node_obj
        # Reload the node from the manager before reading .attrs or calling
        # .update(). The expand flow's `node_obj` here is commonly a
        # snapshot fetched *before* this same node was drained (and
        # pre-pulled) earlier in that flow -- trusting its cached
        # `.attrs["Spec"]["Availability"]` would make the idempotency check
        # below see the pre-drain "active" value and silently short-circuit,
        # leaving the node drained forever (confirmed live, 2026-07-06/07:
        # an expanded node stayed Drain until a manual `docker node update
        # --availability active` fixed it). The cached `.attrs["Version"]`
        # index used by any high-level `.update()` call that succeeds would
        # be stale for the same reason. Prefer `Node.reload()`; if that
        # itself isn't reliable, re-fetch the node by id from the manager.
        try:
            node_obj.reload()
        except Exception as reload_err:
            try:
                node_obj = manager_client.nodes.get(node_obj.id)
            except Exception as refetch_err:
                if log:
                    log.warning(
                        f"[expand] Could not reload node {node_name} before "
                        f"activation check ({reload_err}; re-fetch also "
                        f"failed: {refetch_err}) -- proceeding with "
                        f"possibly-stale node state"
                    )

        spec = node_obj.attrs.get("Spec", {}) or {}
        availability = (spec.get("Availability") or "").lower()
        if availability == "active":
            if log:
                log.info(f"[expand] Node {node_name} already active")
            return True
        # Try high-level update first (older SDKs)
        try:
            node_obj.update(availability="active")
            if log:
                log.info(f"[expand] Node {node_name} set to active (high-level)")
            return True
        except TypeError:
            # Try with capitalized key (some SDK variants)
            try:
                node_obj.update(Availability="active")  # type: ignore[arg-type]
                if log:
                    log.info(f"[expand] Node {node_name} set to active (high-level, capitalized)")
                return True
            except Exception:
                pass
        # Fallback: low-level API
        info = manager_client.api.inspect_node(node_obj.id)  # type: ignore[attr-defined]
        version = ((info or {}).get("Version", {}) or {}).get("Index")
        node_spec = ((info or {}).get("Spec", {}) or {}).copy()
        node_spec["Availability"] = "active"
        manager_client.api.update_node(node_obj.id, version, node_spec)  # type: ignore[attr-defined]
        if log:
            log.info(f"[expand] Node {node_name} set to active (low-level)")
        return True

    delay = initial_delay
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return _attempt_once()
        except Exception as e:
            last_err = e
            if attempt < retries:
                if log:
                    log.warning(
                        f"[expand] Activate attempt {attempt}/{retries} failed for node "
                        f"{node_name}: {e}; retrying in {delay:.1f}s"
                    )
                time.sleep(delay)
                delay = min(delay * 1.7, 10.0)

    if log:
        log.error(
            f"[expand] Node {node_name} failed to reactivate after {retries} attempts "
            f"({last_err}) -- this node was warmed but remains drained and invisible to "
            f"the scheduler (silently-wasted capacity). Manual remedy: "
            f"docker node update --availability active {node_name}"
        )
    return False


def _ensure_label_on_node(manager_client: docker.DockerClient, node_name: str, label_key: str, log=None) -> bool:
    """Ensure the swarm node has Labels[label_key] == 'true'. Returns True if changed/applied.

    Uses low-level API for Docker SDK 2.0 compatibility. Idempotent.
    """
    try:
        short = node_name.split(".")[0] if node_name else node_name
        node_obj = _find_swarm_node(manager_client, node_name, short)
        if not node_obj:
            return False
        info = manager_client.api.inspect_node(node_obj.id)  # type: ignore[attr-defined]
        version = ((info or {}).get("Version", {}) or {}).get("Index")
        spec = ((info or {}).get("Spec", {}) or {}).copy()
        labels = (spec.get("Labels") or {}).copy()
        if labels.get(label_key) == "true":
            return False
        labels[label_key] = "true"
        spec["Labels"] = labels
        manager_client.api.update_node(node_obj.id, version, spec)  # type: ignore[attr-defined]
        if log:
            log.info(f"[expand] Applied node label '{label_key}=true' on {node_name}")
        return True
    except Exception as e:
        if log:
            log.warning(f"[expand] Failed to apply node label '{label_key}' on {node_name}: {e}")
        return False


def _ensure_node_labels(
    manager_client: docker.DockerClient,
    node_name: str,
    labels: dict[str, str],
    log=None,
) -> bool:
    """Merge key=value labels into the swarm node's Spec.Labels. Idempotent.

    Returns True if any label was changed, False if all were already present
    with matching values. Skips gracefully on errors (logs warning).

    Uses the same low-level API as _ensure_label_on_node for SDK 2.0 compat.
    """
    if not labels:
        return False
    try:
        short = node_name.split(".")[0] if node_name else node_name
        node_obj = _find_swarm_node(manager_client, node_name, short)
        if not node_obj:
            return False
        info = manager_client.api.inspect_node(node_obj.id)  # type: ignore[attr-defined]
        version = ((info or {}).get("Version", {}) or {}).get("Index")
        spec = ((info or {}).get("Spec", {}) or {}).copy()
        existing = (spec.get("Labels") or {}).copy()
        # Check which labels actually need updating
        to_set = {k: v for k, v in labels.items() if existing.get(k) != v}
        if not to_set:
            return False  # all already set correctly
        existing.update(to_set)
        spec["Labels"] = existing
        manager_client.api.update_node(node_obj.id, version, spec)  # type: ignore[attr-defined]
        if log:
            for k, v in to_set.items():
                log.info(f"[expand] Applied node label '{k}={v}' on {node_name}")
        return True
    except Exception as e:
        if log:
            log.warning(f"[expand] Failed to apply node labels on {node_name}: {e}")
        return False


def _apply_labels_after_join(
    manager_client: docker.DockerClient,
    target: str,
    labels: dict[str, str],
    *,
    deadline_seconds: float = 90.0,
    poll_interval: float = 3.0,
    log=None,
) -> bool:
    """Poll for the just-joined swarm node and apply `labels`, matching by hostname.

    Locates the node via `_find_swarm_node(manager_client, target, short)` —
    hostname/short-name matching, never by comparing `Status.Addr` to an IP.
    Nodes join with `--advertise-addr <VPC-ip>`, so `Status.Addr` is always
    the private VPC address and can never match a droplet's public IP; that
    mismatch is what silently dropped `cs.tier`/`cs.capacity` on every
    VPC-advertised expand before this helper existed.

    Returns True if labels were applied via `_ensure_node_labels` once the
    node was found, False if the deadline passed with no match (logging a
    WARNING naming `target` and the label keys that were not applied) or if
    `labels` is empty. Never raises.
    """
    if not labels:
        return False
    short = target.split(".")[0] if target else target
    deadline = time.time() + deadline_seconds
    while True:
        try:
            node_obj = _find_swarm_node(manager_client, target, short)
        except Exception:
            node_obj = None
        if node_obj:
            name = node_obj.attrs.get("Description", {}).get("Hostname") or target
            return _ensure_node_labels(manager_client, name, labels, log=log)
        if time.time() >= deadline:
            break
        time.sleep(poll_interval)
    if log:
        log.warning(
            f"[expand] Timed out waiting for node '{target}' to appear in swarm; "
            f"labels not applied: {sorted(labels.keys())}"
        )
    return False


def _find_project_id_for_droplet(token: str, droplet_id: int, log=None) -> str | None:
    """Find the Project ID that contains the given droplet using python-digitalocean."""
    urn = f"do:droplet:{droplet_id}"
    try:
        mgr = digitalocean.Manager(token=token)
        for p in mgr.get_all_projects():
            proj = digitalocean.Project(token=token, id=p.id)
            try:
                resources = proj.get_all_resources()  # returns list of URN strings
            except Exception:
                resources = []
            for res in resources:
                if isinstance(res, str) and res == urn:
                    return p.id
    except Exception as e:
        if log:
            log.warning(f"[expand] Project lookup failed: {e}")
    return None


def _assign_droplet_to_project(token: str, project_id: str, droplet_id: int, log=None) -> bool:
    try:
        proj = digitalocean.Project(token=token, id=project_id)
        # python-digitalocean API uses singular: assign_resource([...])
        proj.assign_resource([f"do:droplet:{droplet_id}"])
        return True
    except Exception as e:
        if log:
            log.warning(f"[expand] Assign via library failed: {e}")
        return False


def _resolve_project_id_by_name_or_id(token: str, selector: str, log=None) -> str | None:
    """Resolve a DO project ID from a name or return the ID if provided using the library."""
    if not selector:
        return None
    # If looks like a UUID, accept as-is
    if len(selector) in (32, 36):
        return selector
    try:
        mgr = digitalocean.Manager(token=token)
        projects = mgr.get_all_projects()
        for p in projects:
            if getattr(p, "name", "") == selector or getattr(p, "name", "").lower() == selector.lower():
                return getattr(p, "id", None)
        for p in projects:
            if selector.lower() in getattr(p, "name", "").lower():
                if log:
                    log.info(f"[expand] Using partial project match: {getattr(p,'name', '')} ({getattr(p,'id', '')})")
                return getattr(p, "id", None)
    except Exception as e:
        if log:
            log.warning(f"[expand] Project resolve failed for '{selector}': {e}")
    return None


def _get_project_name(token: str, project_id: str | None, log=None) -> str | None:
    if not project_id:
        return None
    try:
        proj = digitalocean.Project(token=token, id=project_id)
        try:
            proj.load()
            return getattr(proj, "name", None)
        except Exception:
            pass
        mgr = digitalocean.Manager(token=token)
        for p in mgr.get_all_projects():
            if getattr(p, "id", None) == project_id:
                return getattr(p, "name", None)
    except Exception as e:
        if log:
            log.warning(f"[info] Failed to get project name for {project_id}: {e}")
    return None


def _map_droplet_to_project_ids(token: str, log=None) -> dict[str, str]:
    """Return mapping of droplet_id (str) -> project_id using python-digitalocean."""
    mapping: dict[str, str] = {}
    try:
        mgr = digitalocean.Manager(token=token)
        for p in mgr.get_all_projects():
            proj = digitalocean.Project(token=token, id=p.id)
            try:
                resources = proj.get_all_resources()  # list of URN strings
            except Exception:
                resources = []
            for urn in resources:
                if isinstance(urn, str) and urn.startswith("do:droplet:"):
                    did = urn.split(":")[-1]
                    mapping[did] = p.id
    except Exception as e:
        if log:
            log.warning(f"[info] Failed to build droplet->project map: {e}")
    return mapping


def _ensure_tag_on_droplet(token: str, droplet_id: int, tag: str, log=None) -> None:
    """Ensure a DO tag exists and is attached to the droplet using the library."""
    try:
        t = digitalocean.Tag(token=token, name=tag)
        try:
            t.create()
        except Exception:
            pass
        # Official helper uses add_droplets
        t.add_droplets([droplet_id])
    except Exception as e:
        if log:
            log.warning(f"[expand] Failed to attach tag '{tag}' to droplet {droplet_id}: {e}")


def _ensure_priv_key() -> tuple[Path, Path]:
    """Return (private_key_path, public_key_path) and ensure private exists.

    Primary location: ``<workspace_root>/config/secrets/id_rsa`` (used in local-prod).
    Fallback location: ``~/.ssh/id_rsa`` (used in the deployed prod container where
    ``config/secrets/`` is empty but the swarm key lives in the root home directory).
    The public key path is derived from the private key path; callers must check
    ``pub_key_path.exists()`` before reading it (the fallback location may lack a
    ``.pub`` counterpart).
    """
    workspace_root = find_parent_dir()
    primary_priv = Path(workspace_root) / "config" / "secrets" / "id_rsa"
    fallback_priv = Path.home() / ".ssh" / "id_rsa"

    if primary_priv.exists():
        priv_key_path = primary_priv
    elif fallback_priv.exists():
        priv_key_path = fallback_priv
    else:
        raise click.ClickException(
            f"SSH private key not found at {primary_priv} or {fallback_priv}"
        )

    pub_key_path = priv_key_path.with_suffix(".pub")
    return priv_key_path, pub_key_path


def _collect_do_ssh_keys(mgr: digitalocean.Manager, do_token: str, pub_key_path: Path | None, shortname: str, log):
    """Return list of DO SSHKey objects; upload local pub key if missing."""
    ssh_keys_param = []
    pub_key_text = None
    fingerprint = None
    try:
        account_keys = mgr.get_all_sshkeys()
        if pub_key_path and pub_key_path.exists():
            pub_key_text = pub_key_path.read_text()
            fingerprint = _compute_fingerprint(pub_key_text)
            if fingerprint and not any(getattr(k, "fingerprint", None) == fingerprint for k in account_keys):
                try:
                    key_name = f"cspawn-{shortname}-{int(time.time())}"
                    log.info(f"[expand] Uploading local SSH key to DO as '{key_name}'")
                    new_key = digitalocean.SSHKey(token=do_token, name=key_name, public_key=pub_key_text)
                    new_key.create()
                    account_keys = mgr.get_all_sshkeys()
                except Exception as e:
                    log.warning(f"[expand] Failed to upload local SSH key: {e}")
        ssh_keys_param = account_keys
        log.info(f"[expand] Injecting DO SSH keys: count={len(ssh_keys_param)}")
    except Exception as e:
        log.warning(f"Failed to resolve DO SSH keys: {e}. Proceeding without explicit keys.")
    return ssh_keys_param


def _resolve_cloud_init_path(cfg: dict) -> Path | None:
    """Resolve the configured cloud-init file to a path, without checking existence.

    Returns ``None`` when neither ``DO_CLOUD_INIT`` nor ``DO_CLOUD_INIT_FILE`` is set
    in ``cfg`` — an explicit operator opt-out to proceed without cloud-init. Otherwise
    returns ``<project-root>/config/cloud-init/<configured-file>``, where
    ``<project-root>`` is ``find_parent_dir()``.

    Does not check whether the resolved path exists or is readable — that's each
    caller's job, since "missing" means different things in different contexts
    (e.g. `_create_droplet` treats it as a hard failure; `_expected_docker_version`
    treats it as "skip the version check").
    """
    cloud_init_file = cfg.get("DO_CLOUD_INIT") or cfg.get("DO_CLOUD_INIT_FILE")
    if not cloud_init_file:
        return None
    return Path(find_parent_dir()) / "config" / "cloud-init" / cloud_init_file


def _create_droplet(ctx, *, mgr: digitalocean.Manager, manager_client: docker.DockerClient, name_template: str,
                    do_token: str, do_region: str, do_size: str, do_image: str, project_selector: str | None,
                    desired_serial: int | None, docker_uri: str, do_tag: str | None = None,
                    tier: "Tier | None" = None, node_op_id: str | None = None) -> tuple[digitalocean.Droplet, str, str, str]:
    """Create droplet for next or specific serial. Idempotent if desired_serial provided.

    When ``tier`` is provided it takes precedence over ``do_size`` for the droplet slug.

    Cloud-init user-data is resolved via `_resolve_cloud_init_path` *before* any
    DigitalOcean side effect (SSH-key upload, `droplet.create()`): if
    ``DO_CLOUD_INIT``/``DO_CLOUD_INIT_FILE`` is configured but the resolved file is
    missing or unreadable, this raises `click.ClickException` and creates nothing.
    If unset, proceeds with ``user_data=None`` (unchanged, explicit opt-out).

    If the resolved cloud-init text contains the ``__DOCKER_VERSION__``
    placeholder, it is substituted here — also before any DigitalOcean side
    effect — with the swarm manager's live docker-ce version from
    `_manager_docker_version(manager_client)`. If that placeholder is present
    but the manager's version cannot be determined, this raises
    `click.ClickException` and creates nothing: provisioning a node without a
    guaranteed-matching docker-ce pin defeats the point of pinning at all.
    Cloud-init with no placeholder (e.g. an operator-hardcoded literal pin) is
    left untouched, for backward compatibility.

    When ``node_op_id`` is provided (the admin-triggered `op-run` path passes
    the triggering `NodeOp`'s id), the created droplet's id/fqdn are recorded
    on that `NodeOp` row as a best-effort write once creation succeeds — so
    that if the container dies before the node joins the swarm, the op names
    the droplet an operator should check for orphaning. This requires an
    active Flask app context (provided by the caller). The write never
    raises: any failure (missing row, DB error) is logged as a warning and
    node creation proceeds unaffected. ``node_op_id`` defaults to ``None``,
    which is a complete no-op — every existing caller (bare CLI `expand`,
    the autoscaler's `apply_plan`) is unchanged.

    Returns (droplet, ip, fqdn, shortname)
    """
    config = get_config()
    log = get_logger(ctx)

    # Determine fqdn/shortname based on desired or next serial
    if desired_serial is not None:
        fqdn = name_template.format(serial=desired_serial)
        shortname = fqdn.split(".")[0]
        log.info(f"[expand] Requested create for serial={desired_serial} -> fqdn={fqdn}")
    else:
        log.info(f"[expand] Inspecting current swarm nodes for prefix derived from {name_template}")
        next_serial, fqdn_template, _ = _get_next_serial(manager_client, name_template)
        fqdn = fqdn_template.format(serial=next_serial)
        shortname = fqdn.split(".")[0]
        log.info(f"[expand] Computed next serial={next_serial}, fqdn={fqdn}")

    # Idempotency for specific serial: reuse existing droplet if present
    existing = None
    try:
        # Prefer tag-filtered list if tag provided
        droplets = []
        try:
            if do_tag:
                droplets = mgr.get_all_droplets(tag_name=do_tag)
        except Exception:
            droplets = []
        if not droplets:
            droplets = mgr.get_all_droplets()
        for d in droplets:
            if d.name == fqdn or d.name == shortname:
                existing = d
                break
    except Exception:
        pass

    if existing is not None:
        log.info(f"[expand] Droplet already exists for {fqdn} (id={existing.id}); reusing")
        droplet = existing
    else:
        # Resolve cloud-init user-data BEFORE any DigitalOcean side effect
        # (SSH-key upload, droplet.create()): a misconfigured DO_CLOUD_INIT must
        # fail with zero side effects, not just before droplet.create(). Unset
        # is an explicit operator opt-out and proceeds with user_data=None.
        user_data = None
        cip = _resolve_cloud_init_path(config)
        if cip is None:
            log.info("[expand] No CLOUD_INIT_FILE configured; proceeding without user-data")
        else:
            try:
                user_data = cip.read_text()
            except OSError as e:
                raise click.ClickException(
                    f"[expand] Configured cloud-init file at {cip} could not be read: {e}. "
                    "Fix the file/permissions, or unset DO_CLOUD_INIT/DO_CLOUD_INIT_FILE to "
                    "proceed without cloud-init (not recommended)."
                )
            log.info(f"[expand] Including cloud-init user-data from {cip}")

            # Cloud-init may contain the __DOCKER_VERSION__ placeholder (see
            # config/cloud-init/swarm-node-init-v2.yaml) in place of a
            # hand-maintained docker-ce version literal. Resolve it here —
            # before user-data is baked into the droplet — against the
            # swarm manager's *live* docker-ce version, so new nodes always
            # pin to whatever the manager is actually running (surviving
            # future manager upgrades, including major bumps) without
            # anyone needing to edit the cloud-init file. If the manager's
            # version can't be determined, fail fast and create no droplet
            # rather than provision a node with an unresolved/broken pin.
            if "__DOCKER_VERSION__" in user_data:
                mgr_docker_version = _manager_docker_version(manager_client)
                if not mgr_docker_version:
                    raise click.ClickException(
                        f"[expand] Cloud-init at {cip} requires the swarm manager's docker-ce "
                        "version (placeholder __DOCKER_VERSION__) but it could not be determined "
                        f"from the manager at docker_uri={docker_uri!r}. Refusing to provision a "
                        "node without a guaranteed matching docker-ce pin -- check manager "
                        "connectivity and retry."
                    )
                user_data = user_data.replace("__DOCKER_VERSION__", mgr_docker_version)
                log.info(f"[expand] Resolved manager docker-ce version {mgr_docker_version} into cloud-init DOCKER_PIN")

        # Prepare keys
        priv_key_path, pub_key_path = _ensure_priv_key()
        ssh_keys_param = _collect_do_ssh_keys(mgr, do_token, pub_key_path, shortname, log)

        # Create droplet — tier.slug takes precedence over do_size when provided
        effective_size = tier.slug if tier is not None else do_size
        # A numeric DO_IMAGE is a snapshot/custom-image ID and must be sent to the
        # DO API as an integer; a slug (e.g. "docker-20-04") stays a string. The
        # env-file stores everything as strings, so coerce an all-digits value.
        image_arg = int(do_image) if str(do_image).isdigit() else do_image
        droplet = digitalocean.Droplet(
            token=do_token,
            name=fqdn,
            region=do_region,
            image=image_arg,
            size_slug=effective_size,
            ssh_keys=list(ssh_keys_param or []),
            backups=False,
            ipv6=False,
            tags=[do_tag] if do_tag else None,
            user_data=user_data,
        )
        log.info(f"[expand] Creating droplet {fqdn} in {do_region} with size {effective_size} and image {image_arg}")
        try:
            droplet.create()
        except Exception as e:
            log.error(f"[expand] Droplet creation failed for do token {do_token}: \n{e}")
            emsg = str(e).lower()
            if "not authorized" in emsg or "forbidden" in emsg:
                raise click.ClickException(
                    "DigitalOcean token is valid but lacks droplet create permission. "
                    "Grant at least Droplets: Read+Write (and for full node expand flow also Tags: Read+Write, "
                    "Projects: Read+Write, SSH Keys: Read+Write, Domains: Read+Write)."
                )
            raise

        try:
            droplet.load()
            log.info(f"[expand] Droplet created with id={getattr(droplet, 'id', None)} status={getattr(droplet, 'status', None)}")
        except Exception as e:
            log.error(f"[expand] Droplet creation initiated but failed to load droplet info: {e}")

    # Wait active and get IP
    ip = _wait_for_droplet_active(mgr, droplet, log=log)
    log.info(f"[expand] Droplet {fqdn} active at {ip}")

    # Ensure tag attached (for existing droplets too)
    if do_tag:
        _ensure_tag_on_droplet(do_token, droplet.id, do_tag, log=log)

    # Ensure desired project placement
    docker_uri_host = urlparse(docker_uri).hostname or ""
    try:
        target_project_id = None
        if project_selector:
            target_project_id = _resolve_project_id_by_name_or_id(do_token, project_selector, log=log)
            if target_project_id:
                log.info(f"[expand] Using requested project '{project_selector}' -> id={target_project_id}")
            else:
                log.warning(f"[expand] Could not resolve requested project '{project_selector}'")
        if not target_project_id:
            manager_droplet = _find_manager_droplet(mgr, docker_uri_host, do_tag)
            if manager_droplet:
                log.info(f"[expand] Found manager droplet id={manager_droplet.id} for host {docker_uri_host}")
                target_project_id = _find_project_id_for_droplet(do_token, manager_droplet.id, log=log)
            else:
                log.warning(f"[expand] Could not locate manager droplet for host {docker_uri_host}; skipping project match")
        if target_project_id:
            log.info(f"[expand] Assigning droplet to project id={target_project_id}")
            if not _assign_droplet_to_project(do_token, target_project_id, droplet.id, log=log):
                log.warning("[expand] Project assignment API call did not confirm success")
    except Exception as e:
        log.warning(f"[expand] Error during project assignment: {e}")

    # Best-effort: record the created droplet against the triggering NodeOp row
    # (admin-triggered `op-run` -> `expand(node_op_id=...)` path only). Never
    # allowed to fail node creation — a DB hiccup here just means an
    # interrupted op's message can't name the orphaned droplet, not that
    # creation itself failed. No-op when node_op_id is None (every other
    # caller: bare CLI `expand`, the autoscaler's `apply_plan`).
    if node_op_id is not None:
        try:
            from cspawn.models import NodeOp, db
            op = db.session.get(NodeOp, node_op_id)
            if op is not None:
                op.droplet_id = droplet.id
                op.target_fqdn = fqdn
                db.session.commit()
            else:
                log.warning(f"[expand] NodeOp {node_op_id!r} not found; could not record droplet {fqdn} (id={getattr(droplet, 'id', None)})")
        except Exception as e:
            log.warning(f"[expand] Could not record droplet {fqdn} (id={getattr(droplet, 'id', None)}) on NodeOp {node_op_id!r}: {e}")

    return droplet, ip, fqdn, shortname


def _configure_node(ctx, target: str, desired_shortname: str | None = None, ssh_timeout: int | None = None) -> tuple[str, str]:
    """Configure hostname on the node. Idempotent. Returns (ip, shortname)."""
    log = get_logger(ctx)
    priv_key_path, _ = _ensure_priv_key()
    # Resolve IP and shortname
    cfg = get_config()
    # Allow overriding SSH wait via config (seconds)
    # Determine SSH wait timeout precedence (param > config > default)
    if ssh_timeout is None:
        try:
            ssh_timeout = int(cfg.get("SSH_WAIT_TIMEOUT", 0) or 0)
        except Exception:
            ssh_timeout = 0
    do_token = cfg.get("DO_TOKEN")
    do_tag = cfg.get("DO_TAG")
    do_project = cfg.get("DO_PROJECT")
    name_template = cfg.get("DO_NAMES")
    # Prefer literal IP; otherwise DigitalOcean IP; then DNS/FQDN
    ip = target if _looks_like_ip(target) else (
        _resolve_target_ip_via_do(do_token, target, name_template, do_tag, do_project, log=log)
        or _resolve_ip(target)
        or _resolve_ip(_expand_host_with_template(name_template, target))
        or target
    )
    shortname = desired_shortname or target.split(".")[0]
    _wait_for_ssh(ip, log=log, key_path=priv_key_path, timeout=ssh_timeout or 900)
    # cloud-init is still installing docker/pinning versions/reconfiguring UFW and
    # will restart sshd at the end. Wait for it to finish before any introspection,
    # then re-confirm SSH (the sshd restart drops the early connection).
    _wait_for_cloud_init(ip, priv_key_path, log=log)
    _wait_for_ssh(ip, log=log, key_path=priv_key_path, timeout=ssh_timeout or 900)
    # brief pause to avoid UFW rate-limit on immediate reconnect
    time.sleep(1.0)
    # Check current hostname
    code, out, err = _ssh_exec_retry(ip, "root", priv_key_path, "hostnamectl --static", retries=8, initial_delay=1.5, log=log)
    current = (out or err or "").strip()
    if current == shortname:
        log.info(f"[expand] Hostname already '{shortname}', skipping")
        return ip, shortname
    log.info(f"[expand] Setting droplet hostname to {shortname}")
    code, out, err = _ssh_exec_retry(ip, "root", priv_key_path, f"hostnamectl set-hostname {shortname}", retries=8, initial_delay=1.5, log=log)
    if code != 0:
        log.warning(f"[expand] Failed to set hostname: {err or out}")
    else:
        log.info("[expand] Hostname set")
    return ip, shortname


def _join_swarm(ctx, target: str, manager_client: docker.DockerClient, docker_uri: str, ssh_timeout: int | None = None, tier: "Tier | None" = None) -> None:
    """Join the node to the swarm as worker. Idempotent.

    When ``tier`` is provided, stamps ``cs.tier`` and ``cs.capacity`` labels on the
    node after it joins the swarm, via ``_apply_labels_after_join``. That helper
    matches the joined node by hostname/short-name (``target``), not by comparing
    ``Status.Addr`` to a public IP — nodes join with ``--advertise-addr <VPC-ip>``,
    so ``Status.Addr`` is always the private VPC address and an IP-based match
    against the droplet's public IP can never succeed.
    """
    log = get_logger(ctx)
    priv_key_path, _ = _ensure_priv_key()
    manager_host = urlparse(docker_uri).hostname or ""

    # Resolve IP (prefer literal, then DNS/FQDN, then DO lookup)
    cfg = get_config()
    # Allow overriding SSH wait via config (seconds)
    if ssh_timeout is None:
        try:
            ssh_timeout = int(cfg.get("SSH_WAIT_TIMEOUT", 0) or 0)
        except Exception:
            ssh_timeout = 0
    do_token = cfg.get("DO_TOKEN")
    do_tag = cfg.get("DO_TAG")
    do_project = cfg.get("DO_PROJECT")
    name_template = cfg.get("DO_NAMES")
    ip = target if _looks_like_ip(target) else (
        _resolve_target_ip_via_do(do_token, target, name_template, do_tag, do_project, log=log)
        or _resolve_ip(target)
        or _resolve_ip(_expand_host_with_template(name_template, target))
        or target
    )
    _wait_for_ssh(ip, log=log, key_path=priv_key_path, timeout=ssh_timeout or 900)
    # Ensure cloud-init (docker install/pin, UFW, sshd restart) has fully
    # finished before introspecting/joining, then re-confirm SSH after the
    # cloud-init sshd restart. This is what was racing: the private-IP read
    # (`ip -o -4 addr show ...`) ran while cloud-init restarted sshd, giving
    # "Unable to connect to port 22".
    _wait_for_cloud_init(ip, priv_key_path, log=log)
    _wait_for_ssh(ip, log=log, key_path=priv_key_path, timeout=ssh_timeout or 900)
    time.sleep(1.0)

    # Idempotency check: already in a swarm?
    out = '<no output yet>'
    cmd = "docker info --format '{{.Swarm.LocalNodeState}}'"
    try:
        code, out, err = _ssh_exec_retry(ip, "root", priv_key_path, cmd, retries=10, initial_delay=1.5, log=log)
    except Exception as e:
        raise click.ClickException(f"SSH error connecting to {ip} with key {priv_key_path}\ncommand  {cmd}\n output {out}\n:Exception {e}")

    state = (out or err or "").strip().lower()

    if state == "active":
        log.info("[expand] Node already part of a swarm; skipping join")
        return

    # Preflight: manager/worker Docker major versions should match.
    # Mismatch can fail swarm TLS handshakes (e.g., ALPN-related errors).
    # Uses the module-level `_major()` helper (shared with
    # `_verify_node_provisioning`'s post-join docker-version check).
    manager_ver = _manager_docker_version(manager_client)
    worker_ver = None

    try:
        code_v, out_v, err_v = _ssh_exec_retry(
            ip,
            "root",
            priv_key_path,
            "docker version --format '{{.Server.Version}}'",
            retries=6,
            initial_delay=1.5,
            log=log,
        )
        if code_v == 0:
            worker_ver = (out_v or err_v or "").strip()
    except Exception:
        worker_ver = None

    mgr_major = _major(manager_ver)
    wrk_major = _major(worker_ver)
    if mgr_major is not None and wrk_major is not None and mgr_major != wrk_major:
        raise click.ClickException(
            "Docker version mismatch blocks safe swarm join: "
            f"manager={manager_ver} (major {mgr_major}), worker={worker_ver} (major {wrk_major}). "
            "Align worker Docker engine major version with the manager, then retry join."
        )
    if manager_ver and worker_ver:
        if mgr_major is not None and wrk_major is not None:
            log.info(
                f"[expand] Join preflight passed: manager docker={manager_ver} (major {mgr_major}), "
                f"worker docker={worker_ver} (major {wrk_major})"
            )
        else:
            log.info(
                f"[expand] Join preflight passed: manager docker={manager_ver}, worker docker={worker_ver}"
            )
    else:
        log.info("[expand] Join preflight: docker version comparison unavailable; proceeding")

    # Determine worker VPC IP to use for advertise/data path
    code_ip, out_ip, err_ip = _ssh_exec_retry(
        ip,
        "root",
        priv_key_path,
        "ip -o -4 addr show | awk '$4 ~ /10\\.124\\./ {print $4}' | cut -d/ -f1 | head -n1",
        retries=10,
        initial_delay=1.5,
        log=log,
    )
    worker_vpc_ip = (out_ip or err_ip or "").strip() or ip

    # Get worker join token
    try:
        join_token = manager_client.swarm.attrs["JoinTokens"]["Worker"]
    except Exception:
        join_token = manager_client.api.inspect_swarm()["JoinTokens"]["Worker"]

    # Prefer a manager private address (from node ManagerStatus.Addr) for join,
    # then fall back to swarm-advertised RemoteManagers address, then DOCKER_URI.
    # This avoids ALPN/TLS handshake issues via public/FQDN endpoints.
    join_target = None
    try:
        leader_addr = None
        manager_addr = None
        for n in manager_client.nodes.list():
            attrs = n.attrs or {}
            spec = attrs.get("Spec", {}) or {}
            if (spec.get("Role") or "").lower() != "manager":
                continue
            mstat = attrs.get("ManagerStatus", {}) or {}
            addr = (mstat.get("Addr") or "").strip()
            if not addr:
                continue
            if mstat.get("Leader"):
                leader_addr = addr
                break
            if not manager_addr:
                manager_addr = addr
        join_target = leader_addr or manager_addr
    except Exception:
        join_target = None

    try:
        if not join_target:
            swarm_info = manager_client.api.inspect_swarm()
            remote_managers = (swarm_info or {}).get("RemoteManagers") or []
            for rm in remote_managers:
                addr = (rm or {}).get("Addr")
                if addr:
                    join_target = addr
                    break
    except Exception:
        pass

    if not join_target:
        if not manager_host:
            raise click.ClickException(
                f"Unable to determine swarm manager join endpoint from DOCKER_URI={docker_uri}"
            )
        join_target = f"{manager_host}:2377"

    log.info(f"[expand] Using swarm manager join target: {join_target}")
    # Include advertise-addr and data-path-addr for VPC dataplane
    join_cmd = (
        f"docker swarm join --token {join_token} "
        f"--advertise-addr {worker_vpc_ip} --data-path-addr {worker_vpc_ip} "
        f"{join_target}"
    )
    log.info("[expand] Executing swarm join on droplet")
    code, out, err = _ssh_exec_retry(ip, "root", priv_key_path, join_cmd, retries=10, initial_delay=2.0, log=log)
    if code != 0:
        # If already part of swarm, docker returns an error; re-check and swallow
        code2, out2, err2 = _ssh_exec_retry(ip, "root", priv_key_path, "docker info --format '{{.Swarm.LocalNodeState}}'", retries=6, initial_delay=2.0, log=log)
        if (out2 or err2 or "").strip().lower() == "active":
            log.info("[expand] Node appears to already be joined; continuing")
            return
        raise click.ClickException(f"Failed to join swarm on remote node: {err or out}")
    
    log.info("[expand] Join command executed successfully on droplet")
    # Apply optional node label after join
    try:
        cfg = get_config()
        label = cfg.get("SWARM_NODE_LABEL")
        if label:
            # Wait briefly for node to appear in manager, then match by IP
            deadline = time.time() + 90
            applied = False
            while time.time() < deadline and not applied:
                try:
                    for n in manager_client.nodes.list():
                        try:
                            info = manager_client.api.inspect_node(n.id)  # type: ignore[attr-defined]
                            addr = ((info or {}).get("Status", {}) or {}).get("Addr")
                            if addr == ip:
                                name = ((info or {}).get("Description", {}) or {}).get("Hostname") or ""
                                if name:
                                    applied = _ensure_label_on_node(manager_client, name, label, log=log) or applied
                                    break
                        except Exception:
                            continue
                except Exception:
                    pass
                if not applied:
                    time.sleep(3)
            # Fallback to name guess if needed
            if not applied:
                name_guess = target if "." in target and not _looks_like_ip(target) else (target.split(".")[0] if not _looks_like_ip(target) else None)
                if name_guess:
                    _ensure_label_on_node(manager_client, name_guess, label, log=log)
    except Exception:
        pass

    # Apply cs.tier and cs.capacity labels if tier is known. Matched by
    # hostname (via _apply_labels_after_join), not by comparing Status.Addr
    # to a public IP — nodes join with --advertise-addr <VPC-ip>, so
    # Status.Addr is always the private VPC address.
    if tier is not None:
        _apply_labels_after_join(
            manager_client,
            target,
            {"cs.tier": tier.name, "cs.capacity": str(tier.capacity)},
            log=log,
        )


# ----- Node info and purge commands -----


def _regex_from_template(name_template: str) -> re.Pattern:
    if "{serial}" not in name_template:
        raise ValueError("name_template must contain '{serial}' placeholder")

    prefix, suffix = name_template.split("{serial}", 1)
    prefix_re = re.escape(prefix)
    suffix_re = re.escape(suffix)

    if suffix and suffix.startswith('.'):
        suffix_pattern = rf"(?:{suffix_re})?"
    else:
        suffix_pattern = suffix_re

    return re.compile(rf"^{prefix_re}(\d+){suffix_pattern}$")


def _list_droplets_by_tag_or_project(token: str, project_id: str | None, do_tag: str | None, log=None) -> list[dict]:
    """Return droplet dicts using python-digitalocean. Prefer tag filter; fallback to project resources."""
    try:
        mgr = digitalocean.Manager(token=token)
        droplet_objs = []
        if do_tag:
            droplet_objs = mgr.get_all_droplets(tag_name=do_tag)
        elif project_id:
            # Collect droplet IDs from project resources
            proj = digitalocean.Project(token=token, id=project_id)
            try:
                resources = proj.get_all_resources()  # list of URN strings
            except Exception:
                resources = []
            ids = [urn.split(":")[-1] for urn in resources if isinstance(urn, str) and urn.startswith("do:droplet:")]
            idset = set(ids)
            # Map ids to droplet objects via one list call
            all_droplets = mgr.get_all_droplets()
            droplet_objs = [d for d in all_droplets if str(getattr(d, "id", "")) in idset]
        else:
            droplet_objs = mgr.get_all_droplets()
        # Convert to dicts similar to REST response used elsewhere
        results = []
        for d in droplet_objs:
            nets = getattr(d, "networks", {}) or {}
            v4s = []
            if isinstance(nets, dict):
                v4s = nets.get("v4", [])
            results.append({
                "id": getattr(d, "id", None),
                "name": getattr(d, "name", None),
                "tags": list(getattr(d, "tags", []) or []),
                "networks": {"v4": v4s},
            })
        return results
    except Exception as e:
        if log:
            log.warning(f"[info] Failed to list droplets: {e}")
        return []


def _droplet_public_ip(d: dict) -> str | None:
    nets = (d or {}).get("networks", {})
    for v4 in nets.get("v4", []):
        if v4.get("type") == "public":
            return v4.get("ip_address")
    return None


@node.command(name="info")
@click.option("--all", "show_all", is_flag=True, help="Include all DO droplets and display their tags column")
@click.pass_context
def expand_info(ctx, show_all: bool):
    """Show manager details and cluster/candidate nodes from Swarm and DO."""
    log = get_logger(ctx)
    cfg = get_config()

    docker_uri = cfg.get("DOCKER_URI")
    do_token = cfg.get("DO_TOKEN")
    do_project = cfg.get("DO_PROJECT")
    do_tag = cfg.get("DO_TAG")
    name_template = cfg.get("DO_NAMES")
    if not (docker_uri and do_token and name_template):
        raise click.ClickException("Missing required config: DOCKER_URI, DO_TOKEN, or DO_NAMES")

    # Manager details
    manager_host = urlparse(docker_uri).hostname or ""
    manager_ip = _resolve_ip(manager_host) or ""

    # Docker client
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    # Swarm nodes and roles
    swarm_nodes = []
    swarm_roles: dict[str, str] = {}
    swarm_leaders: set[str] = set()
    try:
        for n in client.nodes.list():
            name = n.attrs.get("Description", {}).get("Hostname", "")
            if not name:
                continue
            swarm_nodes.append(name)
            short = name.split(".")[0]
            role = (n.attrs.get("Spec", {}).get("Role") or "").lower()
            is_leader = bool((n.attrs.get("ManagerStatus", {}) or {}).get("Leader"))
            if role:
                swarm_roles[short] = role
            if is_leader:
                swarm_leaders.add(short)
    except Exception:
        pass
    swarm_short = {n.split(".")[0]: n for n in swarm_nodes}

    # Resolve configured project to an ID if it's a name
    resolved_project_id = _resolve_project_id_by_name_or_id(do_token, do_project, log=log) if do_project else None
    target_project_id = resolved_project_id or do_project

    # Dataclass to hold row info
    @dataclass
    class HostInfo:
        name: str
        short: str
        ip: str = ""
        in_swarm: bool = False
        in_cloud: bool = False
        droplet_id: str | None = None
        project_id: str | None = None
        project_name: str | None = None
        tags: list[str] = field(default_factory=list)
        is_manager: bool = False

        def status(self, pat: re.Pattern) -> str:
            if self.in_swarm and self.in_cloud:
                return "In Swarm"
            if self.in_swarm and not self.in_cloud:
                return "Swarm only"
            if self.in_cloud and (pat.match(self.name) or pat.match(self.short)):
                return "Cloud only"
            return "Unknown"

        def purgable(self, do_tag_val: str | None) -> bool | None:
            if not do_tag_val:
                return None
            return (self.in_cloud and not self.in_swarm) or (self.in_swarm and not self.in_cloud)

        def visible_without_all(self, do_tag_val: str | None, proj_id: str | None, pat: re.Pattern) -> bool:
            # Always show swarm nodes
            if self.in_swarm:
                return True
            # Cloud candidates must match template
            if not self.in_cloud or not (pat.match(self.name) or pat.match(self.short)):
                return False
            # Apply filters
            if do_tag_val and proj_id:
                return (do_tag_val in self.tags) and (self.project_id == proj_id)
            if do_tag_val:
                return do_tag_val in self.tags
            if proj_id:
                return self.project_id == proj_id
            return False

    # Build HostInfo map from all droplets
    droplets_all = _list_droplets_by_tag_or_project(do_token, None, None, log=log)
    proj_map = _map_droplet_to_project_ids(do_token, log=log)
    project_name_cache: dict[str, str] = {}
    hosts: dict[str, HostInfo] = {}
    for d in droplets_all:
        nm = (d.get("name") or "").strip()
        if not nm:
            continue
        short = nm.split(".")[0]
        ip = _droplet_public_ip(d) or ""
        did = str(d.get("id")) if d.get("id") is not None else None
        pid = proj_map.get(did)
        if pid and pid in project_name_cache:
            pname = project_name_cache[pid]
        else:
            pname = _get_project_name(do_token, pid, log=log) if pid else None
            if pid and pname:
                project_name_cache[pid] = pname
        tags = list(d.get("tags") or [])
        hosts[short] = HostInfo(name=nm, short=short, ip=ip, in_cloud=True, droplet_id=did, project_id=pid, project_name=pname, tags=tags)

    # Upsert swarm nodes
    for sname in swarm_nodes:
        short = sname.split(".")[0]
        if short in hosts:
            hi = hosts[short]
            hi.in_swarm = True
            hi.name = sname
            hi.is_manager = (swarm_roles.get(short) == "manager")
        else:
            hosts[short] = HostInfo(name=sname, short=short, in_swarm=True, in_cloud=False, is_manager=(swarm_roles.get(short) == "manager"))

    # Template match regex
    pat = _regex_from_template(name_template)

    # Header
    click.echo("Manager")
    click.echo(f"  Host: {manager_host}")
    click.echo(f"  IP  : {manager_ip}")
    proj_name = _get_project_name(do_token, resolved_project_id or do_project, log=log)
    click.echo(f"  Project (cfg): {proj_name or '-'}")
    click.echo("")

    # Render with tabulate
    from tabulate import tabulate
    rows: list[dict] = []
    for short, hi in sorted(hosts.items(), key=lambda kv: kv[0]):
        if not show_all and not hi.visible_without_all(do_tag, target_project_id, pat):
            continue
        stat = hi.status(pat)
        purg = hi.purgable(do_tag)
        purg_s = "-" if purg is None else ("Yes" if purg else "No")
        row = {
            "Name": hi.name,
            "IP": hi.ip or "",
            "In Swarm": "Yes" if hi.in_swarm else "No",
            "In Cloud": "Yes" if hi.in_cloud else "No",
            "Manager": "Yes" if hi.is_manager else "No",
            "Status": stat,
            "Purgable": purg_s,
            "Project": hi.project_name or "-",
        }
        if show_all:
            row["Tags"] = ",".join(hi.tags)
        rows.append(row)

    if rows:
        click.echo(tabulate(rows, headers="keys", tablefmt="github"))
    else:
        click.echo("(no nodes to display)")


def _sync_domain_records(ctx) -> None:
    """Sync A records in the configured domain to reflect current swarm membership.

    - Create/update A records for all nodes in the swarm (hostnames matching DO_NAMES) to point to their public IPs.
    - Remove A records that match the naming pattern but are not currently in the swarm.
    """
    log = get_logger(ctx)
    cfg = get_config()

    docker_uri = cfg.get("DOCKER_URI")
    do_token = cfg.get("DO_TOKEN")
    name_template = cfg.get("DO_NAMES")

    if not (docker_uri and do_token and name_template):
        raise click.ClickException("Missing required config: DOCKER_URI, DO_TOKEN, or DO_NAMES")

    if "." not in name_template:
        raise click.ClickException("DO_NAMES must include a domain suffix, e.g., swarm{serial}.example.com")

    domain_suffix = name_template.split(".", 1)[1]
    short_pattern = name_template.split(".")[0]  # e.g., 'swarm{serial}'
    short_prefix = short_pattern.split("{serial}")[0]
    short_re = re.compile(rf"^{re.escape(short_prefix)}(\d+)$")

    # Connect to docker
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    # Build set of swarm short names
    swarm_shorts: set[str] = set()
    try:
        for n in client.nodes.list():
            name = n.attrs.get("Description", {}).get("Hostname", "")
            if name:
                swarm_shorts.add(name.split(".")[0])
    except Exception as e:
        log.warning(f"[domains] Failed to list swarm nodes: {e}")

    # Build short->IP map from all droplets
    droplets_all = _list_droplets_by_tag_or_project(do_token, None, None, log=log)
    short_to_ip: dict[str, str] = {}
    for d in droplets_all:
        nm = (d.get("name") or "").strip()
        if not nm:
            continue
        s = nm.split(".")[0]
        ip = _droplet_public_ip(d)
        if s and ip:
            short_to_ip[s] = ip

    # Filter to names matching our pattern
    desired_shorts = {s for s in swarm_shorts if short_re.match(s)}

    # Access DO Domain
    dom = digitalocean.Domain(token=do_token, name=domain_suffix)
    try:
        records = dom.get_records()
    except Exception as e:
        raise click.ClickException(f"Failed to fetch domain records for {domain_suffix}: {e}")

    # Map current A records for our pattern
    current_a: dict[str, list] = {}
    for r in records:
        try:
            if getattr(r, "type", "") == "A":
                rname = getattr(r, "name", "") or ""
                if short_re.match(rname):
                    current_a.setdefault(rname, []).append(r)
        except Exception:
            continue

    created = 0
    updated = 0
    removed = 0
    skipped_no_ip = 0
    ttl_seconds = 60

    # Create or update desired records
    for short in sorted(desired_shorts):
        ip = short_to_ip.get(short)
        if not ip:
            skipped_no_ip += 1
            log.warning(f"[domains] Skipping {short}.{domain_suffix}: no public IP found")
            continue
        recs = current_a.get(short, [])
        if not recs:
            try:
                dom.create_new_domain_record(type="A", name=short, data=ip, ttl=ttl_seconds)
                created += 1
                log.info(f"[domains] Created A {short}.{domain_suffix} -> {ip} (ttl={ttl_seconds}s)")
            except Exception as e:
                log.warning(f"[domains] Failed to create A {short}.{domain_suffix}: {e}")
        else:
            # Update first; delete duplicates with mismatched data
            primary = recs[0]
            try:
                changed = False
                if getattr(primary, "data", "") != ip:
                    primary.data = ip
                    changed = True
                # Ensure TTL is set to 60s
                try:
                    if int(getattr(primary, "ttl", 0) or 0) != ttl_seconds:
                        primary.ttl = ttl_seconds
                        changed = True
                except Exception:
                    # If ttl unparsable, force set
                    try:
                        primary.ttl = ttl_seconds
                        changed = True
                    except Exception:
                        pass
                if changed:
                    primary.save()
                    updated += 1
                    log.info(f"[domains] Updated A {short}.{domain_suffix} -> {ip} (ttl={ttl_seconds}s)")
            except Exception as e:
                log.warning(f"[domains] Failed to update A {short}.{domain_suffix}: {e}")
            # Remove duplicates beyond the first
            for dup in recs[1:]:
                try:
                    dup.destroy()
                    removed += 1
                    log.info(f"[domains] Removed duplicate A record for {short}.{domain_suffix}")
                except Exception:
                    pass

    # Remove records that are not desired
    for short, recs in current_a.items():
        if short in desired_shorts:
            continue
        for r in recs:
            try:
                r.destroy()
                removed += 1
                log.info(f"[domains] Removed A {short}.{domain_suffix} (not in swarm)")
            except Exception as e:
                log.warning(f"[domains] Failed to remove A {short}.{domain_suffix}: {e}")

    click.echo(f"Domain sync complete: created={created}, updated={updated}, removed={removed}, skipped(no-ip)={skipped_no_ip}")


@node.command(name="purge")
@click.option("-N", "--dry-run", is_flag=True, help="Only print what would be done")
@click.pass_context
def expand_purge(ctx, dry_run: bool):
    """Destroy DO_TAG-tagged droplets not in swarm and remove swarm nodes without droplets."""
    log = get_logger(ctx)
    cfg = get_config()

    docker_uri = cfg.get("DOCKER_URI")
    do_token = cfg.get("DO_TOKEN")
    do_tag = cfg.get("DO_TAG")

    if not (docker_uri and do_token and do_tag):
        raise click.ClickException("Missing required config: DOCKER_URI, DO_TOKEN, or DO_TAG")

    # Connect to docker
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    # Swarm nodes map short->(name, node_obj)
    swarm_nodes = {}
    try:
        for n in client.nodes.list():
            name = n.attrs.get("Description", {}).get("Hostname", "")
            if name:
                short = name.split(".")[0]
                swarm_nodes[short] = (name, n)
    except Exception as e:
        log.warning(f"[purge] Failed to list swarm nodes: {e}")

    # DO droplets by tag map short->(name, id)
    droplets = _list_droplets_by_tag_or_project(do_token, project_id=None, do_tag=do_tag, log=log)
    droplet_map = {}
    for d in droplets:
        nm = (d.get("name") or "").strip()
        if not nm:
            continue
        droplet_map[nm.split(".")[0]] = (nm, d.get("id"))

    # Compute actions
    to_destroy = []
    for short, (nm, did) in droplet_map.items():
        if short not in swarm_nodes:
            to_destroy.append((short, nm, did))

    to_remove = []
    for short, (nm, node_obj) in swarm_nodes.items():
        if short not in droplet_map:
            to_remove.append((short, nm, node_obj))

    # Print plan
    click.echo("Purge plan:")
    if to_destroy:
        click.echo("- Destroy droplets (tagged with DO_TAG) not in swarm:")
        for _, nm, did in sorted(to_destroy):
            click.echo(f"    destroy droplet {nm} (id={did})")
    else:
        click.echo("- No droplets to destroy")

    if to_remove:
        click.echo("- Remove swarm nodes without running droplets:")
        for _, nm, _ in sorted(to_remove):
            click.echo(f"    remove node {nm}")
    else:
        click.echo("- No swarm nodes to remove")

    if dry_run:
        click.echo("Dry-run: no changes made.")
        return

    # Execute
    err_count = 0

    # Destroy droplets
    for short, nm, did in to_destroy:
        try:
            d = digitalocean.Droplet(token=do_token, id=did)
            d.destroy()
            log.info(f"[purge] Destroyed droplet {nm} (id={did})")
        except Exception as e:
            err_count += 1
            log.warning(f"[purge] Failed to destroy droplet {nm} (id={did}): {e}")

    # Remove swarm nodes (force)
    for short, nm, node_obj in to_remove:
        try:
            node_obj.remove(force=True)
            log.info(f"[purge] Removed swarm node {nm}")
        except Exception as e:
            err_count += 1
            log.warning(f"[purge] Failed to remove node {nm}: {e}")

    if err_count:
        raise click.ClickException(f"Purge completed with {err_count} errors")
    click.echo("Purge completed successfully")


def graceful_remove_node(
    ctx,
    manager_client: docker.DockerClient,
    mgr,
    fqdn: str,
    *,
    dry_run: bool,
    log,
) -> None:
    """Drain → wait-tasks-drained → remove-swarm-node → destroy-droplet for a named node.

    Shared between the 'stop' CLI command and cspawn/cs_docker/autoscale.py so that
    autoscale.py can remove nodes without duplicating the drain/remove/destroy sequence.
    Before draining, clears any stale `node.hostname==` pins on this node so a
    hard-pinned service can be rescheduled elsewhere instead of being permanently
    orphaned (see `_unpin_services_from_node`).

    Parameters
    ----------
    ctx:
        Click context (passed through to helpers that need it).
    manager_client:
        Docker client connected to the swarm manager.
    mgr:
        DigitalOcean Manager object (python-digitalocean).
    fqdn:
        Fully-qualified (or short) node name to remove.
    dry_run:
        When True, print the planned actions (including how many pinned
        services would be unpinned) and return without making any changes —
        no `svc.update()` call is made in this mode.
    log:
        Logger instance (from get_logger).
    """
    import digitalocean as _do

    cfg = get_config()
    do_token = cfg.get("DO_TOKEN")
    do_names = cfg.get("DO_NAMES")
    do_tag = cfg.get("DO_TAG")
    do_project = cfg.get("DO_PROJECT")

    # Resolve the droplet from the FQDN spec
    droplet, resolved_fqdn = _resolve_droplet_by_spec(
        mgr=mgr,
        token=do_token,
        do_names=do_names,
        do_tag=do_tag,
        do_project=do_project,
        spec=fqdn,
        log=log,
    )

    short = resolved_fqdn.split(".")[0]
    node_obj = _find_swarm_node(manager_client, resolved_fqdn, short)

    if dry_run:
        actions = []
        would_unpin = _unpin_services_from_node(
            manager_client, resolved_fqdn, log=log, dry_run=True
        )
        if would_unpin:
            actions.append(f"unpin {would_unpin} service(s) pinned to {resolved_fqdn}")
        if node_obj:
            actions.append(f"drain swarm node {resolved_fqdn}")
            actions.append(f"wait for tasks to drain on {resolved_fqdn}")
            actions.append(f"remove swarm node {resolved_fqdn}")
        else:
            actions.append(f"(node {resolved_fqdn} not in swarm; skip drain/remove)")
        actions.append(f"destroy droplet {resolved_fqdn} (id={droplet.id})")
        click.echo("Would perform (in order):")
        for a in actions:
            click.echo(f"  - {a}")
        return

    # A stale node.hostname pin is meaningful even if the swarm-side node
    # object is already gone, so clear it regardless of node_obj above.
    _unpin_services_from_node(manager_client, resolved_fqdn, log=log)

    if node_obj:
        log.info(f"[stop] Draining swarm node {resolved_fqdn}")
        _drain_swarm_node(manager_client, node_obj, log=log)
        try:
            _wait_node_tasks_drained(manager_client, node_obj.id, log=log)
        except Exception as e:
            # If timeout, proceed but warn
            log.warning(f"[stop] Proceeding to remove despite drain timeout: {e}")
        try:
            log.info(f"[stop] Removing swarm node {resolved_fqdn}")
            try:
                node_obj.remove(force=True)
            except Exception:
                # Try low-level API remove as fallback (Docker SDK 2.0 compatible)
                manager_client.api.remove_node(node_obj.id, force=True)  # type: ignore[attr-defined]
        except Exception as e:
            # It's possible the node is down; proceed to droplet destroy
            log.warning(f"[stop] Failed to remove node cleanly: {e}")
    else:
        log.info(f"[stop] Node {resolved_fqdn} not found in swarm; skipping drain/remove")

    # Finally, destroy the droplet
    try:
        log.info(f"[stop] Destroying droplet {resolved_fqdn} (id={droplet.id})")
        droplet.destroy()
        click.echo(f"Stopped droplet: {resolved_fqdn}")
    except Exception as e:
        raise click.ClickException(f"Failed to destroy droplet {resolved_fqdn}: {e}")


@node.command(name="stop")
@click.option("-F", "--force", is_flag=True, help="Force stop immediately (required until drain/remove is implemented)")
@click.option("-N", "--dry-run", is_flag=True, help="Only print what would be done")
@click.argument("node_spec", nargs=1)
@click.pass_context
def stop_node(ctx, force: bool, dry_run: bool, node_spec: str):
    """Stop (destroy) a DigitalOcean node by spec (serial, shortname, or FQDN).

    Initially requires --force; draining/removing from swarm is not implemented yet.
    The --force path best-effort clears stale node.hostname pins before destroying
    the droplet (see `_unpin_services_from_node`); a failure there never blocks
    the destroy.
    """
    log = get_logger(ctx)
    cfg = get_config()

    do_token = cfg.get("DO_TOKEN")
    do_names = cfg.get("DO_NAMES")
    do_tag = cfg.get("DO_TAG")
    do_project = cfg.get("DO_PROJECT")
    docker_uri = cfg.get("DOCKER_URI")

    if not (do_token and do_names):
        raise click.ClickException("Missing DO_TOKEN or DO_NAMES in configuration")

    mgr = digitalocean.Manager(token=do_token)

    droplet, fqdn = _resolve_droplet_by_spec(
        mgr=mgr,
        token=do_token,
        do_names=do_names,
        do_tag=do_tag,
        do_project=do_project,
        spec=node_spec,
        log=log,
    )

    if force:
        if dry_run:
            click.echo(f"Would stop droplet: {fqdn} (id={droplet.id})")
            return
        # Best-effort: clear stale node.hostname pins before destroying the
        # droplet, so any hard-pinned service isn't left permanently
        # unreschedulable. Never blocks the force-destroy escape hatch.
        if docker_uri:
            try:
                _mc = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
                n = _unpin_services_from_node(_mc, fqdn, log=log)
                if n:
                    log.info(f"[stop] Cleared {n} node pin(s) before force-destroying {fqdn}")
            except Exception as e:
                log.warning(f"[stop] Could not clear node pins before force-destroy (proceeding anyway): {e}")
        try:
            log.info(f"[stop] Destroying droplet {fqdn} (id={droplet.id})")
            droplet.destroy()
            click.echo(f"Stopped droplet: {fqdn}")
        except Exception as e:
            raise click.ClickException(f"Failed to destroy droplet {fqdn}: {e}")
        return

    # Non-force path: delegate to graceful_remove_node
    if not docker_uri:
        raise click.ClickException("Missing DOCKER_URI in configuration for graceful stop")
    try:
        manager_client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    graceful_remove_node(ctx, manager_client, mgr, fqdn, dry_run=dry_run, log=log)


@node.command()
@click.option("--project", "project_selector", required=False, help="DigitalOcean project name or ID for the new droplet")
@click.option("--create", "create_only", is_flag=True, help="Only create the node; if no --id given, uses next serial")
@click.option("--id", "create_serial", required=False, type=int, help="Serial id to create (used with --create or defaults in all-steps)")
@click.option("--configure", "configure_name", required=False, type=str, help="Only configure the node hostname (by name or IP)")
@click.option("--join", "join_name", required=False, type=str, help="Only join the node to the swarm (by name or IP)")
@click.option("--domains", "domains_only", is_flag=True, help="Only sync domain A records for swarm nodes (create missing, remove stale)")
@click.option("--ssh-timeout", "ssh_timeout_opt", required=False, type=int, help="Seconds to wait for SSH during configure/join (overrides config)")
@click.option("--tier", "tier_name", required=False, type=str,
              help="Node size tier from NODE_TIERS (default: DEFAULT_TIER). "
                   "See 'cspawnctl node tiers' or NODE_TIERS config key.")
@click.pass_context
def expand(ctx, project_selector: str | None, create_only: bool, create_serial: int | None, configure_name: str | None, join_name: str | None, domains_only: bool, ssh_timeout_opt: int | None, tier_name: str | None, node_op_id: str | None = None):
    """Provision and/or configure and/or join a node.

    If none of --create/--configure/--join are supplied, performs all three in order.
    Use --tier to select a node size tier defined in NODE_TIERS config (e.g. --tier large).

    ``node_op_id`` is an optional, internal-only parameter (not a `--option`;
    only reachable via `ctx.invoke(expand, node_op_id=...)`) used by the
    admin-triggered `op-run` worker to link a created droplet back to the
    `NodeOp` row that triggered this call. See `_create_droplet` for details.
    Defaults to `None` for every CLI/autoscaler caller, which is a no-op.
    """
    log = get_logger(ctx)
    log.info("[expand] Starting node expansion")
    cfg = get_config()

    # Resolve tier: --tier <name> takes precedence, otherwise use DEFAULT_TIER / first tier.
    if tier_name:
        tier = tier_by_name(cfg, tier_name)
        if tier is None:
            valid = [t.name for t in load_tiers(cfg)]
            raise click.ClickException(
                f"Unknown tier '{tier_name}'. Valid tiers: {', '.join(valid)}"
            )
    else:
        tier = default_tier(cfg)

    # Read DigitalOcean config
    do_token = cfg.get("DO_TOKEN")

    do_size = tier.slug  # derived from tier; do_size kept for backward-compat log messages
    do_image = cfg.get("DO_IMAGE", "docker-20-04")
    do_region = cfg.get("DO_REGION") or cfg.get("DO_REGIOIN") or "sfo3"
    name_template = cfg.get("DO_NAMES")
    do_tag = cfg.get("DO_TAG")

    if not project_selector and cfg.get("DO_PROJECT"):
        project_selector = cfg.get("DO_PROJECT")
        log.info(f"[expand] Using DO_PROJECT from config: {project_selector}")

    if not (do_token and name_template):
        raise click.ClickException("Missing DO_TOKEN or DO_NAMES in configuration")

    log.info(f"[expand] Using DO region={do_region} tier={tier.name} size={do_size} image={do_image}")

    docker_uri = cfg.get("DOCKER_URI")
    if not docker_uri:
        raise click.ClickException("Missing DOCKER_URI in configuration")
    try:
        manager_client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    mgr = digitalocean.Manager(token=do_token)

    # Resolve SSH wait timeout precedence: CLI > config > default
    try:
        cfg_ssh_timeout = int(cfg.get("SSH_WAIT_TIMEOUT", 0) or 0)
    except Exception:
        cfg_ssh_timeout = 0
    ssh_timeout_effective = ssh_timeout_opt if (ssh_timeout_opt and ssh_timeout_opt > 0) else (cfg_ssh_timeout or 900)

    # Domains-only short-circuit
    if domains_only:
        _sync_domain_records(ctx)
        click.echo("Domains synced.")
        return

    # Determine default behavior
    do_all = not any([create_only, create_serial is not None, configure_name, join_name])

    last_ip = None
    last_shortname = None
    last_fqdn = None

    # CREATE
    if do_all or create_only or create_serial is not None:
        droplet, ip, fqdn, shortname = _create_droplet(
            ctx,
            mgr=mgr,
            manager_client=manager_client,
            name_template=name_template,
            do_token=do_token,
            do_region=do_region,
            do_size=do_size,
            do_image=do_image,
            project_selector=project_selector,
            desired_serial=create_serial,
            docker_uri=docker_uri,
            do_tag=do_tag,
            tier=tier,
            node_op_id=node_op_id,
        )
        last_ip, last_shortname, last_fqdn = ip, shortname, fqdn

    # CONFIGURE
    target_for_config = configure_name or last_fqdn or last_ip
    if do_all or configure_name:
        if not target_for_config:
            raise click.ClickException("No target to configure; please provide --configure <name>")
        ip, shortname = _configure_node(ctx, target_for_config, desired_shortname=(last_shortname or None), ssh_timeout=ssh_timeout_effective)
        log.info(f"[expand] SSH wait timeout used for configure: {ssh_timeout_effective}s")
        last_ip, last_shortname = ip, shortname

    # JOIN
    target_for_join = join_name or last_fqdn or last_ip
    if do_all or join_name:
        if not target_for_join:
            raise click.ClickException("No target to join; please provide --join <name>")
        log.info(f"[expand] SSH wait timeout used for join: {ssh_timeout_effective}s")
        # Pass tier only for full flow or --create+join; skip cs.* labeling for standalone --join
        # (standalone --join means we're joining a pre-existing node whose tier is unknown)
        join_tier = None if (join_name and not do_all) else tier
        _join_swarm(ctx, target_for_join, manager_client, docker_uri, ssh_timeout=ssh_timeout_effective, tier=join_tier)

    # Verify membership when we know the shortname
    if last_shortname:
            deadline = time.time() + 300
            log.info("[expand] Verifying node appears in swarm membership")
            while time.time() < deadline:
                try:
                    nodes = manager_client.nodes.list()
                    names = [n.attrs.get("Description", {}).get("Hostname", "") for n in nodes]
                    if any(name.split(".")[0] == last_shortname for name in names):
                        log.info(f"[expand] Node {last_shortname} appears in swarm: {names}")
                        break
                except Exception:
                    pass
                time.sleep(5)
            else:
                raise click.ClickException("Timed out waiting for node to appear in swarm")

    # Post-join provisioning verification: a separate, later, hard-fail gate
    # distinct from _wait_for_cloud_init's earlier best-effort wait. Only runs
    # when this invocation actually configured+joined a node (i.e. we know its
    # ip and shortname) — not for a standalone --create-only run.
    if last_ip and last_shortname:
        # Drain immediately, before verification even runs: Docker Swarm marks
        # a freshly-joined node Availability=active by default, and
        # _verify_node_provisioning itself takes real wall-clock time (three
        # SSH connect attempts plus two more round-trips) during which an
        # active-but-unverified-and-cold node could still be scheduled onto.
        # This closes that race earlier than before (previously drain only
        # fired reactively, in the verify-failure branch below).
        node_obj = None
        try:
            node_obj = _find_swarm_node(manager_client, last_fqdn, last_shortname)
            if node_obj is not None:
                _drain_swarm_node(manager_client, node_obj, log=log)
            else:
                log.warning(f"[expand] Could not find swarm node {last_shortname} to drain (pre-verify)")
        except Exception as e:
            log.warning(f"[expand] Best-effort pre-verify drain of {last_shortname} failed: {e}")

        log.info("[expand] Verifying node provisioning (SSH, docker version, cloud-init)")
        verify_key_path, _ = _ensure_priv_key()
        expected_docker_version = _manager_docker_version(manager_client) or _expected_docker_version(cfg)
        failures = _verify_node_provisioning(
            last_ip, verify_key_path,
            expected_docker_version=expected_docker_version,
            log=log,
        )
        if failures:
            for failure in failures:
                log.error(f"[expand] Post-join verification failed: {failure}")
            # Best-effort: keep Swarm from scheduling more work onto a node we
            # just determined is defective. A drain failure is logged, not
            # raised — the verification failure is what aborts the command.
            # This is a second attempt at the same drain done above: in the
            # common case it's a harmless idempotent no-op, and it remains a
            # fallback for the case where the earlier drain itself failed.
            try:
                node_obj = _find_swarm_node(manager_client, last_fqdn, last_shortname)
                if node_obj is not None:
                    _drain_swarm_node(manager_client, node_obj, log=log)
                else:
                    log.warning(f"[expand] Could not find swarm node {last_shortname} to drain")
            except Exception as e:
                log.warning(f"[expand] Best-effort drain of {last_shortname} failed: {e}")
            raise click.ClickException(
                f"Node {last_shortname} failed post-join provisioning verification and "
                f"was drained: {'; '.join(failures)}"
            )
        log.info(f"[expand] Node {last_shortname} passed post-join provisioning verification")

        # Purely diagnostic: warn if the node's docker-ce major has drifted
        # from the manager's (e.g. a stale golden snapshot). Never alters the
        # verify verdict above -- reuses the same expected_docker_version
        # already resolved for that call, no second resolution.
        _check_docker_staleness(
            last_ip, verify_key_path,
            expected_docker_version=expected_docker_version, log=log,
        )

        # Warm the node's image cache before it can be scheduled onto, then
        # reactivate it. Best-effort: a pre-pull failure never blocks
        # activation, and activation itself is retried loudly (see
        # _activate_swarm_node) since a node that stays drained after being
        # warmed is silently-wasted capacity.
        images: list[str] = []
        try:
            from cspawn.cli.util import get_app
            app = get_app(ctx)
            with app.app_context():
                images = _get_prepull_images(cfg)
        except Exception as e:
            log.warning(f"[expand] Failed to resolve pre-pull image list: {e}")
            images = []
        _prepull_images(
            last_ip, verify_key_path, images,
            timeout=cfg.get("NODE_PREPULL_TIMEOUT_S", 300), log=log,
        )
        activate_node_obj = node_obj or _find_swarm_node(manager_client, last_fqdn, last_shortname)
        if activate_node_obj is not None:
            _activate_swarm_node(manager_client, activate_node_obj, log=log)
        else:
            log.warning(f"[expand] Could not find swarm node {last_shortname} to activate")

    # Output summary
    if do_all:
        click.echo(f"Created and joined node: {last_fqdn}")
    elif create_only or create_serial is not None:
        click.echo(f"Created node: {last_fqdn}")
    elif configure_name:
        click.echo(f"Configured node: {target_for_config}")
    elif join_name:
        click.echo(f"Joined node: {target_for_join}")

    # After successful full flow, optionally sync domains for convenience
    try:
        _sync_domain_records(ctx)
    except Exception:
        # Non-fatal
        pass


@node.command(name="label-backfill")
@click.option("--apply", "do_apply", is_flag=True,
              help="Write cs.tier and cs.capacity labels (default: dry-run, print only).")
@click.pass_context
def label_backfill(ctx, do_apply: bool):
    """Stamp cs.tier/cs.capacity labels on existing unlabeled swarm nodes.

    Reads each node's DigitalOcean droplet size_slug, maps it to a tier via
    NODE_TIERS (tier_for_slug), and (with --apply) writes the labels.

    Safe to run multiple times: nodes with cs.tier already set are skipped.
    """
    from cspawn.cs_docker.tiers import load_tiers, tier_for_slug

    log = get_logger(ctx)
    cfg = get_config()
    docker_uri = cfg.get("DOCKER_URI")
    do_token = cfg.get("DO_TOKEN")
    name_template = cfg.get("DO_NAMES")
    do_tag = cfg.get("DO_TAG")

    if not do_token:
        raise click.ClickException("Missing DO_TOKEN in configuration")
    if not (docker_uri and name_template):
        raise click.ClickException("Missing DOCKER_URI or DO_NAMES in configuration")

    tiers = load_tiers(cfg)
    if not tiers:
        raise click.ClickException("No tiers configured; check NODE_TIERS or DO_SIZE.")

    # Connect to swarm manager
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager: {e}")

    # Fetch all droplets once via python-digitalocean (need size_slug, not in dict helper)
    mgr = digitalocean.Manager(token=do_token)
    try:
        droplet_objs = mgr.get_all_droplets(tag_name=do_tag) if do_tag else mgr.get_all_droplets()
    except Exception as e:
        raise click.ClickException(f"Failed to list DO droplets: {e}")

    # Build short-name -> droplet object map for fast lookup
    droplet_by_name = {}
    for d in droplet_objs:
        name = (getattr(d, "name", None) or "").strip()
        if name:
            droplet_by_name[name.split(".")[0]] = d

    pat = _regex_from_template(name_template)

    rows = []  # list of (node_name, slug, tier_name, capacity, action)

    for n in client.nodes.list():
        attrs = n.attrs or {}
        hostname = ((attrs.get("Description") or {}).get("Hostname") or "").strip()
        if not hostname or not pat.match(hostname):
            continue
        short = hostname.split(".")[0]

        # Check existing labels — skip if cs.tier already set
        spec_labels = ((attrs.get("Spec") or {}).get("Labels") or {})
        if "cs.tier" in spec_labels:
            rows.append((short, "—", spec_labels.get("cs.tier", "?"),
                         spec_labels.get("cs.capacity", "?"), "already-set"))
            continue

        # Resolve droplet
        droplet = droplet_by_name.get(short)
        if not droplet:
            rows.append((short, "?", "?", "?", "WARN: droplet not found"))
            log.warning(f"[label-backfill] Node {short}: no matching DO droplet found; skipping")
            continue

        slug = (getattr(droplet, "size_slug", None) or "").strip()
        tier = tier_for_slug(cfg, slug)
        if tier is None:
            rows.append((short, slug, "?", "?", "WARN: unknown slug"))
            log.warning(
                f"[label-backfill] Node {short} has slug '{slug}' not in NODE_TIERS; skipping"
            )
            continue

        if do_apply:
            _ensure_node_labels(
                client, hostname,
                {"cs.tier": tier.name, "cs.capacity": str(tier.capacity)},
                log=log,
            )
            action = "applied"
        else:
            action = "would-apply"

        rows.append((short, slug, tier.name, str(tier.capacity), action))

    # Print table
    click.echo(f"{'NODE':<12} {'SIZE_SLUG':<24} {'INFERRED_TIER':<16} {'CAPACITY':<10} ACTION")
    click.echo("-" * 80)
    for node_name, slug, tier_name, cap, action in rows:
        click.echo(f"{node_name:<12} {slug:<24} {tier_name:<16} {cap:<10} {action}")

    if not do_apply:
        click.echo("\n(Dry run. Re-run with --apply to write labels.)")


def _select_contract_candidate(
    client: docker.DockerClient, cfg
) -> tuple[int, str] | None:
    """Return (serial, fqdn) of the best empty worker to remove, or None.

    Selection policy:
    - Eligible: hostname matches DO_NAMES, not leader, not manager, running_hosts == 0.
    - Sort key: (capacity ASC, serial DESC) — remove smallest capacity first;
      among ties, remove the newest (highest serial) to preserve long-lived nodes.

    Returns None if no eligible empty node exists. Never returns a loaded node.
    """
    from cspawn.cs_docker.tiers import node_capacity

    name_template = cfg.get("DO_NAMES")
    if not name_template:
        return None

    pat = _regex_from_template(name_template)
    running = _running_hosts_by_node(client)

    candidates = []
    for n in client.nodes.list():
        attrs = n.attrs or {}
        hostname = ((attrs.get("Description") or {}).get("Hostname") or "").strip()
        if not hostname or not pat.match(hostname):
            continue
        short = hostname.split(".")[0]
        role = ((attrs.get("Spec") or {}).get("Role") or "").lower()
        is_leader = bool(((attrs.get("ManagerStatus") or {}).get("Leader")) or False)
        if is_leader or role == "manager":
            continue
        host_count = running.get(short, 0)
        if host_count > 0:
            continue  # never remove a loaded node in default mode

        m = pat.match(hostname)
        try:
            serial = int(m.group(1))
        except Exception:
            continue

        cap = node_capacity(attrs, cfg)
        candidates.append((cap, -serial, serial, hostname))  # -serial for DESC sort

    if not candidates:
        return None

    candidates.sort()  # (capacity ASC, -serial ASC i.e. serial DESC)
    _, _, serial, fqdn = candidates[0]
    return (serial, fqdn)


def _select_drain_candidate(
    client: docker.DockerClient, cfg
) -> tuple[int, str] | None:
    """Return (serial, fqdn) of the least-loaded eligible worker, or None.

    Used by --force-drain when no empty node exists. Selection among loaded
    eligible workers: fewest running_hosts first, then (capacity ASC, serial DESC).

    Still never selects the manager or leader.
    """
    from cspawn.cs_docker.tiers import node_capacity

    name_template = cfg.get("DO_NAMES")
    if not name_template:
        return None

    pat = _regex_from_template(name_template)
    running = _running_hosts_by_node(client)

    candidates = []
    for n in client.nodes.list():
        attrs = n.attrs or {}
        hostname = ((attrs.get("Description") or {}).get("Hostname") or "").strip()
        if not hostname or not pat.match(hostname):
            continue
        short = hostname.split(".")[0]
        role = ((attrs.get("Spec") or {}).get("Role") or "").lower()
        is_leader = bool(((attrs.get("ManagerStatus") or {}).get("Leader")) or False)
        if is_leader or role == "manager":
            continue

        m = pat.match(hostname)
        try:
            serial = int(m.group(1))
        except Exception:
            continue

        host_count = running.get(short, 0)
        cap = node_capacity(attrs, cfg)
        # Sort: fewest hosts first, then capacity ASC, then serial DESC (-serial ASC)
        candidates.append((host_count, cap, -serial, serial, hostname))

    if not candidates:
        return None

    candidates.sort()
    _, _, _, serial, fqdn = candidates[0]
    return (serial, fqdn)


@node.command(name="contract")
@click.option("-N", "--dry-run", is_flag=True, help="Only print what would be done")
@click.option("--force-drain", is_flag=True,
              help="When no empty node exists, gracefully drain the least-loaded eligible worker.")
@click.pass_context
def contract_node(ctx, dry_run: bool, force_drain: bool):
    """Shrink the cluster by removing the smallest empty worker node.

    Default mode: only removes nodes with zero running code-server hosts. If no
    empty node exists, exits cleanly without removing anything.

    --force-drain mode: when no empty node exists, selects the least-loaded
    eligible worker and gracefully drains it (drain → wait tasks drained →
    remove node → destroy droplet) so live sessions reschedule. Never removes
    the manager or leader.

    Use --dry-run with either mode to see which node would be selected without
    making any changes.
    """
    log = get_logger(ctx)
    cfg = get_config()

    docker_uri = cfg.get("DOCKER_URI")
    name_template = cfg.get("DO_NAMES")
    if not (docker_uri and name_template):
        raise click.ClickException("Missing DOCKER_URI or DO_NAMES in configuration")

    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    try:
        result = _select_contract_candidate(client, cfg)
    except Exception as e:
        raise click.ClickException(f"Failed to select contract candidate: {e}")

    if result is None and force_drain:
        # Fall back to least-loaded worker
        try:
            result = _select_drain_candidate(client, cfg)
        except Exception as e:
            raise click.ClickException(f"Failed to select drain candidate: {e}")
        if result is None:
            click.echo("No empty node to contract.")
            return
        serial, fqdn = result
        if dry_run:
            click.echo(f"Would force-drain node {fqdn} (serial={serial})")
            return
        log.info(f"[contract] Force-drain: selected node {fqdn} (serial={serial})")
        ctx.invoke(stop_node, force=False, dry_run=False, node_spec=fqdn)
        return

    if result is None:
        click.echo("No empty node to contract.")
        return

    serial, fqdn = result
    if dry_run:
        click.echo(f"Would contract by stopping node {fqdn} (serial={serial})")
        return

    log.info(f"[contract] Selected node {fqdn} (serial={serial}) for contraction")
    ctx.invoke(stop_node, force=False, dry_run=False, node_spec=fqdn)


@node.command(name="autoscale")
@click.option(
    "-N", "--dry-run",
    is_flag=True,
    help="Read-only mode: log the plan but make no changes.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the AUTOSCALE_ENABLED kill-switch and scale-down cooldown "
         "for a one-off manual run (AUTOSCALE_DRY_RUN still applies).",
)
@click.option(
    "--up-only/--down-only",
    "up_only",
    default=None,
    help="Limit to scale-up-only or scale-down-only actions.",
)
@click.pass_context
def autoscale_cmd(ctx, dry_run: bool, force: bool, up_only):
    """Run one autoscale cycle: assess cluster demand and scale up or down.

    Respects AUTOSCALE_ENABLED (kill-switch) and AUTOSCALE_DRY_RUN (global
    dry-run override). Safe to run from cron: exits cleanly when disabled.

    Use --force to run a one-off cycle even when AUTOSCALE_ENABLED=false
    (it also bypasses the scale-down cooldown). AUTOSCALE_DRY_RUN still
    applies, so pair --force with a config where AUTOSCALE_DRY_RUN=false
    (or run --dry-run first) to control whether mutations actually occur.
    """
    from cspawn.cs_docker.autoscale import run_autoscale
    result = run_autoscale(ctx, dry_run=dry_run, force=force, up_only=up_only)
    click.echo(result.summary())


# ---------------------------------------------------------------------------
# op-run — detached subprocess worker for admin-triggered node operations
# ---------------------------------------------------------------------------

@node.command(name="op-run")
@click.argument("op_id")
@click.pass_context
def op_run(ctx, op_id: str):
    """Execute a pending NodeOp by ID (called as a detached subprocess by the admin UI).

    Loads the NodeOp from the database, acquires an exclusive file lock to
    serialise concurrent node operations, redirects stdout/stderr to the op's
    log file, invokes the appropriate existing command (expand or stop), and
    updates the NodeOp status on completion.
    """
    import fcntl
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    from cspawn.cli.util import get_app
    from cspawn.models import NodeOp, db

    cfg = get_config()
    data_dir = cfg.get("DATA_DIR", "/tmp")

    app = get_app(ctx)

    # ---- 1. Load op and set running ----------------------------------------
    with app.app_context():
        op = db.session.get(NodeOp, op_id)
        if op is None:
            raise click.ClickException(f"NodeOp {op_id!r} not found in database")

        # Compute log path and create directory before acquiring lock so the
        # directory is ready when we redirect output.
        log_dir = Path(data_dir) / "node-ops"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{op_id}.log"

        op.log_path = str(log_path)
        op.status = "running"
        op.started_at = datetime.now(timezone.utc)
        db.session.commit()

    # ---- 2. Redirect stdout/stderr AND attach a logging FileHandler ---------
    # Two separate output paths must both be captured into the op log:
    #   * click.echo()/print() → go through sys.stdout, so reassign it.
    #   * log.info()/log.warning() → go through the logging StreamHandler that
    #     basicConfig bound to the ORIGINAL sys.stderr at import time. Reassigning
    #     sys.stderr does NOT redirect them (the handler holds its own ref), so
    #     without a dedicated FileHandler all logging output (which is most of
    #     expand's narration, and ALL of a failed op's diagnostics) is lost —
    #     the log file ends up 0 bytes. Attach a FileHandler to the root logger
    #     so every module logger (cspawn.cli, cspawn.docker, ...) is captured.
    log_file = open(log_path, "w")  # noqa: WPS515
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    sys.stdout = log_file
    sys.stderr = log_file

    _root_logger = logging.getLogger()
    _file_handler = logging.FileHandler(str(log_path))
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _root_logger.addHandler(_file_handler)
    # Ensure INFO records actually reach the handler even if the root level is higher.
    _prev_root_level = _root_logger.level
    if _root_logger.level > logging.INFO or _root_logger.level == logging.NOTSET:
        _root_logger.setLevel(logging.INFO)

    # ---- 3. Acquire exclusive non-blocking flock ----------------------------
    lock_path = Path(data_dir) / ".node-ops.lock"
    lock_file = open(lock_path, "w")  # noqa: WPS515
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        with app.app_context():
            op = db.session.get(NodeOp, op_id)
            if op is not None:
                op.status = "failed"
                op.exit_code = 1
                op.message = "another node operation is in progress"
                op.finished_at = datetime.now(timezone.utc)
                db.session.commit()
        log_file.write("another node operation is in progress\n")
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr
        log_file.close()
        lock_file.close()
        return

    # ---- 4. Execute operation and update status -----------------------------
    exc_message: str | None = None
    success = False
    try:
        with app.app_context():
            op = db.session.get(NodeOp, op_id)
            kind = op.kind if op is not None else None
            tier = op.tier if op is not None else None
            target_fqdn = op.target_fqdn if op is not None else None

        if kind == "expand":
            # _create_droplet's optional NodeOp write-back needs an active app
            # context; op_run's other invocations don't create droplets and so
            # don't need one here (each still opens its own context as needed).
            with app.app_context():
                ctx.invoke(expand, tier_name=tier, node_op_id=op_id)
        elif kind == "remove":
            ctx.invoke(stop_node, node_spec=target_fqdn, force=False, dry_run=False)
        elif kind == "rebalance":
            ctx.invoke(rebalance, dry_run=False, no_push=False, max_moves=None)
        else:
            raise click.ClickException(f"Unknown NodeOp kind: {kind!r}")

        success = True
    except Exception as exc:
        exc_message = str(exc)
        # Make sure the failure is in the log itself, not only in op.message —
        # logging may have already captured the traceback, but a top-level
        # ClickException raised after retries is the headline the operator needs.
        try:
            logging.getLogger("cspawn.cli").error("Node operation failed: %s", exc_message)
        except Exception:
            pass
    finally:
        # Detach the FileHandler and restore the root log level.
        try:
            _file_handler.flush()
            _root_logger.removeHandler(_file_handler)
            _file_handler.close()
            _root_logger.setLevel(_prev_root_level)
        except Exception:
            pass

        # Restore stdout/stderr before releasing lock (so any final prints work)
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr

        # Release flock
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass

        # Update final status
        with app.app_context():
            op = db.session.get(NodeOp, op_id)
            if op is not None:
                if success:
                    op.status = "done"
                    op.exit_code = 0
                else:
                    op.status = "failed"
                    op.exit_code = 1
                    op.message = exc_message
                op.finished_at = datetime.now(timezone.utc)
                db.session.commit()

        log_file.close()
