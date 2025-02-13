
import click

from .root import cli
from .util import make_data, load_data, get_app, get_logger


@cli.group()
def probe():
    """Run probes to collect container information."""
    pass

@probe.command()
@click.pass_context
def run(ctx):
    """Run probes."""
    
    app =  get_app(ctx)
    logger = get_logger(ctx)
    for c in app.csm.collect_containers(generate=True):
        print(c['service_name'])

@probe.command()
@click.option('--mem', is_flag=True, help="Collect memory usage information.")
def mem(mem):
    pass
