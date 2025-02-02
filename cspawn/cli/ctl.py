import click
import logging
from cspawn.init import init_app
from functools import lru_cache
import pandas
logger = logging.getLogger(__name__)

def get_logging_level():
    ctx = click.get_current_context()
    v = ctx.parent.params.get("v", 0)
    
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

@lru_cache
def get_app():
    log_level = get_logging_level()
    return init_app(log_level=log_level)

def get_logger():
    log_level = get_logging_level()
    
    logger.setLevel(log_level)
    return logger

@click.group()
@click.option('-v', count=True, help="Set INFO (-v) or DEBUG (-vv) level on loggers.")
@click.option('-c', '--config-file', type=click.Path(exists=True), help="Load only the config file.")
def cli(v, config_file):
    """cspawnctl - A command-line tool for managing Docker services."""

@cli.group()
def config():
    """Configuration commands."""
    pass

@config.command()
def show():
    """Show the configuration."""
    
    app =  get_app()
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
    pass

@dctl.command()
@click.argument('service_name')
def stop(service_name):
    """Stop the specified service."""
    pass

@dctl.command()
@click.argument('service_name')
def rm(service_name):
    """Remove the specified service."""
    pass

@cli.group()
def probe():
    """Run probes to collect container information."""
    pass

@probe.command()
def run():
    """Run probes."""
    pass

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
