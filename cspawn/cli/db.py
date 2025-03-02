import click
from sqlalchemy import MetaData

from .root import cli
from .util import get_app, load_data, make_data


@cli.group()
def db():
    """Database commands."""
    pass


@db.command()
@click.pass_context
def info(ctx):
    """Info about the database"""
    app = get_app(ctx)

    with app.app_context():

        connection_string = str(app.db.engine.url)
        print(connection_string)


@db.command()
@click.pass_context
def create(ctx):
    """Create all database tables."""

    app = get_app(ctx)
    with app.app_context():
        app.db.create_all()
        print("Database tables created successfully.")


@db.command()
@click.pass_context
def destroy(ctx):
    """Destroy all database tables."""
    app = get_app(ctx)

    with app.app_context():
        db = app.db
        e = db.engine

        m = MetaData()
        m.reflect(e)

        m.drop_all(e)
        print("Database tables destroyed successfully.")


@db.command()
@click.pass_context
def sync(ctx):
    """Sync docker with the database. same as `cspawn host sync`"""
    app = get_app(ctx)

    with app.app_context():
        app.csm.sync(check_ready=True)


@db.command()
@click.option(
    "-d", "--demo", is_flag=True, help="Load demo data after recreating the database."
)
@click.pass_context
def recreate(ctx, demo):
    """Destroy and recreate all database tables."""
    app = get_app(ctx)

    with app.app_context():
        db = app.db
        e = db.engine

        m = MetaData()
        m.reflect(e)
        m.drop_all(e)
        print("Database tables destroyed successfully.")

        db.create_all()
        print("Database tables created successfully.")

        load_data(app)

        if demo:
            make_data(app)
            print("Demo data loaded successfully.")
