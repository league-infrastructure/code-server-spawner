"""
Unit tests for the `node autoscale` CLI command (ticket 004-005).

Tests cover:
- Option parsing: --dry-run, --force, --up-only, --down-only forwarded correctly.
- Result summary is echoed to stdout.
- AUTOSCALE_ENABLED=false path: exits 0 and echoes the disabled summary.
- Crontab static check: the cron line is present and commented out.

No live Docker, DigitalOcean, or database I/O in any test here.
Run with::

    uv run pytest test/test_node_autoscale_cmd.py -v
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cspawn.cli.node import autoscale_cmd
from cspawn.cs_docker.autoscale import ApplyResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    *,
    added: int = 0,
    removed: int = 0,
    purged: bool = False,
    dry_run: bool = False,
    errors: list[str] | None = None,
) -> ApplyResult:
    return ApplyResult(
        added=added,
        removed=removed,
        purged=purged,
        dry_run=dry_run,
        errors=errors or [],
    )


def _invoke_autoscale(args: list[str], run_result: ApplyResult | None = None):
    """Invoke autoscale_cmd with run_autoscale mocked out.

    Returns (CliRunner result, mock_run_autoscale).
    """
    if run_result is None:
        run_result = _make_result()

    runner = CliRunner(mix_stderr=False)

    with patch(
        "cspawn.cli.node.autoscale_cmd.__wrapped__",
        # Patch the lazy-imported run_autoscale inside the command body.
        # Because autoscale_cmd imports run_autoscale at call time, we patch
        # the module-level name inside cspawn.cs_docker.autoscale.
        create=True,
    ):
        pass  # context manager entry only — actual patch below

    # Patch run_autoscale via the module it lives in so the lazy import picks it up.
    with patch(
        "cspawn.cs_docker.autoscale.run_autoscale",
        return_value=run_result,
    ) as mock_run:
        result = runner.invoke(
            autoscale_cmd,
            args,
            obj={"v": 0, "deploy": "devel"},
            catch_exceptions=False,
        )
        return result, mock_run


# ---------------------------------------------------------------------------
# Tests: ApplyResult.summary
# ---------------------------------------------------------------------------

class TestApplyResultSummary:
    def test_summary_default(self):
        r = ApplyResult()
        s = r.summary()
        assert "added=0" in s
        assert "removed=0" in s
        assert "dry_run=False" in s
        assert "errors=0" in s

    def test_summary_with_values(self):
        r = ApplyResult(added=2, removed=1, purged=True, dry_run=True, errors=["oops"])
        s = r.summary()
        assert "added=2" in s
        assert "removed=1" in s
        assert "purged=True" in s
        assert "dry_run=True" in s
        assert "errors=1" in s


# ---------------------------------------------------------------------------
# Tests: autoscale_cmd CLI wiring
# ---------------------------------------------------------------------------

class TestAutoscaleCmdCLI:
    def test_no_options_calls_run_autoscale_with_defaults(self):
        """Bare invocation calls run_autoscale with dry_run=False, force=False, up_only=None."""
        result, mock_run = _invoke_autoscale([])
        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["dry_run"] is False
        assert kwargs["force"] is False
        assert kwargs["up_only"] is None

    def test_dry_run_flag_forwarded(self):
        """--dry-run sets dry_run=True."""
        result, mock_run = _invoke_autoscale(["--dry-run"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["dry_run"] is True

    def test_short_dry_run_flag_forwarded(self):
        """-N sets dry_run=True (short form)."""
        result, mock_run = _invoke_autoscale(["-N"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["dry_run"] is True

    def test_force_flag_forwarded(self):
        """--force sets force=True."""
        result, mock_run = _invoke_autoscale(["--force"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["force"] is True

    def test_up_only_flag_forwarded(self):
        """--up-only sets up_only=True."""
        result, mock_run = _invoke_autoscale(["--up-only"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["up_only"] is True

    def test_down_only_flag_forwarded(self):
        """--down-only sets up_only=False."""
        result, mock_run = _invoke_autoscale(["--down-only"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["up_only"] is False

    def test_all_options_combined(self):
        """--dry-run --force --up-only all forwarded together."""
        result, mock_run = _invoke_autoscale(["--dry-run", "--force", "--up-only"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_run.call_args
        assert kwargs["dry_run"] is True
        assert kwargs["force"] is True
        assert kwargs["up_only"] is True

    def test_summary_is_echoed_to_stdout(self):
        """The result.summary() string is printed to stdout."""
        run_result = _make_result(added=1, dry_run=True)
        result, _ = _invoke_autoscale(["--dry-run"], run_result=run_result)
        assert result.exit_code == 0, result.output
        assert "added=1" in result.output
        assert "dry_run=True" in result.output

    def test_disabled_path_exits_zero_and_echoes_summary(self):
        """When AUTOSCALE_ENABLED=false, run_autoscale returns empty ApplyResult;
        autoscale_cmd still exits 0 and echoes the summary."""
        # run_autoscale returns a default (empty) ApplyResult when disabled
        run_result = _make_result(added=0, removed=0, dry_run=False)
        result, _ = _invoke_autoscale([], run_result=run_result)
        assert result.exit_code == 0, result.output
        # Summary should appear on stdout
        assert "autoscale result=" in result.output

    def test_help_exits_zero_and_shows_options(self):
        """--help exits 0 and shows all expected option names."""
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(autoscale_cmd, ["--help"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "--dry-run" in result.output
        assert "--force" in result.output
        assert "--up-only" in result.output
        assert "--down-only" in result.output


# ---------------------------------------------------------------------------
# Tests: crontab static check
# ---------------------------------------------------------------------------

class TestCrontabAutoscaleLine:
    """Static checks on docker/crontab.

    The autoscale cron was activated 2026-07-07 (Phase 1 dry-run): the line is now
    LIVE, and safety comes from AUTOSCALE_DRY_RUN=true in config, not from the line
    being commented. These checks enforce it is present, active, and correctly
    formed for cron's stripped environment (cd /app + source the runtime env)."""

    CRONTAB_PATH = Path(__file__).parent.parent / "docker" / "crontab"

    def test_crontab_exists(self):
        assert self.CRONTAB_PATH.exists(), f"crontab not found at {self.CRONTAB_PATH}"

    def test_autoscale_cron_line_is_present(self):
        """The autoscale cron entry should exist in the file."""
        text = self.CRONTAB_PATH.read_text()
        assert "node autoscale" in text, "Expected 'node autoscale' line not found in crontab"

    def test_autoscale_cron_line_is_active_and_well_formed(self):
        """The autoscale cron is now LIVE (Phase 1 dry-run) and must be correctly
        formed for cron's stripped environment: exactly one uncommented line that
        cd's to /app and sources /app/cron.env before running `cspawnctl -d prod
        node autoscale` on a */2 schedule. Missing the `cd`/env-source is the exact
        regression that made the cron crash (FileNotFoundError / DATABASE_URI unset)."""
        text = self.CRONTAB_PATH.read_text()
        live_lines = [
            line for line in text.splitlines()
            if "node autoscale" in line and not line.strip().startswith("#")
        ]
        assert len(live_lines) == 1, (
            f"Expected exactly one live 'node autoscale' cron line, found {len(live_lines)}:\n"
            + "\n".join(live_lines)
        )
        line = live_lines[0]
        assert line.strip().startswith("*/2 "), f"Autoscale cron should run every 2 min: {line!r}"
        assert "cd /app" in line, (
            f"Autoscale cron must `cd /app` (else find_parent_dir can't locate config/): {line!r}"
        )
        assert ". /app/cron.env" in line, (
            f"Autoscale cron must source /app/cron.env (cron has a stripped env; without it "
            f"DATABASE_URI/DO_TOKEN are unset): {line!r}"
        )
        assert "cspawnctl -d prod node autoscale" in line, (
            f"Autoscale cron must run `cspawnctl -d prod node autoscale`: {line!r}"
        )
