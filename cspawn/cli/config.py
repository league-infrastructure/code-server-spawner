import click

from cspawn.cli.root import cli
from cspawn.cli.util import get_config, get_app
from cspawn.__version__ import __version__ as cspawn_version


from psycopg2 import OperationalError

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

 
    try:
        app = get_app(ctx)

        print("Database")
        with app.app_context():
            print("    Postgres: ", str(app.db.engine.url))

            try:
                print("    Mongo:    ", str(app.mongo.cx))
            except Exception as e:
                pass
    except OperationalError as e:
        print("    Postgres: Not connected")


    print("Config items:")
    for key, value in sorted(config.to_dict().items()):
        display_key = (key[:15]) if len(key) > 15 else key.ljust(15)
        print(f"    {display_key}: {value}")



@config.command()
def version():
    """Show the version number."""
    pass
