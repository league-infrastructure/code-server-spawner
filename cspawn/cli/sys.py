import click

from .root import cli
from .util import get_app


@cli.group()
def sys():
    """System level commands."""
    pass


@sys.command()
@click.option("--no-push", is_flag=True, help="Skip pushing each host's changes to GitHub before shutdown.")
@click.pass_context
def shutdown(ctx, no_push):
    """Shutdown the system.

    By default every host's local changes are pushed to GitHub before it is
    stopped and removed, so no student work is lost. Pass --no-push to skip
    the push and shut down immediately.
    """
    app = get_app(ctx)
    with app.app_context():
        app.csm.remove_all(push=not no_push)
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
