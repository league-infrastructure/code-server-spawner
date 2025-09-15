import click

from cspawn.cli.root import cli
from cspawn.cli.util import get_app
from cspawn.cs_github.repo import GithubOrg
from cspawn.models import ClassProto


@cli.group()
def github():
    """GitHub operations (fork/remove repos in student org)."""
    pass


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
    with app.app_context():
        if class_id:
            proto = ClassProto.query.get(class_id)
            if not proto or not proto.repo_uri:
                raise click.ClickException("Class prototype repo not found")
            repo_url = proto.repo_uri

        org = GithubOrg(config=app.app_config)
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
        org = GithubOrg(config=app.app_config)
        if class_id:
            proto = ClassProto.query.get(class_id)
            if not proto or not proto.repo_uri:
                raise click.ClickException("Class prototype repo not found")
            repo_url = proto.repo_uri

        if not repo_url and not username:
            raise click.UsageError("Provide --repo/--class with USERNAME or a full org/name via --repo")

        ok = org.remove(repo_url, username=username)
        click.echo("deleted" if ok else "not found")
