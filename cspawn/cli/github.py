import click

from cspawn.cli.root import cli
from cspawn.cli.util import get_app
from cspawn.cs_github.repo import GithubOrg
from cspawn.models import ClassProto, Class


@cli.group()
def github():
    """GitHub operations (fork/remove repos in student org)."""
    pass


def get_class_from_classid(ctx, class_id):
    if class_id is None:
        return None
    app = get_app(ctx)
    with app.app_context():
        clazz = Class.query.get(class_id)
        if not clazz:
            raise click.ClickException(f"Class ID {class_id} not found")
        proto = clazz.proto
        if not proto or not proto.repo_uri:
            raise click.ClickException(f"Class prototype repo not found for class ID {class_id}")
        return proto

@github.command()
@click.option("--repo", "repo_url", help="Upstream repo URL or owner/name")
@click.option("--class", "class_id", type=int, help="Class ID to use its prototype repo")
@click.argument("username")
@click.pass_context
def fork(ctx, repo_url, class_id, username):
    """Fork into GITHUB_ORG with -<username> suffix."""

    if not repo_url and not class_id:
        raise click.UsageError("Provide --repo or --class")

    app = get_app(ctx)

    if not repo_url:
        proto = get_class_from_classid(class_id)
        repo_url = proto.repo_uri

    org = GithubOrg(app=app)
    sr = org.fork(repo_url, username)
    click.echo(sr.html_url)


@github.command(name="rm")
@click.option("--repo", "repo_url", help="Upstream repo URL or owner/name")
@click.option("--class", "class_id", type=int, help="Class ID to use its prototype repo")
@click.argument("username", required=False)
@click.pass_context
def remove(ctx, repo_url, class_id, username):
    """Delete student repo. Supports --repo/--class + username or full org/name."""
    app = get_app(ctx)
    with app.app_context():
        org = GithubOrg(app=app)
        if class_id:
            proto = ClassProto.query.get(class_id)
            if not proto or not proto.repo_uri:
                raise click.ClickException("Class prototype repo not found")
            repo_url = proto.repo_uri

        if not repo_url and not username:
            raise click.UsageError("Provide --repo/--class with USERNAME or a full org/name via --repo")

        ok = org.remove(repo_url, username=username)
        click.echo("deleted" if ok else "not found")


@github.command()
@click.option("--repo", "repo_url", help="Upstream repo URL or owner/name")
@click.option("--class", "class_id", type=int, help="Class ID to use its prototype repo")
@click.argument("username")
@click.pass_context

def info(ctx, repo_url, class_id, username):
    """Show info about a student repo (exists, url, etc)."""
    if not repo_url and not class_id:
        raise click.UsageError("Provide --repo or --class")

    app = get_app(ctx)
    if not repo_url:
        proto = get_class_from_classid(ctx, class_id)
        repo_url = proto.repo_uri

    org = GithubOrg(app=app)
    repo_obj = org.get_repo(repo_url, username)
    if repo_obj:
        info = repo_obj.get_info_dict(token=org.token)
    else:
        target_name, _ = org._org_repo_name(repo_url, username)
        repo_full_name = f"{org.org}/{target_name}"
        info = {
            "repo_url": f"https://github.com/{repo_full_name}",
            "exists": False,
            "description": None,
            "private": None,
            "created_at": None,
            "pushed_at": None,
        }
    click.echo(f"Repo: {info['repo_url']}")
    click.echo(f"Exists: {'yes' if info['exists'] else 'no'}")
    if info["exists"]:
        click.echo(f"Description: {info['description']}")
        click.echo(f"Private: {info['private']}")
        click.echo(f"Created at: {info['created_at']}")
        click.echo(f"Last push: {info['pushed_at']}")
