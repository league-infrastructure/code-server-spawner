import click

from .root import cli
from .util import get_app, load_data, make_data
from docker.errors import NotFound


@cli.group()
def host():
    """Start, stop and find code-server hosts"""
    pass


@host.command()
@click.pass_context
def ls(ctx):
    """List all of the Docker containers in the system."""
    from tabulate import tabulate

    app = get_app(ctx)

    rows = []
    for s in app.csm.list():
        for c in s.containers_info():
            rows.append(
                {
                    "service": c["service_name"],
                    "state": c["state"],
                    "node_id": c["node_id"],
                    "hostname": c["hostname"],
                }
            )

    print(tabulate(rows, headers="keys"))


@host.command()
@click.argument("service_name")
@click.option("--no-wait", is_flag=True, help="Do not wait for the service to be ready.")
@click.pass_context
def start(ctx, service_name, no_wait):
    """Start the specified service."""
    from time import sleep, time

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
    """Delete all proble records"""
    app = get_app(ctx)

    confirmation = input("Are you sure? Type 'yes' to proceed: ")
    if confirmation.lower() != "yes":
        print("Operation cancelled.")
        return

    for s in app.csm.list():
        s.remove()

    print("All services removed successfully.")

    app.csm.collect_containers()

    print("Cleaned database.")
