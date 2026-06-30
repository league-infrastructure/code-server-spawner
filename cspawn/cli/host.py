
from bson import Code
import click
from typing import cast
import time

from cspawn.models import CodeHost
from cspawn.init import cast_app

from .root import cli
from .util import get_app, get_logger
from docker.errors import NotFound
from typing import cast

@cli.group()
def host():
    """Start, stop and find code-server hosts"""
    pass


@host.command()
@click.pass_context
def ls(ctx):
    """List all of the Docker containers in the system."""
    from tabulate import tabulate

    logger = get_logger(ctx)

    app = cast_app(get_app(ctx))

    with app.app_context():
        table_data = []
        for s in app.csm.list():
            s.sync_to_db()
            ch: CodeHost = CodeHost.query.filter_by(service_name=s.name).first()
            if not ch:
                logger.warning(f"CodeHost not found for {s.name}")
                continue
            
            table_data.append([
                ch.service_name or 'N/A',
                ch.state or 'N/A',
                ch.app_state or 'N/A',
                ch.node_name or 'N/A',
                round(ch.user_activity_rate, 3) if ch.user_activity_rate is not None else None,
                ch.modified_ago,
                ch.heart_beat_ago,
                '✓' if ch.is_quiescent else '',
                '✓' if ch.is_mia else '',
                '✓' if ch.is_purgeable else ''
            ])

        # Define headers
        headers = [
            'Service',
            'State',
            'App State',
            'Node',
            'Act Rate',
            'Last Act',
            'Last Heart',
            'Quiet',
            'MIA',
            'Purgeable'
        ]

        if table_data:
            print(f"\nCode Hosts ({len(table_data)} hosts):")
            print(tabulate(table_data, headers=headers, tablefmt='grid'))
        else:
            print("No code hosts found.")


@host.command()
@click.argument("service_name")
@click.pass_context
def cont(ctx, service_name):
    """Print the node and container name for the given service."""
    from typing import cast
    from cspawn.cs_docker.csmanager import CodeServerManager
    app = get_app(ctx)

    try:

        s = app.csm.get(service_name)
        s = cast(CodeServerManager, s)

        ci = list(s.containers_info())[0]

        print(f"Service Name: {service_name}")
        print(f"Container Name: {ci['container_id']}")
        print("NodeId:", ci['node_id'])
        print(list(s.containers)[0].o)
    except NotFound:
        print(f"Service {service_name} not found")

@host.command()
@click.argument("service_name")
@click.option("--no-wait", is_flag=True, help="Do not wait for the service to be ready.")
@click.pass_context
def start(ctx, service_name, no_wait):
    """Start the specified service."""

    app = get_app(ctx)

    s = app.csm.new_cs(service_name)
    if not no_wait:
        s.wait_until_ready(timeout=60)


@host.command()
@click.argument("service_name", required=False)
@click.option("-a", "--all", is_flag=True, help="Stop all services.")
@click.pass_context
def stop(ctx, service_name, all):
    """Stop the specified service or all services if --all is provided."""

    app = get_app(ctx)
    if all:
        for s in app.csm.list():
            print(f"Stopping {s.name}")
            s.stop()
        print("All services stopped successfully")
    elif service_name:
        try:
            s = app.csm.get(service_name)
            s.stop()
            print(f"Service {service_name} stopped successfully")
        except NotFound:
            print(f"Service {service_name} not found")
    else:
        print("Please provide a service name or use --all to stop all services.")


@host.command()
@click.argument("query")
@click.pass_context
def find(ctx, query):
    """Stop the specified service."""

    app = get_app(ctx)

    def _f():
        try:
            return app.csm.get(query)
        except NotFound:
            pass

        if s := app.csm.get_by_hostname(query):
            return s

        if s := app.csm.get_by_username(query):
            return s

        return None

    s = _f()

    print(s)


@host.command()
@click.pass_context
@click.option("-N", "--dry-run", is_flag=True, help="Show what would be done, without making any changes.")
def reap(ctx, dry_run: bool):
    """Delete all mia hosts"""
    app = get_app(ctx)

    with app.app_context():
        app.csm.sync(check_ready=True)

        for ch in CodeHost.query.all():
            if ch.is_mia:
                print(ch.service_name + ": ", end=" ")
                if dry_run:
                    print("MIA (would delete)")
                else:
                    app.db.session.delete(ch)
                    print(f"Deleted {ch.service_name}")

        if not dry_run:
            app.db.session.commit()




@host.command()
@click.option("-N", "--dry-run", is_flag=True, help="Show what would be done, without making any changes.")
@click.option("--no-push", is_flag=True, help="Skip pushing each host's changes to GitHub before stopping it.")
@click.pass_context
def purge(ctx, dry_run: bool, no_push: bool):
    """Remove containers that are purgable.

    By default each purgeable host's local changes are pushed to GitHub
    before the host is stopped and deleted, so no student work is lost.
    Pass --no-push to skip the push and purge immediately.
    """
    from datetime import datetime, timezone
    from cspawn.cs_github.repo import CodeHostRepo

    app = get_app(ctx)

    with app.app_context():
        app.csm.sync(check_ready=True)

        for ch in CodeHost.query.all():
            ch = cast(CodeHost, ch)

            if ch.is_mia or ch.is_quiescent:
                print(ch.service_name + ": ", end=" ")

                if not dry_run:
                    # Push the host's work to GitHub before tearing it down,
                    # unless the operator opted out. A push failure must not
                    # abort the purge — log it and still remove the host.
                    if not no_push:
                        try:
                            ch_repo = CodeHostRepo.new_codehostrepo(app, ch.service_name)
                            ch_repo.push()
                            print("(pushed)", end=" ")
                        except Exception as e:
                            print(f"(push failed: {e})", end=" ")

                    # Stopping the service tunnels to the node and can fail if the
                    # node is unreachable. Don't let one bad host abort the batch;
                    # still delete the DB record so the orphan doesn't linger.
                    try:
                        s = app.csm.get(ch)
                        if s:
                            s.stop()
                    except Exception as e:
                        print(f"(stop failed: {e})", end=" ")
                    app.db.session.delete(ch)
                    print(f"Stopped and deleted:   {ch.service_name}")
                else:
                    action = "stop and delete" if no_push else "push, stop and delete"
                    print(f"Would {action}: {ch.service_name}")

        if not dry_run:
            app.db.session.commit()

@host.command()
@click.option("--converge", is_flag=True,
              help="Keep re-syncing hosts in an unknown/transient state until they "
                   "settle (ready or MIA) or the deadline is hit.")
@click.option("--deadline", "deadline_s", type=float, default=90.0, show_default=True,
              help="Max wall-clock seconds for --converge.")
@click.option("--max-passes", type=int, default=8, show_default=True,
              help="Max sync passes for --converge.")
@click.pass_context
def dbsync(ctx, converge: bool, deadline_s: float, max_passes: int):
    """Sync the database with the docker Code Hosts. Same as `cspawnctl db sync`.

    Without flags this is a single reconciliation pass. With --converge it
    repeatedly re-syncs hosts that are still in an unknown/transient state
    (just-created or mid-reschedule) until every host settles to a known state
    (ready or MIA) or the deadline expires — intended to be cron'd so the DB
    self-heals after bursts of host creation or node rebalancing.
    """
    app = get_app(ctx)
    with app.app_context():
        if converge:
            summary = app.csm.sync_converge(deadline_s=deadline_s, max_passes=max_passes)
            click.echo(
                f"Converged in {summary['passes']} pass(es): "
                f"{summary['settled']} settled, {summary['unsettled']} unsettled."
            )
            if summary["unsettled"]:
                click.echo("Still unsettled: " + ", ".join(summary["unsettled_names"]))
        else:
            app.csm.sync(check_ready=True)


@host.command()
@click.argument("username")
@click.option("-n", "--dry-run", is_flag=True, help="Show what would be done, without making any changes.")
@click.pass_context
def sync(ctx, username: str, dry_run: bool):
    """Sync user storage between the code host and storage buckets."""
    app = get_app(ctx)
    from cspawn.util.host_s3_sync import HostS3Sync
    with app.app_context():
        syncer = HostS3Sync(app)
        try:
            syncer.sync_host(username, dry_run=dry_run)
        except Exception as e:
            print(f"Sync failed: {e}")


@host.command()
@click.argument("username", required=False)
@click.option("-a", "--all", "all_hosts", is_flag=True, help="Push every host to GitHub.")
@click.option("--branch", default=None, help="Branch to push.")
@click.option("--timeout", "timeout_s", default=90, show_default=True, type=int,
              help="Per-host timeout in seconds for --all (the docker-over-SSH "
                   "round-trips can stall under node SSH rate-limiting).")
@click.pass_context
def push(ctx, username, all_hosts, branch, timeout_s):
    """Push local changes from a user's code host to GitHub (or all hosts with --all)."""
    app = get_app(ctx)
    from cspawn.cs_github.repo import CodeHostRepo

    with app.app_context():
        if all_hosts:
            # Don't filter by DB state — it drifts (a "shutdown" row is often a
            # live 1/1 service). Collect every non-MIA host's name up front.
            names = [ch.service_name for ch in CodeHost.query.order_by(CodeHost.service_name).all()
                     if not ch.is_mia]
            _push_all(ctx, names, branch, timeout_s)
            return

        if not username:
            print("Provide a username, or use --all to push every host.")
            return

        try:
            ch_repo = CodeHostRepo.new_codehostrepo(app, username)
            ch_repo.push(branch=branch)
            print(f"Push completed for {username} on branch {branch}")
        except Exception as e:
            raise


def _push_all(ctx, names, branch, timeout_s):
    """Push every host, one isolated subprocess per host.

    Each push must be its own process: the docker-over-SSH client caches a
    persistent connection on app.csm, and when the swarm-manager SSH tunnel
    drops (node SSH rate-limiting), that broken pipe poisons every subsequent
    in-process call — so an in-process loop fails the whole batch after the
    first drop. A fresh `cspawnctl host push <name>` per host gets a fresh
    connection, is hard-killable on timeout, and one host's failure can't
    cascade. Failures/timeouts are retried once, then reported.
    """
    import subprocess
    import sys

    deploy = ctx.obj.get("deploy") if isinstance(ctx.obj, dict) else None
    base = [sys.argv[0]]
    if deploy:
        base += ["-d", deploy]
    base += ["host", "push"]
    if branch:
        base += ["--branch", branch]

    print(f"Pushing {len(names)} hosts to GitHub "
          f"(isolated process, {timeout_s}s timeout, 1 retry each)...")

    ok, failed = 0, []
    for name in names:
        result = None
        for attempt in (1, 2):
            try:
                proc = subprocess.run(base + [name], capture_output=True, text=True,
                                      timeout=timeout_s)
                if proc.returncode == 0:
                    result = ("ok", None)
                    break
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                result = ("fail", tail[-1] if tail else f"exit {proc.returncode}")
            except subprocess.TimeoutExpired:
                result = ("timeout", f"timed out after {timeout_s}s")
            if attempt == 1:
                print(f"  … {name}: {result[1]} — retrying")
        status, detail = result
        if status == "ok":
            ok += 1
            print(f"  ✓ {name}")
        elif status == "timeout":
            failed.append((name, detail))
            print(f"  ⏱ {name}: {detail}")
        else:
            failed.append((name, detail))
            print(f"  ✗ {name}: {detail}")

    print(f"\nPush complete: {ok} succeeded, {len(failed)} failed.")
    for name, detail in failed:
        print(f"  failed: {name}: {detail}")
    if failed:
        print("\nRe-run `host push --all` to retry failures "
              "(already-pushed hosts return instantly).")


@host.command()
@click.argument("username")
@click.option("--repo", "repo_url", help="Upstream repo URL or owner/name")
@click.option("--class", "class_id", type=int, help="Class ID to use its prototype repo")
@click.option("--branch", default="master", help="Branch to pull.")
@click.option("--rebase/--no-rebase", default=True, help="Use git pull --rebase.")
@click.option("-n", "--dry-run", is_flag=True, help="Show what would be done, without making any changes.")
@click.pass_context
def pull(ctx, username, repo_url, class_id, branch, rebase, dry_run):
    """Pull changes from GitHub into the user's code host."""
    app = get_app(ctx)
    from cspawn.cs_github.repo import CodeHostRepo
    with app.app_context():
        try:
            ch_repo = CodeHostRepo.new_codehostrepo(app, username)
            ch_repo.pull(branch=branch, rebase=rebase, dry_run=dry_run)
            print(f"Pull completed for {username} on branch {branch}")
        except Exception as e:
            print(f"Pull failed: {e}")


