import click 
from cspawn.cli.util import get_logger


@click.group()
@click.option('-v', count=True, help="Set INFO (-v) or DEBUG (-vv) level on loggers.")
@click.option('-c', '--config-file', type=click.Path(exists=True), help="Load only the config file.")
@click.pass_context
def cli(ctx, v, config_file):
    """cspawnctl - A command-line tool for managing Docker services."""


    ctx.obj = {}
    ctx.obj['v'] = v
    ctx.obj['config_file'] = config_file
    get_logger(ctx)


