import click

from .root import cli
from .util import get_app, load_data, make_data


@cli.group()
def telem():
    """Telemetry commands."""
    pass


@telem.command()
@click.pass_context
def summary(ctx):
    """Show a summary of telemetry data."""
    from itertools import islice

    app = get_app(ctx)

    a = [e.service_name for e in app.csm.repo.all]

    for r in islice(app.csm.keyrate.summarize_latest(a), 100):
        print(r)


@telem.command()
@click.pass_context
def count(ctx):
    """Count the number of telemetry records."""
    app = get_app(ctx)
    count = len(app.csm.keyrate)
    print(f"Total telemetry records: {count}")


@telem.command()
@click.pass_context
def purge(ctx):
    """Purge all telemetry data."""
    app = get_app(ctx)
    app.csm.keyrate.delete_all()
    print("All telemetry data purged successfully")
