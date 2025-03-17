import click

from cspawn.cli.util import get_logger


@click.group()
@click.option("-v", count=True, help="Set INFO (-v) or DEBUG (-vv) level on loggers.")
@click.option("-c", "--config-file", type=click.Path(exists=True), help="Load only the config file.")
@click.option("-d", "--deploy", default=None, help="deployment name for configuration, either devel or prod")
@click.pass_context
def cli(ctx, v, deploy, config_file):
    """cspawnctl - A command-line tool for managing Docker services."""

    ctx.obj = {}
    ctx.obj["v"] = v
    ctx.obj["config_file"] = config_file
    ctx.obj["deploy"] = deploy

    get_logger(ctx)
