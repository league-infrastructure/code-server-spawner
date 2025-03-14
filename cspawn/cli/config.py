import click

from cspawn.cli.root import cli
from cspawn.cli.util import get_config, get_app


@cli.group()
def config():
    """Configuration commands."""
    pass


@config.command()
@click.pass_context
def show(ctx):
    """Show the configuration."""

    config = get_config()

    print("Configuration:")
    for e in config["__CONFIG_PATH"]:
        print(" " * 4, e)
    pass

    app = get_app(ctx)

    print("Database")
    with app.app_context():
        print("    Postgres: ", str(app.db.engine.url))

        print("    Mongo:    ", str(app.mongo.cx))


@config.command()
def version():
    """Show the version number."""
    pass
