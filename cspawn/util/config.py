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


def find_parent_dir():
    jtp_app_dir = os.getenv("JTP_APP_DIR")
    if jtp_app_dir and Path(jtp_app_dir).is_dir():
        return Path(jtp_app_dir)

    cwd = Path.cwd()

    for i in range(3):
        if (cwd / "config").exists() or (cwd / "secrets").exists():
            return cwd
        try:
            cwd = cwd.parent
        except Exception:
            break

    raise FileNotFoundError("No directory with 'config'found")


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


def get_config_dirs(cwd=Path.cwd(), root=Path("/"), home=Path().home()) -> List[Path]:
    """Return possible config dirs in order of precedence:

    JT_CONFIG_DIR env var
    Current directory
    $HOME/.jtl
    /app/config
    /config

    """

    import os

    jtl_config_dir = os.getenv("JTL_CONFIG_DIR")

    cwd = Path(cwd)
    root = Path(root)
    home = Path(home)

    return (
        [Path(jtl_config_dir)]
        if jtl_config_dir
        else []
        + [
            home.joinpath(".jtl"),
            root / "app/config",
            root / "config",
            cwd,
        ]
    )


def get_config_files(
    dirs: List[Path], config_name="config", deploy: str = "devel"
) -> List[Path]:
    """ """

    config_name += ".env"

    def first_config():
        for d in dirs:
            if (d / config_name).exists():
                return d

    cdir = first_config()

    if not cdir:
        raise FileNotFoundError(f"No config files found in ${dirs}")

    f = [
        (cdir / config_name),
        cdir / f"{deploy}.env",
        cdir / "secrets/secret.env",
        cdir / f"secrets/{deploy}.env",
    ]

    return [p for p in f if p.exists()]


def get_config(
    root: str | Path = None,
    dirs: List[str] | List[Path] = None,
    file: str | Path | List[str] | List[Path] = None,
    deploy: str = "devel",
) -> Config:
    """Get the first config file found. There must at least be a file 'config.env',
    and may be a file '{deploy}.env' where deploy is typically 'devel' or 'prod'.

    After finding a config file in dir $D, the function will look for a file
    $D/secrets/secret.env and $D/secrets/{deploy}.env and combine them into a single
    config object.

    """

    if file is None:
        file = "config"

    config = {}
    loaded = []

    cf = get_config_files(
        dirs or get_config_dirs(root=root), config_name=file, deploy=deploy
    )

    for f in cf:
        if f.exists():
            config.update(dotenv_values(f))
            loaded.append(f)

    config.update(os.environ)
    config["__CONFIG_PATH"] = loaded

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
