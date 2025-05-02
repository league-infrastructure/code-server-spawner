import click

from .root import cli
from .util import get_app


@cli.group()
def sys():
    """System level commands."""
    pass


@sys.command()
@click.pass_context
def shutdown(ctx):
    """Shutdown the system."""
    app = get_app(ctx)
    app.csm.remove_all()
    print("System shutdown initiated.")


@sys.command()
@click.pass_context
def restart(ctx):
    """Restart the system."""
    app = get_app(ctx)

    print("System restart initiated. haha jk")


@sys.command()
@click.pass_context
def events(ctx):
    """display docker events"""
    app = get_app(ctx)

    for e in app.csm.events:
        print(e)
