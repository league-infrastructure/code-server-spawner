import click

from .root import cli
from .util import get_app, load_data, make_data


@cli.group()
def node():
    """Manage nodes in the cluster."""
    pass


@node.command()
@click.option("-d", "--drain", required=True, help="Drain the node named <node-name>.")
def drain(drain):
    pass


@node.command()
@click.option("-a", "--add", required=True, help="Add a new node to the cluster.")
def add(add):
    pass


@node.command()
@click.option("-r", "--rm", required=True, help="Remove a node from the cluster.")
def rm(rm):

    app = get_app()
