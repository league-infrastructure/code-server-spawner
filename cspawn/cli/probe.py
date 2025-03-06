import click

from cspawn.models import HostImage
from cspawn.models import CodeHost, User

from .root import cli
from .util import get_app, get_logger, load_data, make_data


@cli.group()
def probe():
    """Run probes to collect container information."""
    pass


@probe.command()
@click.pass_context
def run(ctx):
    """Run probes."""

    app = get_app(ctx)
    logger = get_logger(ctx)
    for c in app.csm.collect_containers(generate=True):
        print(c["service_name"])
        print(c)

        with app.app_context():
            code_host = CodeHost.query.filter_by(service_id=c["service_id"]).first()
            if code_host:

                code_host.service_name = c["service_name"]
                code_host.container_id = c["container_id"]
                code_host.node_id = c["node_id"]
                code_host.state = c["state"]
                app.db.session.commit()
            else:
                username = c["labels"].get("jtl.codeserver.username")
                user = (
                    User.query.filter_by(username=username).first()
                    if username
                    else User.query.get(1)
                )

                image_uri = c["image_uri"]
                host_image = HostImage.query.filter_by(
                    image_uri=image_uri
                ).first()  # Might not get the right repo_uri

                user = User.query.get(1)
                new_code_host = CodeHost(
                    service_id=c["service_id"],
                    service_name=c["service_name"],
                    container_id=c["container_id"],
                    node_id=c["node_id"],
                    host_image=host_image,
                    state=c["state"],
                    user=user,
                )

                app.db.session.add(new_code_host)
                app.db.session.commit()


@probe.command()
@click.option("--mem", is_flag=True, help="Collect memory usage information.")
def mem(mem):
    pass
