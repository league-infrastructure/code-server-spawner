import click

from .root import cli
from .util import make_data, load_data, get_app


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
