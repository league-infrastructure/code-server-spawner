import click

from cspawn.cli.root import cli
from cspawn.cli.util import get_config, get_app
from cspawn.__version__ import __version__ as cspawn_version


@cli.group()
def config():
    """Configuration commands."""
    pass


@config.command()
@click.pass_context
def show(ctx):
    """Show the configuration."""

    config = get_config()

    print("Version:", cspawn_version)
    print("Configuration:")
    for e in config["__CONFIG_PATH"]:
        print(" " * 4, e)
    pass

    app = get_app(ctx)

    print("Database")
    with app.app_context():
        print("    Postgres: ", str(app.db.engine.url))

        try:
            print("    Mongo:    ", str(app.mongo.cx))
        except Exception as e:
            pass


@config.command()
def version():
    """Show the version number."""
    pass
