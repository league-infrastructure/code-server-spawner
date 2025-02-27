import shutil
from pathlib import Path

import click

from .root import cli
from .util import get_config


@cli.group()
def fs():
    """File system commands."""
    pass


@fs.command()
@click.argument("username")
@click.pass_context
def mkdir(ctx, username):
    """Create a new user directory."""

    app = get_app(ctx)
    app.csm.make_user_dir(username)


@fs.command()
@click.argument("local_dir")
@click.argument("username")
@click.pass_context
def copyin(ctx, local_dir, username):
    """Copy local files into the user directory."""

    config = get_config()
    docker_uri = config.get("DOCKER_URI")

    if docker_uri.startswith("unix"):
        user_dir = Path(f"/home/{username}")
        if user_dir.exists():
            shutil.copytree(local_dir, user_dir, dirs_exist_ok=True)
            print(f"Files from {local_dir} copied to {user_dir} successfully.")
        else:
            print(f"User directory {user_dir} does not exist.")
    elif docker_uri.startswith("ssh:"):
        os.system(f"scp -r {local_dir} {docker_uri}:/home/{username}")
        print(f"Files from {local_dir} copied to {docker_uri}:/home/{username} successfully.")
    else:
        print("Unsupported DOCKER_URI scheme for copyin command.")


@fs.command()
@click.argument("username")
@click.argument("local_dir")
@click.pass_context
def copyout(ctx, username, local_dir):
    """Copy files from the user directory to local."""

    config = get_config()
    docker_uri = config.get("DOCKER_URI")

    if docker_uri.startswith("unix"):
        user_dir = Path(f"/home/{username}")
        if user_dir.exists():
            shutil.copytree(user_dir, local_dir, dirs_exist_ok=True)
            print(f"Files from {user_dir} copied to {local_dir} successfully.")
        else:
            print(f"User directory {user_dir} does not exist.")
    elif docker_uri.startswith("ssh:"):
        os.system(f"scp -r {docker_uri}:/home/{username} {local_dir}")
        print(f"Files from {docker_uri}:/home/{username} copied to {local_dir} successfully.")
    else:
        print("Unsupported DOCKER_URI scheme for copyout command.")
