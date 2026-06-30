"""
Unit tests for CodeServerManager.sync_converge — the convergent state sync that
keeps re-syncing hosts in an unknown/transient state until they settle (ready or
MIA) or a deadline is hit.

These tests drive sync()/unsettled_hosts()/time via patches so no Docker, DB, or
real sleeping is involved — they verify the convergence control flow only.

Run with::

    uv run pytest test/test_sync_converge.py -v
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cspawn.cs_docker.csmanager import CodeServerManager


def _mgr():
    """A CodeServerManager instance without running __init__ (which needs Docker)."""
    return CodeServerManager.__new__(CodeServerManager)


def _host(name):
    return SimpleNamespace(service_name=name)


def test_stops_immediately_when_already_settled():
    mgr = _mgr()
    mgr.sync = MagicMock()
    mgr.unsettled_hosts = MagicMock(return_value=[])  # everything settled after pass 1

    with patch("cspawn.cs_docker.csmanager.CodeHost") as CH, \
         patch("time.sleep") as sleep:
        CH.query.count.return_value = 5
        summary = mgr.sync_converge()

    assert summary["passes"] == 1
    assert summary["unsettled"] == 0
    assert summary["settled"] == 5
    mgr.sync.assert_called_once_with(check_ready=True)
    sleep.assert_not_called()  # no backoff needed when it settles on pass 1


def test_converges_after_several_passes():
    mgr = _mgr()
    mgr.sync = MagicMock()
    # Unsettled on passes 1 and 2, then settles on pass 3.
    mgr.unsettled_hosts = MagicMock(side_effect=[
        [_host("teststudent04"), _host("teststudent06")],
        [_host("teststudent04")],
        [],
    ])

    with patch("cspawn.cs_docker.csmanager.CodeHost") as CH, \
         patch("time.sleep") as sleep, \
         patch("time.monotonic", side_effect=[0, 1, 2, 3, 4, 5, 6]):
        CH.query.count.return_value = 10
        summary = mgr.sync_converge()

    assert summary["passes"] == 3
    assert summary["unsettled"] == 0
    assert mgr.sync.call_count == 3
    assert sleep.call_count == 2  # backoff between passes 1->2 and 2->3


def test_respects_max_passes():
    mgr = _mgr()
    mgr.sync = MagicMock()
    mgr.unsettled_hosts = MagicMock(return_value=[_host("stuck")])  # never settles

    with patch("cspawn.cs_docker.csmanager.CodeHost") as CH, \
         patch("time.sleep"), \
         patch("time.monotonic", side_effect=[i * 0.1 for i in range(50)]):
        CH.query.count.return_value = 3
        summary = mgr.sync_converge(max_passes=4, deadline_s=1000)

    assert summary["passes"] == 4
    assert summary["unsettled"] == 1
    assert summary["unsettled_names"] == ["stuck"]
    assert mgr.sync.call_count == 4


def test_respects_deadline():
    mgr = _mgr()
    mgr.sync = MagicMock()
    mgr.unsettled_hosts = MagicMock(return_value=[_host("slow")])  # never settles

    # monotonic jumps past the deadline after the first pass.
    with patch("cspawn.cs_docker.csmanager.CodeHost") as CH, \
         patch("time.sleep"), \
         patch("time.monotonic", side_effect=[0, 100, 200, 300]):
        CH.query.count.return_value = 2
        summary = mgr.sync_converge(max_passes=99, deadline_s=90)

    # Should bail on the deadline check after pass 1, not run all 99 passes.
    assert summary["passes"] == 1
    assert summary["unsettled"] == 1
    mgr.sync.assert_called_once()
