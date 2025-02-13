import click

from cspawn.cli.root import cli
from cspawn.cli.util import get_config


@cli.group()
def config():
    """Configuration commands."""
    pass

@config.command()
@click.pass_context
def show(ctx):
    """Show the configuration."""

    config = get_config()

    for e in config['__CONFIG_PATH']:
        print(e)
    pass


@config.command()
def version():
    """Show the version number."""
    pass