"""
Load-test fixtures: create a class + 20 test students, start their code-server
hosts in parallel while timing each, report distribution/performance, and tear
everything down (hosts, GitHub forks, students, class).

    cspawnctl -d local-prod test setup
    cspawnctl -d local-prod test start
    cspawnctl -d local-prod test report
    cspawnctl -d local-prod test teardown
"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from statistics import mean

import click

from cspawn.init import cast_app
from cspawn.models import Class, ClassProto, CodeHost, User, db
from cspawn.util.app_support import set_role_from_email

from .root import cli
from .util import get_app, get_logger

N_STUDENTS = 20
TEST_USERNAME_FMT = "teststudent{:02d}"
TEST_EMAIL_FMT = "teststudent{:02d}@students.jointheleague.org"
TEST_CLASS_CODE = "loadtest"
TEST_CLASS_NAME = "Load Test Class"

PROTO_NAME = "Python Apprentice"
PROTO_IMAGE = "ghcr.io/league-infrastructure/league-infrastructure/docker-codeserver-python:v1.20250916.2"
PROTO_REPO = "https://github.com/league-curriculum/Python-Apprentice"


@cli.group()
def test():
    """Create and tear down load-test fixtures (students, class, hosts)."""
    pass


def _iter_test_users():
    """Yield (username, email) for each test student."""
    for n in range(1, N_STUDENTS + 1):
        yield TEST_USERNAME_FMT.format(n), TEST_EMAIL_FMT.format(n)


def _get_or_create_proto(app) -> ClassProto:
    """Find the Python Apprentice proto, creating it if absent."""
    proto = ClassProto.query.filter_by(name=PROTO_NAME).first()
    if not proto:
        proto = ClassProto.query.filter_by(repo_uri=PROTO_REPO).first()
    if proto:
        return proto

    proto = ClassProto(
        name=PROTO_NAME,
        image_uri=PROTO_IMAGE,
        repo_uri=PROTO_REPO,
        is_public=True,
        creator_id=0,
    )
    ClassProto.set_hash(None, None, proto)
    db.session.add(proto)
    db.session.commit()
    return proto


def _get_or_create_class(app, proto: ClassProto) -> Class:
    """Find the load-test class by class_code, creating it if absent."""
    class_ = Class.query.filter_by(class_code=TEST_CLASS_CODE).first()
    if class_:
        return class_

    now = datetime.now(timezone.utc)
    class_ = Class(
        name=TEST_CLASS_NAME,
        proto_id=proto.id,
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(days=1),
        active=True,
        class_code=TEST_CLASS_CODE,
    )
    db.session.add(class_)
    db.session.commit()
    return class_


@test.command()
@click.pass_context
def setup(ctx):
    """Create the load-test class and 20 test students (idempotent)."""
    logger = get_logger(ctx)
    app = cast_app(get_app(ctx))

    with app.app_context():
        proto = _get_or_create_proto(app)
        class_ = _get_or_create_class(app, proto)

        created = 0
        existed = 0
        enrolled = 0

        for username, email in _iter_test_users():
            user = User.query.filter_by(username=username).first()
            if user:
                existed += 1
            else:
                user = User(
                    user_id=str(uuid.uuid4()),
                    username=username,
                    email=email,
                    password="password",
                    is_student=True,
                )
                set_role_from_email(app, user)
                db.session.add(user)
                created += 1

            if user not in class_.students:
                class_.students.append(user)
                enrolled += 1

        db.session.commit()

        logger.info("Test setup complete")
        click.echo(
            f"Class '{class_.name}' (code={class_.class_code}, id={class_.id}) "
            f"using proto '{proto.name}'."
        )
        click.echo(f"Students: {created} created, {existed} already present, {enrolled} newly enrolled.")


def _percentile(values, pct):
    """Return the pct-th percentile of a list (nearest-rank), or None if empty."""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def _start_one(app, user_id, proto_id, class_id, wait, timeout):
    """Start a single host. Runs in its own thread with its own app context."""
    import time

    result = {"username": None, "ok": False, "err": None,
              "create_s": None, "ready_s": None, "node_name": None}
    with app.app_context():
        try:
            user = User.query.get(user_id)
            proto = ClassProto.query.get(proto_id)
            class_ = Class.query.get(class_id)
            username = user.username if user else f"id={user_id}"
            result["username"] = username

            if app.csm.get_by_username(username):
                result["ok"] = True
                result["err"] = "already running"
                return result

            t0 = time.monotonic()
            s, ch = app.csm.new_cs(user=user, proto=proto, class_=class_)
            if not s:
                result["err"] = "new_cs returned no service"
                return result
            result["create_s"] = round(time.monotonic() - t0, 2)

            # Release the DB connection back to the pool before the long
            # readiness poll — otherwise 20 worker threads each hold a session
            # for the full timeout and exhaust the SQLAlchemy QueuePool.
            db.session.remove()

            if wait:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if s.is_ready:
                        result["ready_s"] = round(time.monotonic() - t0, 2)
                        break
                    time.sleep(2)

            s.sync_to_db()
            ch = CodeHost.query.filter_by(service_name=username).first()
            result["node_name"] = ch.node_name if ch else None
            result["ok"] = True
        except Exception as e:  # surface, don't swallow
            result["err"] = str(e)
        finally:
            # Always return the scoped session's connection to the pool.
            db.session.remove()
    return result


@test.command()
@click.option("-c", "--concurrency", default=N_STUDENTS, show_default=True,
              help="Max concurrent host starts.")
@click.option("--no-wait", is_flag=True, help="Don't poll for readiness after create.")
@click.option("--timeout", default=90, show_default=True, help="Readiness poll timeout (s).")
@click.pass_context
def start(ctx, concurrency, no_wait, timeout):
    """Start hosts for all test students in parallel, timing each."""
    logger = get_logger(ctx)
    app = cast_app(get_app(ctx))

    with app.app_context():
        proto = _get_or_create_proto(app)
        class_ = Class.query.filter_by(class_code=TEST_CLASS_CODE).first()
        if not class_:
            raise click.ClickException("No load-test class found. Run 'test setup' first.")
        proto_id, class_id = proto.id, class_.id
        user_ids = [
            u.id for u in (
                User.query.filter_by(username=TEST_USERNAME_FMT.format(n)).first()
                for n in range(1, N_STUDENTS + 1)
            ) if u
        ]

    if not user_ids:
        raise click.ClickException("No test students found. Run 'test setup' first.")

    logger.info("Starting %d hosts (concurrency=%d, wait=%s)", len(user_ids), concurrency, not no_wait)
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [
            ex.submit(_start_one, app, uid, proto_id, class_id, not no_wait, timeout)
            for uid in user_ids
        ]
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            click.echo(
                f"  {r['username']}: "
                f"{'ok' if r['ok'] else 'FAIL'} "
                f"create={r['create_s']}s ready={r['ready_s']}s "
                f"node={r['node_name'] or '-'}"
                + (f" ({r['err']})" if r['err'] else "")
            )

    _print_metrics(results)


def _print_metrics(results):
    """Print latency, node distribution, and failures from start results."""
    from tabulate import tabulate

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    creates = [r["create_s"] for r in ok if r["create_s"] is not None]
    readys = [r["ready_s"] for r in ok if r["ready_s"] is not None]

    click.echo("\n=== Latency ===")
    rows = []
    for label, vals in [("create (s)", creates), ("ready (s)", readys)]:
        if vals:
            rows.append([label, len(vals), round(min(vals), 2), round(mean(vals), 2),
                         round(_percentile(vals, 95), 2), round(max(vals), 2)])
        else:
            rows.append([label, 0, "-", "-", "-", "-"])
    click.echo(tabulate(rows, headers=["metric", "n", "min", "mean", "p95", "max"], tablefmt="github"))

    click.echo("\n=== Node distribution ===")
    dist = {}
    for r in ok:
        dist[r["node_name"] or "unknown"] = dist.get(r["node_name"] or "unknown", 0) + 1
    click.echo(tabulate(sorted(dist.items()), headers=["node", "hosts"], tablefmt="github"))

    click.echo(f"\n=== Summary: {len(ok)} ok, {len(failed)} failed ===")
    for r in failed:
        click.echo(f"  FAIL {r['username']}: {r['err']}")


@test.command()
@click.pass_context
def report(ctx):
    """Report distribution, memory, and failures for test hosts."""
    from tabulate import tabulate

    logger = get_logger(ctx)
    app = cast_app(get_app(ctx))

    with app.app_context():
        logger.info("Syncing DB with swarm")
        app.csm.sync(check_ready=True)

        hosts = CodeHost.query.filter(CodeHost.service_name.like("teststudent%")).all()
        if not hosts:
            click.echo("No test hosts found.")
            return

        # Node distribution + per-node memory aggregate
        by_node = {}
        for h in hosts:
            node = h.node_name or "unknown"
            agg = by_node.setdefault(node, {"hosts": 0, "mem": 0})
            agg["hosts"] += 1
            agg["mem"] += h.memory_usage or 0

        click.echo("=== Node distribution / memory ===")
        rows = [[n, a["hosts"], round(a["mem"] / 1024 / 1024) if a["mem"] else 0]
                for n, a in sorted(by_node.items())]
        click.echo(tabulate(rows, headers=["node", "hosts", "mem (MB)"], tablefmt="github"))

        # Failures: not running/ready or MIA
        failures = [h for h in hosts if h.state in ("mia", "unknown") or h.app_state != "ready"]
        click.echo(f"\n=== {len(hosts)} hosts, {len(failures)} not-ready/failed ===")
        for h in failures:
            click.echo(f"  {h.service_name}: state={h.state} app_state={h.app_state}")


@test.command()
@click.option("--keep-students", is_flag=True, help="Only stop hosts; keep students and class.")
@click.option("--keep-repos", is_flag=True, help="Don't delete the students' GitHub forks.")
@click.option("-N", "--dry-run", is_flag=True, help="Only print what would be done.")
@click.pass_context
def teardown(ctx, keep_students, keep_repos, dry_run):
    """Remove test hosts, GitHub forks, students, and the class (idempotent)."""
    logger = get_logger(ctx)
    app = cast_app(get_app(ctx))

    with app.app_context():
        proto = ClassProto.query.filter_by(name=PROTO_NAME).first()
        class_ = Class.query.filter_by(class_code=TEST_CLASS_CODE).first()

        gorg = None
        if not keep_repos and proto:
            try:
                from cspawn.cs_github.repo import GithubOrg
                gorg = GithubOrg.new_org(app)
            except Exception as e:
                logger.warning("Could not init GithubOrg (repos will not be deleted): %s", e)

        stopped = repos_deleted = rows_deleted = users_deleted = 0

        for username, _ in _iter_test_users():
            s = app.csm.get_by_username(username)
            if s:
                if dry_run:
                    click.echo(f"  would stop service {username}")
                else:
                    s.stop()
                stopped += 1

            if gorg and proto:
                if dry_run:
                    click.echo(f"  would delete GitHub fork for {username}")
                else:
                    try:
                        if gorg.remove(proto.repo_uri, username):
                            repos_deleted += 1
                    except Exception as e:
                        logger.warning("Failed to delete repo for %s: %s", username, e)

            ch = CodeHost.query.filter_by(service_name=username).first()
            if ch:
                if dry_run:
                    click.echo(f"  would delete CodeHost row {username}")
                else:
                    db.session.delete(ch)
                rows_deleted += 1

        if not keep_students:
            for username, _ in _iter_test_users():
                user = User.query.filter_by(username=username).first()
                if not user:
                    continue
                if class_ and user in class_.students:
                    if not dry_run:
                        class_.students.remove(user)
                if dry_run:
                    click.echo(f"  would delete user {username}")
                else:
                    db.session.delete(user)
                users_deleted += 1

            if class_:
                if dry_run:
                    click.echo(f"  would delete class {class_.class_code}")
                else:
                    db.session.delete(class_)

        if not dry_run:
            db.session.commit()

        click.echo(
            f"Teardown {'(dry-run) ' if dry_run else ''}— "
            f"hosts stopped: {stopped}, repos deleted: {repos_deleted}, "
            f"CodeHost rows: {rows_deleted}, users: {users_deleted}"
        )
