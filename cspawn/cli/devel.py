import click

from .root import cli
from cspawn.init import init_app, resolve_deployment


@cli.group()
def devel():
    """Manage nodes in the cluster."""
    pass


@devel.command()
@click.option("-D", "--debug", is_flag=True, default=False, help="Enable debug mode.")
@click.pass_context
def run(ctx, debug):
    """Run the development server."""
    deployment = ctx.obj["deploy"]
    log_level = None
    if ctx.obj["v"] == 1:
        log_level = "INFO"
    elif ctx.obj["v"] >= 2:
        log_level = "DEBUG"
    app = init_app(config_dir=ctx.obj["config_file"], deployment=deployment, log_level=log_level)
    app.run(debug=debug, host="0.0.0.0")
