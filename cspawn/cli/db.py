import click
import subprocess
from sqlalchemy import MetaData
from cspawn.models import export_dict, import_dict, ensure_database_exists
import json
from urllib.parse import urlparse
import os

from .root import cli
from .util import get_app, load_data, make_data


def drop_db(app):
    db = app.db
    e = db.engine

    m = MetaData()
    m.reflect(e)
    m.drop_all(e)


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
        print("Postgres: ", str(app.db.engine.url))

        print("Mongo: ", str(app.mongo.cx))


@db.command()
@click.pass_context
def create(ctx):
    """Create all database tables."""

    app = get_app(ctx)
    with app.app_context():
        ensure_database_exists(app)
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
@click.option("-d", "--demo", is_flag=True, help="Load demo data after recreating the database.")
@click.pass_context
def recreate(ctx, demo):
    """Destroy and recreate all database tables."""
    app = get_app(ctx)

    with app.app_context():
        drop_db(app)
        print("Database tables destroyed successfully.")

        db.create_all()
        print("Database tables created successfully.")

        load_data(app)

        if demo:
            make_data(app)
            print("Demo data loaded successfully.")


@db.command()
@click.option("-f", "--file", help="Load demo data after recreating the database.")
@click.pass_context
def export(ctx, file):
    """Export the database to JSON"""

    app = get_app(ctx)

    d = {}
    with app.app_context():
        d = export_dict()

        if file:
            with open(file, "w") as f:
                f.write(json.dumps(d, indent=4))
        else:
            print(json.dumps(d, indent=4))


@db.command(name="import")
@click.option("-f", "--file", help="Load demo data after recreating the database.")
@click.pass_context
def import_(ctx, file):
    """Import data from a JSON file into the database."""

    app = get_app(ctx)

    with app.app_context():
        ensure_database_exists(app)
        drop_db(app)
        with open(file, "r") as f:
            data = json.load(f)
            import_dict(data)
            print("Data imported successfully.")

        # Update sequence IDs to the max of the id field in each table
        for table in app.db.metadata.tables.values():
            if "id" in table.columns:
                max_id = app.db.session.query(app.db.func.max(table.c.id)).scalar()
                if max_id is not None:
                    sequence_name = f"{table.name}_id_seq"
                    with app.db.engine.connect() as connection:
                        connection.execute(
                            app.db.text(f"SELECT setval(:sequence_name, :max_id)"),
                            {"sequence_name": sequence_name, "max_id": max_id},
                        )
        print("Sequence IDs updated successfully.")


@db.command(name="backup")
@click.option("-f", "--file", default="-", help='Output location of the backup file, or "-" to output to stdout.')
@click.pass_context
def backup(ctx, file):
    """Run pg_dump to backup the database."""
    app = get_app(ctx)
    with app.app_context():
        db_url = urlparse(str(app.app_config["DATABASE_URI"]))
        command = [
            "pg_dump",
            f"--host={db_url.hostname}",
            f"--port={db_url.port}",
            f"--username={db_url.username}",
            f"--dbname={db_url.path[1:]}",  # Remove leading slash
        ]
        if file != "-":
            command.extend(["--file", file])

        # Set the password in the environment
        os.environ["PGPASSWORD"] = db_url.password

        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as e:
            click.echo(f"Error running pg_dump: {e}", err=True)
        finally:
            # Remove the password from the environment
            del os.environ["PGPASSWORD"]


@click.option("-f", "--file", default="-", help='Output location of the backup file, or "-" to output to stdout.')
@click.pass_context
def restore(ctx, file):
    """Run pg_restore to restore the database from a backup file."""
    app = get_app(ctx)
    with app.app_context():
        db_url = urlparse(str(app.app_config["DATABASE_URI"]))
        command = [
            "pg_restore",
            f"--host={db_url.hostname}",
            f"--port={db_url.port}",
            f"--username={db_url.username}",
            f"--dbname={db_url.path[1:]}",  # Remove leading slash
        ]
        if file != "-":
            command.extend(["--file", file])

        # Set the password in the environment
        os.environ["PGPASSWORD"] = db_url.password

        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as e:
            click.echo(f"Error running pg_restore: {e}", err=True)
        finally:
            # Remove the password from the environment
            del os.environ["PGPASSWORD"]


@db.command()
@click.pass_context
def psql(ctx):
    """Open a psql terminal session."""
    app = get_app(ctx)
    with app.app_context():
        db_url = urlparse(str(app.app_config["DATABASE_URI"]))
        command = [
            "psql",
            f"--host={db_url.hostname}",
            f"--port={db_url.port}",
            f"--username={db_url.username}",
            f"--dbname={db_url.path[1:]}",  # Remove leading slash
        ]

        # Set the password in the environment
        os.environ["PGPASSWORD"] = db_url.password

        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as e:
            click.echo(f"Error running psql: {e}", err=True)
        finally:
            # Remove the password from the environment
            del os.environ["PGPASSWORD"]


if __name__ == "__main__":
    db()
