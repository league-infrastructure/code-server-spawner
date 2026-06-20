"""Full user teardown: stop code servers, delete GitHub repos, delete the user.

This isolates the destructive orchestration behind delete-user. The teardown is
run synchronously and uses a continue-and-collect strategy: a failure in one
step is recorded and the teardown proceeds, so a single bad host or repo does
not orphan the rest. The user DB record is only deleted if servers and repos
were confirmed gone (unless ``force=True``), so a partially-failed teardown
leaves the user discoverable for retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from cspawn.models import CodeHost, User, db


@dataclass
class TeardownReport:
    """Summary of what a full-delete attempted and what happened."""

    username: str
    servers_stopped: List[str] = field(default_factory=list)
    repos_deleted: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    user_deleted: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures


def _stop_user_servers(app, user: User, report: TeardownReport) -> None:
    """Stop + remove every CodeHost belonging to the user and its swarm service."""
    hosts: List[CodeHost] = list(CodeHost.query.filter_by(user_id=user.id).all())

    for ch in hosts:
        name = ch.service_name or ch.service_id or f"host:{ch.id}"
        # Stopping tunnels to the swarm node and can fail if the node is
        # unreachable. Don't let one bad host abort the batch; still delete the
        # DB record so the orphan doesn't linger.
        try:
            s = app.csm.get(ch)
            if s:
                s.stop()
        except Exception as e:  # noqa: BLE001 - report, don't abort
            report.failures.append(f"stop server {name}: {e}")
        try:
            db.session.delete(ch)
            db.session.commit()
            report.servers_stopped.append(name)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            report.failures.append(f"delete host record {name}: {e}")


def _delete_user_repos(app, user: User, report: TeardownReport) -> None:
    """Delete all of the user's repos under GITHUB_ORG.

    Enumerates org repos and matches the ``-{username}`` suffix convention used
    by GithubOrg.fork(), so it catches repos for any class/prototype the user
    forked, not just ones still tracked by a CodeHost.
    """
    from cspawn.cs_github.repo import GithubOrg

    try:
        org = GithubOrg.new_org(app)
    except Exception as e:  # noqa: BLE001 - GitHub not configured / unreachable
        report.failures.append(f"github org init: {e}")
        return

    suffix = f"-{user.username}"
    try:
        candidates = [
            r for r in org._org_obj.get_repos() if r.name.endswith(suffix)
        ]
    except Exception as e:  # noqa: BLE001
        report.failures.append(f"list github repos: {e}")
        return

    for repo in candidates:
        full = f"{org.org}/{repo.name}"
        try:
            if org.remove(full):
                report.repos_deleted.append(full)
        except Exception as e:  # noqa: BLE001
            report.failures.append(f"delete repo {full}: {e}")


def teardown_user(app, user: User, force: bool = False) -> TeardownReport:
    """Fully delete a user: servers, then repos, then the DB record.

    Args:
        app: the cspawn App (provides ``csm`` and ``app_config``).
        user: the User to delete.
        force: if True, delete the user record even when earlier steps failed.

    Returns:
        TeardownReport describing what was cleaned up and any failures.
    """
    report = TeardownReport(username=user.username or f"id:{user.id}")

    _stop_user_servers(app, user, report)
    _delete_user_repos(app, user, report)

    # Only delete the user record if everything else is confirmed gone, so a
    # partially-failed teardown leaves the user discoverable for retry.
    if report.ok or force:
        try:
            db.session.delete(user)
            db.session.commit()
            report.user_deleted = True
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            report.failures.append(f"delete user record: {e}")

    return report
