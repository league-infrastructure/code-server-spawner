# Golden node snapshot

Swarm worker nodes are provisioned from a **DigitalOcean snapshot** with
docker-ce pre-installed, instead of installing docker at every boot. This makes
node provisioning fast (~1–2 min vs ~10 min) and removes the boot-time
docker-install failure modes (dpkg-lock races, version drift).

## What is / isn't baked into the snapshot

**Baked in** (built by `scripts/build-golden-node-snapshot.sh`):
- `docker-ce` + `docker-ce-cli`, pinned to the swarm **manager's** version and
  `apt-mark hold`ed (automatic upgrades can't drift it).
- `ufw`, `jq`, the `do-agent` metrics agent, docker helper plugins.
- The sshd `MaxStartups` tuning file.

**Deliberately NOT baked** — and this is the important part:
- **The code-server / codehost image** (`ghcr.io/.../docker-codeserver-python`).
  It is **pre-pulled at node-expand time** (see `cspawn/cli/node.py`), so it is
  always the *current* image the class prototype points at.

  → **Updating the code-server image does NOT require rebuilding this snapshot.**
  A normal `make release` in `docker-codeserver-python` + pointing the class
  prototype at the new tag is all that's needed; the next node expand pulls it.

**Runs at boot** (kept in the slimmed cloud-init, because it's per-node):
- UFW swarm-dataplane rules (they detect the node's VPC iface at boot),
- the swarm join, sshd restart,
- the docker-version pin step — a **no-op** when the snapshot already has the
  right version; it re-pins only if the manager has since moved.

## When to rebuild the snapshot

**Only on a docker MAJOR-version change on the manager.** That's rare and
deliberate. A minor/patch drift is fine — `_verify_node_provisioning` checks
**major** version only (sprint 012), and the boot-time pin step will re-align a
patch difference on its own. If a node ever comes up on the wrong docker major,
the spawner surfaces it loudly (staleness check + post-join verify) rather than
letting it join broken.

You do **not** rebuild it for: code-server image updates, class changes, or
routine OS patching (nodes keep getting security updates; docker-ce is held).

## How to rebuild

```bash
# From the code-server-spawner repo root. Requires DO_TOKEN in .env, doctl,
# and ~/.ssh/id_rsa (matches DO ssh-key cspawn-swarm3).
scripts/build-golden-node-snapshot.sh            # bakes the live manager's docker version
scripts/build-golden-node-snapshot.sh 30.0.1     # or pin an explicit version
```

The script: creates a throwaway builder droplet → installs docker (held) +
prereqs → cleans identity (machine-id, ssh host keys, cloud-init state) →
powers off → snapshots → **destroys the builder** → prints the snapshot id.
It is safe to re-run; each run makes a new, timestamped snapshot.

## How to switch the fleet to a new snapshot

1. Run the build script, note the printed **snapshot id**.
2. Set `DO_IMAGE=<snapshot-id>` in the prod config (dotconfig) and deploy the
   spawner.
3. **Test one node first**: expand a single node, confirm it joins, the
   code-server image pre-pulls, and a host runs on it — *before* the fleet
   depends on it.
4. Delete the previous golden snapshot once the new one is proven (snapshots
   cost storage): `doctl compute snapshot delete <old-id>`.

## Cost

A builder droplet for the ~30–60 min build (a few cents) plus ongoing snapshot
storage (~$0.06/GB·mo; a docker-only snapshot is ~3–4 GB → well under $1/mo).
Keep one or two snapshots around; delete older ones.
