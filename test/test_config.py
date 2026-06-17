"""Tests for cspawn.util.config — single .env loading path."""
import os
import textwrap
from pathlib import Path

import pytest

from cspawn.util.config import Config, _find_env_file, get_config, find_parent_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_env(tmp_path: Path, content: str) -> Path:
    """Write a .env file to tmp_path and return its path."""
    env_file = tmp_path / ".env"
    env_file.write_text(textwrap.dedent(content))
    return env_file


# ---------------------------------------------------------------------------
# Config class
# ---------------------------------------------------------------------------


class TestConfig:
    def test_get_item(self):
        c = Config({"KEY": "value"})
        assert c["KEY"] == "value"

    def test_get_method(self):
        c = Config({"KEY": "value"})
        assert c.get("KEY") == "value"
        assert c.get("MISSING", "default") == "default"

    def test_getattr(self):
        c = Config({"KEY": "value"})
        assert c.KEY == "value"

    def test_contains(self):
        c = Config({"KEY": "value"})
        assert "KEY" in c
        assert "NOPE" not in c

    def test_setitem(self):
        c = Config({"KEY": "value"})
        c["KEY"] = "new"
        assert c["KEY"] == "new"

    def test_to_dict(self):
        d = {"A": "1", "B": "2"}
        c = Config(d)
        assert c.to_dict() == d


# ---------------------------------------------------------------------------
# _find_env_file
# ---------------------------------------------------------------------------


class TestFindEnvFile:
    def test_finds_env_in_root_arg(self, tmp_path):
        write_env(tmp_path, "KEY=val\n")
        found = _find_env_file(tmp_path)
        assert found == (tmp_path / ".env").resolve()

    def test_raises_when_missing(self, tmp_path, monkeypatch):
        # Point cwd at an empty temp dir so no .env is found walking up
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("JTL_CONFIG_DIR", raising=False)
        monkeypatch.delenv("JTL_APP_DIR", raising=False)
        monkeypatch.delenv("JTP_APP_DIR", raising=False)
        with pytest.raises(FileNotFoundError, match="dotconfig load"):
            _find_env_file(None)

    def test_jtl_config_dir_wins(self, tmp_path, monkeypatch):
        env_dir = tmp_path / "config"
        env_dir.mkdir()
        write_env(env_dir, "KEY=from_config_dir\n")
        monkeypatch.setenv("JTL_CONFIG_DIR", str(env_dir))
        found = _find_env_file(None)
        assert found == (env_dir / ".env").resolve()

    def test_jtl_app_dir_wins_over_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JTL_CONFIG_DIR", raising=False)
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        write_env(app_dir, "KEY=from_app_dir\n")
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        write_env(root_dir, "KEY=from_root\n")
        monkeypatch.setenv("JTL_APP_DIR", str(app_dir))
        found = _find_env_file(root_dir)
        assert found == (app_dir / ".env").resolve()


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_loads_env_file(self, tmp_path, monkeypatch):
        # Clear env vars that would override our test file values
        monkeypatch.delenv("DATABASE_URI", raising=False)
        monkeypatch.delenv("SECRET_KEY", raising=False)
        write_env(tmp_path, "DATABASE_URI=sqlite:///test.db\nSECRET_KEY=abc\n")
        c = get_config(root=tmp_path)
        assert c.get("DATABASE_URI") == "sqlite:///test.db"
        assert c.get("SECRET_KEY") == "abc"

    def test_os_environ_wins(self, tmp_path, monkeypatch):
        write_env(tmp_path, "DATABASE_URI=from_file\n")
        monkeypatch.setenv("DATABASE_URI", "OVERRIDE_VALUE")
        c = get_config(root=tmp_path)
        assert c["DATABASE_URI"] == "OVERRIDE_VALUE"

    def test_config_path_is_list(self, tmp_path):
        write_env(tmp_path, "KEY=val\n")
        c = get_config(root=tmp_path)
        paths = c["__CONFIG_PATH"]
        assert isinstance(paths, list)
        assert len(paths) == 1
        assert Path(paths[0]).name == ".env"

    def test_config_dir_populated(self, tmp_path):
        write_env(tmp_path, "KEY=val\n")
        c = get_config(root=tmp_path)
        assert c.get("CONFIG_DIR") == str(tmp_path.resolve())

    def test_raises_when_no_env(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("JTL_CONFIG_DIR", raising=False)
        monkeypatch.delenv("JTL_APP_DIR", raising=False)
        monkeypatch.delenv("JTP_APP_DIR", raising=False)
        with pytest.raises(FileNotFoundError):
            get_config(root=tmp_path / "nonexistent")

    def test_deploy_param_accepted(self, tmp_path):
        """deploy kwarg is accepted (for backward compat) and does not raise."""
        write_env(tmp_path, "KEY=val\n")
        c = get_config(root=tmp_path, deploy="prod")
        assert c.get("KEY") == "val"


# ---------------------------------------------------------------------------
# find_parent_dir
# ---------------------------------------------------------------------------


class TestFindParentDir:
    def test_finds_dir_with_config(self, tmp_path):
        (tmp_path / "config").mkdir()
        # Simulate cwd inside tmp_path
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = find_parent_dir()
            assert result == tmp_path
        finally:
            os.chdir(orig_cwd)

    def test_finds_dir_with_env_file(self, tmp_path):
        write_env(tmp_path, "KEY=val\n")
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = find_parent_dir()
            assert result == tmp_path
        finally:
            os.chdir(orig_cwd)

    def test_jtl_app_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JTL_APP_DIR", str(tmp_path))
        monkeypatch.delenv("JTP_APP_DIR", raising=False)
        result = find_parent_dir()
        assert result == tmp_path
