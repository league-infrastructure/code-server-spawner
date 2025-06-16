import time
from bson import Code
import click
from typing import cast

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
        rows = []
        for s in app.csm.list():
            s.sync_to_db()
            ch: CodeHost = CodeHost.query.filter_by(service_name=s.name).first()
            if not ch:
                logger.warning(f"CodeHost not found for {s.name}")
                continue
            rows.append(
                {
                    "service": ch.service_name,
                    "state": ch.state,
                    "app state": ch.app_state,
                    "node": ch.node_name,
                    "act rate": round(ch.user_activity_rate, 3),
                    "last act": ch.modified_ago,
                }
            )

    print(tabulate(rows, headers="keys"))


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
@click.option("--purge", is_flag=True, help="Delete all probe records.")
def purge(ctx, purge):
    """Delete all mia hosts"""
    app = get_app(ctx)

    with app.app_context():
        app.csm.sync(check_ready=True)

        for ch in CodeHost.query.all():
            if ch.is_mia:
                print(ch.service_name + ": ", end=" ")
                if not purge:
                    print("MIA")
                else:
                    print("Purge", end=" ")
                    app.db.session.delete(ch)
                    print(f"; Deleted {ch.service_name}")

        if purge:
            app.db.session.commit()


@host.command()
@click.pass_context
def sync(ctx):
    """Sync the database with the docker Code Hosts. Same as `cspawn db sync`."""
    app = get_app(ctx)
    with app.app_context():
        app.csm.sync(check_ready=True)


@host.command()
@click.option("-N", "--dry-run", is_flag=True, help="Show what would be done, without making any changes.")

@click.pass_context
def reap(ctx, dry_run: bool):
    """Remove containers that are MIA or are quiescent."""
    from datetime import datetime, timezone

    app = get_app(ctx)

    with app.app_context():
        app.csm.sync(check_ready=True)

        for ch in CodeHost.query.all():
            ch = cast(CodeHost, ch)

            if ch.is_mia or ch.is_quiescent:
                s = app.csm.get(ch)
                print(ch.service_name + ": ", end=" ")

                if ch.is_mia or not s:
                    print("MIA", end=" ")
                elif ch.is_quiescent:
                    print("Quiescent", end=" ")

                if not dry_run:
                    if s:
                        s.stop()
                    app.db.session.delete(ch)
                    print(f"; Stopped and deleted {ch.service_name}")
                else:
                    if ch.is_mia:
                        reason = f"MIA"
                    if ch.is_quiescent:
                        reason = "Quiescent"
                        
                    print(f"; Would stop and delete {ch.service_name}: {reason}")

        if not dry_run:
            app.db.session.commit()
