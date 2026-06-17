import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import dotenv_values


class Config:
    def __init__(self, config_dict: Dict[str, Any]):
        self._config_dict = config_dict

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_config_dict":
            super().__setattr__(name, value)
        else:
            self._config_dict[name] = value

    def __getattr__(self, name: str) -> Any:
        try:
            return self._config_dict[name]
        except KeyError:
            raise AttributeError(f"'Config' object has no attribute '{name}'")

    def __getitem__(self, key: str) -> Any:
        return self._config_dict[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._config_dict[key] = value

    def __delitem__(self, key: str) -> None:
        del self._config_dict[key]

    def __contains__(self, key: str) -> bool:
        return key in self._config_dict

    def get(self, key: str, default: Any = None) -> Any:
        return self._config_dict.get(key, default)

    def keys(self) -> List[str]:
        return list(self._config_dict.keys())

    def values(self) -> List[Any]:
        return list(self._config_dict.values())

    def items(self) -> List[Tuple[str, Any]]:
        return list(self._config_dict.items())

    def to_dict(self) -> Dict[str, Any]:
        return self._config_dict.copy()


def find_parent_dir() -> Path:
    """Walk up from cwd (or JTL_APP_DIR / JTP_APP_DIR) to find the project root.

    The project root is the directory that contains a ``config`` subdirectory
    or a ``.env`` file.  Checks up to three levels above cwd.
    """
    # Support both old (JTP_APP_DIR) and new (JTL_APP_DIR) env-var names.
    app_dir = os.getenv("JTL_APP_DIR") or os.getenv("JTP_APP_DIR")
    if app_dir and Path(app_dir).is_dir():
        return Path(app_dir)

    cwd = Path.cwd()

    for _ in range(3):
        if (cwd / ".env").exists() or (cwd / "config").exists() or (cwd / "secrets").exists():
            return cwd
        try:
            cwd = cwd.parent
        except Exception:
            break

    raise FileNotFoundError("No project root (containing '.env' or 'config') found")


def _find_env_file(root: Path | None) -> Path:
    """Locate the dotconfig-generated ``.env`` file.

    Search order (first match wins):
    1. ``JTL_CONFIG_DIR`` environment variable — look for ``.env`` there.
    2. ``JTL_APP_DIR`` (or legacy ``JTP_APP_DIR``) — look for ``.env`` there.
    3. *root* argument (if provided) — look for ``.env`` there.
    4. Walk up from ``cwd`` up to three levels, looking for ``.env``.

    Raises ``FileNotFoundError`` with a hint if no ``.env`` is found.
    """
    candidates: List[Path] = []

    jtl_config_dir = os.getenv("JTL_CONFIG_DIR")
    if jtl_config_dir:
        candidates.append(Path(jtl_config_dir) / ".env")

    app_dir = os.getenv("JTL_APP_DIR") or os.getenv("JTP_APP_DIR")
    if app_dir:
        candidates.append(Path(app_dir) / ".env")

    if root is not None:
        candidates.append(Path(root) / ".env")

    # Walk up from cwd
    cwd = Path.cwd()
    for _ in range(4):
        candidates.append(cwd / ".env")
        if cwd.parent == cwd:
            break
        cwd = cwd.parent

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    # No .env file on disk. This is normal in the container deployment, where
    # config is injected as environment variables via the stack's `env_file:`
    # (env_file populates the process environment, it does not write a file).
    # Callers fall back to os.environ in that case.
    return None


def walk_up(d, f=None) -> List[Path]:
    d = Path(d).resolve()
    paths = []
    while True:
        d = d.parent
        if d == Path("/"):
            break

        if f is not None:
            paths.append(d / f)
        else:
            paths.append(d)

    return paths


def get_config(
    root: str | Path = None,
    dirs: List[str] | List[Path] = None,
    file: str | Path | List[str] | List[Path] = None,
    deploy: str = "devel",
) -> Config:
    """Load application configuration from a single dotconfig-generated ``.env`` file.

    The ``.env`` file is expected to be produced by::

        dotconfig load -d <deploy> [--no-export] [-e] -o .env

    **File location** (first match wins):

    1. ``JTL_CONFIG_DIR`` env var — ``.env`` is loaded from that directory.
    2. ``JTL_APP_DIR`` / ``JTP_APP_DIR`` env var — ``.env`` is loaded from there.
    3. ``root`` argument — ``.env`` is loaded from that directory.
    4. Walk up from ``cwd`` up to three levels.

    **Precedence** (higher wins):

    - ``os.environ`` values override everything in the ``.env`` file.

    **Keys set on the returned Config**:

    - ``__CONFIG_PATH`` — list containing the resolved path to the ``.env`` file
      (kept as a list for backward compatibility with CLI callers).
    - ``CONFIG_DIR`` — parent directory of the ``.env`` file (used by CLI tools
      that reference ``cloud-init`` files relative to the project root).

    Raises ``FileNotFoundError`` with a hint when no ``.env`` is found.
    """
    env_path = _find_env_file(Path(root) if root is not None else None)

    config: Dict[str, Any] = {}
    if env_path is not None:
        # Local / developer flow: a dotconfig-generated .env on disk.
        config.update(dotenv_values(env_path))

    # os.environ wins over .env values (backward-compatible precedence rule).
    # In the container deployment there is no .env file — config arrives here
    # entirely via os.environ (the stack's env_file: injects the vars).
    config.update(os.environ)

    # Populate legacy keys that callers depend on. When config came from the
    # environment (no file), anchor CONFIG_DIR to JTL_APP_DIR / cwd so callers
    # that resolve paths relative to it (e.g. cloud-init files) still work.
    if env_path is not None:
        config["CONFIG_DIR"] = str(env_path.parent)
        config["__CONFIG_PATH"] = [env_path]
    else:
        config["CONFIG_DIR"] = config.get("JTL_APP_DIR") or config.get("APP_DIR") or str(Path.cwd())
        config["__CONFIG_PATH"] = ["<environment>"]

    return Config(config)


def path_interp(path: str, **kwargs) -> Tuple[str, Dict[str, Any]]:
    """
    Interpolates the parameters into the endpoint URL. So if you have a path
    like '/api/v1/leagues/:league_id/teams/:team_id' and you call

            path_interp(path, league_id=1, team_id=2, foobar=3)

    it will return '/api/v1/leagues/1/teams/2', along with a dictionary of
    the remaining parameters {'foobar': 3}.

    :param path: The endpoint URL template with placeholders.
    :param kwargs: The keyword arguments where the key is the placeholder (without ':') and the value is the actual value to interpolate.

    :return: A string with the placeholders in the path replaced with actual values from kwargs.
    """

    params = {}
    for key, value in kwargs.items():
        placeholder = f":{key}"  # Placeholder format in the path
        if placeholder in path:
            path = path.replace(placeholder, str(value))
        else:
            # Remove the trailing underscore from the key, so we can use params
            # like 'from' that are python keywords.
            params[key.rstrip("_")] = value

    return path, params
