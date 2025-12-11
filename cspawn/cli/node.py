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


def _ssh_exec(host: str, username: str, key_path: Path, cmd: str, *, connect_timeout: int = 15) -> tuple[int, str, str]:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = paramiko.RSAKey.from_private_key_file(str(key_path))
    try:
        ssh.connect(host, username=username, pkey=pkey, look_for_keys=False, timeout=connect_timeout)
        stdin, stdout, stderr = ssh.exec_command(cmd)
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
    """Return (private_key_path, public_key_path) and ensure private exists."""
    workspace_root = find_parent_dir()
    priv_key_path = Path(workspace_root) / "config" / "secrets" / "id_rsa"
    pub_key_path = Path(workspace_root) / "config" / "secrets" / "id_rsa.pub"
    if not priv_key_path.exists():
        raise click.ClickException(f"SSH private key not found at {priv_key_path}")
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


def _create_droplet(ctx, *, mgr: digitalocean.Manager, manager_client: docker.DockerClient, name_template: str,
                    do_token: str, do_region: str, do_size: str, do_image: str, project_selector: str | None,
                    desired_serial: int | None, docker_uri: str, do_tag: str | None = None) -> tuple[digitalocean.Droplet, str, str, str]:
    """Create droplet for next or specific serial. Idempotent if desired_serial provided.

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
        # Prepare keys
        priv_key_path, pub_key_path = _ensure_priv_key()
        ssh_keys_param = _collect_do_ssh_keys(mgr, do_token, pub_key_path, shortname, log)

        # Optional cloud-init user-data
        user_data = None
        try:
            cfg = get_config()
            cloud_init_file =  cfg.get("DO_CLOUD_INIT_FILE")
            if cloud_init_file:
               
                cip = Path(config['CONFIG_DIR']) / 'cloud-init' / cloud_init_file

                if cip.exists():
                    user_data = cip.read_text()
                    log.info(f"[expand] Including cloud-init user-data from {cip}")
                else:
                    log.warning(f"[expand] CLOUD_INIT_FILE not found at {cip}; proceeding without user-data")
            else:
                log.info("[expand] No CLOUD_INIT_FILE configured; proceeding without user-data")
        except Exception as e:
            log.warning(f"[expand] Error reading CLOUD_INIT_FILE: {e}")

        # Create droplet
        droplet = digitalocean.Droplet(
            token=do_token,
            name=fqdn,
            region=do_region,
            image=do_image,
            size_slug=do_size,
            ssh_keys=ssh_keys_param or None,
            backups=False,
            ipv6=False,
            tags=[do_tag] if do_tag else None,
            user_data=user_data,
        )
        log.info(f"[expand] Creating droplet {fqdn} in {do_region} with size {do_size} and image {do_image}")
        try:
            droplet.create()
        except Exception as e:
            log.error(f"[expand] Droplet creation failed for do token {do_token}: \n{e}")
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


def _join_swarm(ctx, target: str, manager_client: docker.DockerClient, docker_uri: str, ssh_timeout: int | None = None) -> None:
    """Join the node to the swarm as worker. Idempotent."""
    log = get_logger(ctx)
    priv_key_path, _ = _ensure_priv_key()
    manager_host = urlparse(docker_uri).hostname or ""
    if not manager_host:
        raise click.ClickException(f"Unable to parse manager host from DOCKER_URI={docker_uri}")

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

    # Determine worker VPC IP to use for advertise/data path
    code_ip, out_ip, err_ip = _ssh_exec_retry(
        ip,
        "root",
        priv_key_path,
        "ip -o -4 addr show | awk '$4 ~ /10\\.124\\./ {print $4}' | cut -d/ -f1 | head -n1",
        retries=6,
        initial_delay=1.5,
        log=log,
    )
    worker_vpc_ip = (out_ip or err_ip or "").strip() or ip

    # Get worker join token
    try:
        join_token = manager_client.swarm.attrs["JoinTokens"]["Worker"]
    except Exception:
        join_token = manager_client.api.inspect_swarm()["JoinTokens"]["Worker"]
    # Include advertise-addr and data-path-addr for VPC dataplane
    join_cmd = (
        f"docker swarm join --token {join_token} "
        f"--advertise-addr {worker_vpc_ip} --data-path-addr {worker_vpc_ip} "
        f"{manager_host}:2377"
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


@node.command(name="stop")
@click.option("-F", "--force", is_flag=True, help="Force stop immediately (required until drain/remove is implemented)")
@click.option("-N", "--dry-run", is_flag=True, help="Only print what would be done")
@click.argument("node_spec", nargs=1)
@click.pass_context
def stop_node(ctx, force: bool, dry_run: bool, node_spec: str):
    """Stop (destroy) a DigitalOcean node by spec (serial, shortname, or FQDN).

    Initially requires --force; draining/removing from swarm is not implemented yet.
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
        try:
            log.info(f"[stop] Destroying droplet {fqdn} (id={droplet.id})")
            droplet.destroy()
            click.echo(f"Stopped droplet: {fqdn}")
        except Exception as e:
            raise click.ClickException(f"Failed to destroy droplet {fqdn}: {e}")
        return

    # Non-force path: drain -> wait -> remove from swarm -> destroy droplet
    if not docker_uri:
        raise click.ClickException("Missing DOCKER_URI in configuration for graceful stop")
    try:
        manager_client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    short = fqdn.split(".")[0]
    node_obj = _find_swarm_node(manager_client, fqdn, short)

    if dry_run:
        actions = []
        if node_obj:
            actions.append(f"drain swarm node {fqdn}")
            actions.append(f"wait for tasks to drain on {fqdn}")
            actions.append(f"remove swarm node {fqdn}")
        else:
            actions.append(f"(node {fqdn} not in swarm; skip drain/remove)")
        actions.append(f"destroy droplet {fqdn} (id={droplet.id})")
        click.echo("Would perform (in order):")
        for a in actions:
            click.echo(f"  - {a}")
        return

    if node_obj:
        log.info(f"[stop] Draining swarm node {fqdn}")
        _drain_swarm_node(manager_client, node_obj, log=log)
        try:
            _wait_node_tasks_drained(manager_client, node_obj.id, log=log)
        except Exception as e:
            # If timeout, proceed but warn
            log.warning(f"[stop] Proceeding to remove despite drain timeout: {e}")
        try:
            log.info(f"[stop] Removing swarm node {fqdn}")
            try:
                node_obj.remove(force=True)
            except Exception:
                # Try low-level API remove as fallback (Docker SDK 2.0 compatible)
                manager_client.api.remove_node(node_obj.id, force=True)  # type: ignore[attr-defined]
        except Exception as e:
            # It's possible the node is down; proceed to droplet destroy
            log.warning(f"[stop] Failed to remove node cleanly: {e}")
    else:
        log.info(f"[stop] Node {fqdn} not found in swarm; skipping drain/remove")

    # Finally, destroy the droplet
    try:
        log.info(f"[stop] Destroying droplet {fqdn} (id={droplet.id})")
        droplet.destroy()
        click.echo(f"Stopped droplet: {fqdn}")
    except Exception as e:
        raise click.ClickException(f"Failed to destroy droplet {fqdn}: {e}")


@node.command()
@click.option("--project", "project_selector", required=False, help="DigitalOcean project name or ID for the new droplet")
@click.option("--create", "create_only", is_flag=True, help="Only create the node; if no --id given, uses next serial")
@click.option("--id", "create_serial", required=False, type=int, help="Serial id to create (used with --create or defaults in all-steps)")
@click.option("--configure", "configure_name", required=False, type=str, help="Only configure the node hostname (by name or IP)")
@click.option("--join", "join_name", required=False, type=str, help="Only join the node to the swarm (by name or IP)")
@click.option("--domains", "domains_only", is_flag=True, help="Only sync domain A records for swarm nodes (create missing, remove stale)")
@click.option("--ssh-timeout", "ssh_timeout_opt", required=False, type=int, help="Seconds to wait for SSH during configure/join (overrides config)")
@click.pass_context
def expand(ctx, project_selector: str | None, create_only: bool, create_serial: int | None, configure_name: str | None, join_name: str | None, domains_only: bool, ssh_timeout_opt: int | None):
    """Provision and/or configure and/or join a node.

    If none of --create/--configure/--join are supplied, performs all three in order.
    """
    log = get_logger(ctx)
    log.info("[expand] Starting node expansion")
    cfg = get_config()

    # Read DigitalOcean config
    do_token = cfg.get("DO_TOKEN")
  
    do_size = cfg.get("DO_SIZE", "s-1vcpu-1gb")
    do_image = cfg.get("DO_IMAGE", "docker-20-04")
    do_region = cfg.get("DO_REGION") or cfg.get("DO_REGIOIN") or "sfo3"
    name_template = cfg.get("DO_NAMES")
    do_tag = cfg.get("DO_TAG")

    if not project_selector and cfg.get("DO_PROJECT"):
        project_selector = cfg.get("DO_PROJECT")
        log.info(f"[expand] Using DO_PROJECT from config: {project_selector}")

    if not (do_token and name_template):
        raise click.ClickException("Missing DO_TOKEN or DO_NAMES in configuration")

    log.info(f"[expand] Using DO region={do_region} size={do_size} image={do_image}")

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
    _join_swarm(ctx, target_for_join, manager_client, docker_uri, ssh_timeout=ssh_timeout_effective)

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


@node.command(name="contract")
@click.option("-N", "--dry-run", is_flag=True, help="Only print what would be done")
@click.pass_context
def contract_node(ctx, dry_run: bool):
    """Shrink the cluster by removing the highest-numbered eligible node.

    Eligibility: hostname matches DO_NAMES pattern and node is not the swarm leader (and not a manager).
    """
    log = get_logger(ctx)
    cfg = get_config()

    docker_uri = cfg.get("DOCKER_URI")
    name_template = cfg.get("DO_NAMES")
    if not (docker_uri and name_template):
        raise click.ClickException("Missing DOCKER_URI or DO_NAMES in configuration")

    # Connect to docker
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    pat = _regex_from_template(name_template)

    # Find highest-numbered non-leader worker node
    selected = None  # tuple(serial:int, fqdn:str)
    try:
        for n in client.nodes.list():

            attrs = n.attrs or {}
            name = ((attrs.get("Description", {}) or {}).get("Hostname") or "").strip()
            log.debug(f"[contract] Examining node: {name}")
            if not name:
                log.debug(f"[contract] Skipping empty node name")
                continue
            m = pat.match(name)
            if not m:
                log.debug(f"[contract] Skipping node name `{name}` not matching pattern {pat}")
                continue
            short = name.split(".")[0]
            role = ((attrs.get("Spec", {}) or {}).get("Role") or "").lower()
            is_leader = bool(((attrs.get("ManagerStatus", {}) or {}).get("Leader")) or False)
            if is_leader:
                log.debug(f"[contract] Skipping leader node: {name}")
                continue
            # Avoid contracting managers even if not leader
            if role == "manager":
                log.debug(f"[contract] Skipping manager node: {name}")
                continue
            try:
                serial = int(m.group(1))
            except Exception as e:
                log.debug(f"[contract] Failed to parse serial from node name {name}: {e}")
                continue
            if (selected is None) or (serial > selected[0]):
                selected = (serial, name)

    except Exception as e:
        raise click.ClickException(f"Failed to list swarm nodes: {e}")

    if not selected:
        click.echo("No eligible node to contract.")
        return

    serial, fqdn = selected
    if dry_run:
        click.echo(f"Would contract by stopping node {fqdn} (serial={serial})")
        return

    log.info(f"[contract] Selected node {fqdn} (serial={serial}) for contraction")
    # Reuse stop flow (non-force, not dry-run)
    ctx.invoke(stop_node, force=False, dry_run=False, node_spec=fqdn)
