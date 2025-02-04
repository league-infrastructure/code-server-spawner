import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path

import click
import pandas
from docker.errors import NotFound
from jtlutil.flask.flaskapp import configure_config_tree

from cspawn.init import init_app

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

@lru_cache
def get_logger(ctx):
    log_level = get_logging_level(ctx)
   
    ctrl_logger.setLevel(log_level)
    logger.setLevel(log_level)
    return logger

@lru_cache
def get_config():
    c =  configure_config_tree()
    
    if len(c['__CONFIG_PATH']) == 0:
        raise Exception("No configuration files found. Maybe you are in the wrong directory?")
    
    return c

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

@cli.group()
def host():
    """Start, stop and find code-server hosts"""
    pass

@host.command()
@click.pass_context
def ls(ctx):
    """List all of the Docker containers in the system."""
    from tabulate import tabulate
    
    app =  get_app(ctx)
    
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
        

    

@host.command()
@click.argument('service_name')
@click.pass_context
def start(ctx,service_name):
    """Start the specified service."""
    from time import sleep, time
    
    app =  get_app(ctx)

    s = app.csm.new_cs(service_name)
    s.wait_until_ready(timeout=60)



@host.command()
@click.argument('service_name')
@click.pass_context
def stop(ctx,service_name):
    """Stop the specified service."""
    
    app = get_app(ctx)
    try:
        s = app.csm.get(service_name)
        s.stop()
        print(f"Service {service_name} stopped successfully")
    except NotFound:
        print(f"Service {service_name} not found")

@host.command()
@click.argument('query')
@click.pass_context
def find(ctx,query):
    """Stop the specified service."""
    
    app = get_app(ctx)
    
    def _f():
        
        try:
            return app.csm.get(query)
        except NotFound:
            pass
        
        if s :=  app.csm.get_by_hostname(query):
            return s
 
        if s:=  app.csm.get_by_username(query):
            return s
        
        return None
    
    s = _f()
 
    print (s)

@host.command()
@click.pass_context
@click.option('--prune', is_flag=True, help="Find and shut down unused services.")
def prune(ctx, prune):
    
    
    from tabulate import tabulate
    
    app =  get_app(ctx)
    
    app.csm.mark_all_unknown()
    


@host.command()
@click.pass_context
@click.option('--purge', is_flag=True, help="Delete all probe records.")
def purge(ctx, purge):
    
    confirmation = input("Are you sure? Type 'yes' to proceed: ")
    if confirmation.lower() != 'yes':
        print("Operation cancelled.")
        return
    
    app =  get_app(ctx)
    app.csm.repo.delete_all()


@cli.group()
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


    
@cli.group()
def telem():
    """Telemetry commands."""
    pass

@telem.command()
@click.pass_context
def summary(ctx):
    """Show a summary of telemetry data."""
    app = get_app(ctx)
    
    

@telem.command()
@click.pass_context
def count(ctx):
    """Count the number of telemetry records."""
    app = get_app(ctx)
    count = len(app.csm.keyrate)
    print(f"Total telemetry records: {count}")

@telem.command()
@click.pass_context
def purge(ctx):
    """Purge all telemetry data."""
    app = get_app(ctx)
    app.csm.keyrate.delete_all()
    print("All telemetry data purged successfully")


@cli.group()
def sys():
    """System level commands."""
    pass

@sys.command()
@click.pass_context
def shutdown(ctx):
    """Shutdown the system."""
    app = get_app(ctx)
    app.csm.remove_all()
    print("System shutdown initiated.")

@sys.command()
@click.pass_context
def restart(ctx):
    """Restart the system."""
    app = get_app(ctx)
    
    print("System restart initiated. haha jk")

@cli.group()
def fs():
    """File system commands."""
    pass

@fs.command()
@click.argument('username')
@click.pass_context
def mkdir(ctx, username):
    """Create a new user directory."""

    app = get_app(ctx)
    app.csm.make_user_dir(username)

@fs.command()
@click.argument('local_dir')
@click.argument('username')
@click.pass_context
def copyin(ctx, local_dir, username):
    """Copy local files into the user directory."""

    config = get_config()
    docker_uri = config.get('DOCKER_URI')
    
    if docker_uri.startswith('unix'):
        user_dir = Path(f"/home/{username}")
        if user_dir.exists():
            shutil.copytree(local_dir, user_dir, dirs_exist_ok=True)
            print(f"Files from {local_dir} copied to {user_dir} successfully.")
        else:
            print(f"User directory {user_dir} does not exist.")
    elif docker_uri.startswith('ssh:'):
        os.system(f"scp -r {local_dir} {docker_uri}:/home/{username}")
        print(f"Files from {local_dir} copied to {docker_uri}:/home/{username} successfully.")
    else:
        print("Unsupported DOCKER_URI scheme for copyin command.")

@fs.command()
@click.argument('username')
@click.argument('local_dir')
@click.pass_context
def copyout(ctx, username, local_dir):
    """Copy files from the user directory to local."""

    config = get_config()
    docker_uri = config.get('DOCKER_URI')
    
    if docker_uri.startswith('unix'):
        user_dir = Path(f"/home/{username}")
        if user_dir.exists():
            shutil.copytree(user_dir, local_dir, dirs_exist_ok=True)
            print(f"Files from {user_dir} copied to {local_dir} successfully.")
        else:
            print(f"User directory {user_dir} does not exist.")
    elif docker_uri.startswith('ssh:'):
        os.system(f"scp -r {docker_uri}:/home/{username} {local_dir}")
        print(f"Files from {docker_uri}:/home/{username} copied to {local_dir} successfully.")
    else:
        print("Unsupported DOCKER_URI scheme for copyout command.")

if __name__ == '__main__':
    cli()
