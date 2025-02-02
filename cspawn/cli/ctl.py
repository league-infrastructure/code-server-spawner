import click
import logging
from cspawn.init import init_app
from functools import lru_cache
import pandas

logging.basicConfig(level=logging.ERROR)

from cspawn.control import logger as ctrl_logger

logger = logging.getLogger(__name__)

def get_logging_level(ctx): 
    
    v = ctx.obj['v']
    
    log_level = None
    if v == 0:
        log_level = logging.ERROR
    if v == 1:
        log_level = logging.INFO
    elif v >= 2:
        log_level = logging.DEBUG
    else:
        log_level = logging.ERROR

    return log_level

_app = None

@lru_cache
def get_app(ctx):
    global _app
    if _app is None:
        log_level = get_logging_level(ctx)
        _app = init_app(log_level=log_level)
    
    return _app

def get_logger(ctx):
    log_level = get_logging_level(ctx)
   
    ctrl_logger.setLevel(log_level)
    logger.setLevel(log_level)
    return logger

@click.group()
@click.option('-v', count=True, help="Set INFO (-v) or DEBUG (-vv) level on loggers.")
@click.option('-c', '--config-file', type=click.Path(exists=True), help="Load only the config file.")
@click.pass_context
def cli(ctx, v, config_file):
    """cspawnctl - A command-line tool for managing Docker services."""

    ctx.obj = {}
    ctx.obj['v'] = v
    ctx.obj['config_file'] = config_file
    ctx.obj['app'] = get_app(ctx)
    ctx.obj['logger'] = get_logger(ctx)

@cli.group()
def config():
    """Configuration commands."""
    pass

@config.command()
@click.pass_context
def show(ctx):
    """Show the configuration."""
    
    app =  get_app(ctx)
    for e in app.app_config['__CONFIG_PATH']:
        print(e)
    pass

@config.command()
def version():
    """Show the version number."""
    pass

@cli.group()
def dctl():
    """Docker control commands."""
    pass

@dctl.command()
def ls():
    """List all of the Docker containers in the system."""
    from tabulate import tabulate
    
    app =  get_app()
    
    rows = []
    for s in app.csm.list():
        for c in s.containers_info():
            rows.append({
                'service': c['service_name'],
                'state': c['state'],
                'node_id': c['node_id'],
                'hostname': c['hostname'],
               
            })
    
    print(tabulate(rows, headers="keys"))
        
@dctl.group()
def node():
    """Manage nodes in the cluster."""
    pass

@node.command()
@click.option('-d', '--drain', required=True, help="Drain the node named <node-name>.")
def drain(drain):
    pass

@node.command()
@click.option('-a', '--add', required=True, help="Add a new node to the cluster.")
def add(add):
    pass

@node.command()
@click.option('-r', '--rm', required=True, help="Remove a node from the cluster.")
def rm(rm):
    
    app =  get_app()
    

@dctl.command()
@click.argument('service_name')
@click.pass_context
def start(ctx,service_name):
    """Start the specified service."""
    from time import time, sleep
    
    app =  get_app(ctx)

    s = app.csm.new_cs(service_name)
    s.wait_until_ready(timeout=60)



@dctl.command()
@click.argument('service_name')
@click.pass_context
def stop(ctx,service_name):
    """Stop the specified service."""
    from docker.errors import NotFound
    
    app = get_app(ctx)
    try:
        s = app.csm.get(service_name)
        s.stop()
        print(f"Service {service_name} stopped successfully")
    except NotFound:
        print(f"Service {service_name} not found")



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

@probe.command()
@click.option('--prune', is_flag=True, help="Find and shut down unused services.")
def prune(prune):
    pass

if __name__ == '__main__':
    cli()
