

from pathlib import Path
from typing import Any, Dict, List, Tuple
from dotenv import dotenv_values
import os 

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
    cwd = Path.cwd()
    
    for i in range(3):
        if (cwd / 'config').exists() or (cwd / 'secrets').exists():
            return cwd
        try:
            cwd = cwd.parent
        except:
            break

    raise FileNotFoundError("No directory with 'config' or 'secrets' found")

def walk_up(d, f=None) -> List[Path]:
    d = Path(d).resolve()
    paths = []
    while True:
        d = d.parent
        if d == Path('/'):
            break

        if f is not None:
            paths.append(d / f)
        else:
            paths.append(d)

    return paths

def get_config_dirs(cwd=None, root=Path('/'), home=Path().home()) -> List[Path]:
    
    if cwd is None:
        cwd = Path.cwd()
    else:
        cwd = Path(cwd)

    root = Path(root)
    home = Path(home)

    return  [
        cwd,
        home.joinpath('.jtl'),

        root / 'app/config',
        root / 'app/secrets',
        root / 'config',
        root / 'secrets',
        cwd.joinpath('secrets'),
        cwd.parent.joinpath('secrets'),
    ] 


def find_config_files(file: str | List[str], dirs: List[str] | List[Path] = None) -> Path:
    """Find the first instance of a config file, from  a list of possible files, 
    in a list of directories. Return the first file that exists. """

    if isinstance(file, str):
        file = [file]

    if dirs is None:
        dirs = get_config_dirs()

    files = []
    for d in dirs:
        for f in file:
            p = Path(d) / f 
           
            if p.exists():
                files.append(p)  
    
    if files:
        return files
    else:
        raise FileNotFoundError(f"Could not find any of {file} in {dirs}")

def find_config_file(file: str | List[str], dirs: List[str] | List[Path] = None) -> Path:
    files = find_config_files(file, dirs)
    if len(files) > 1:
        raise FileNotFoundError(f"Found multiple files: {files}")
    return files[0]

def get_config(file: str | Path | List[str] | List[Path] = None, 
               dirs: List[str] | List[Path] = None) -> Config:

    """ Get the first config file found. The is for when you 
    just want one config file, but there may be multiple places for it. """

    if file is None:
        file = 'config.env'


    if '/' in str(file):
        fp = Path(file)
    else:
        fp = find_config_file(file, dirs)

    config = {
        '__CONFIG_PATH': str(fp.absolute()),
        **os.environ,
        **dotenv_values(fp),
    }

    return Config(config)

def get_config_tree(config_root: Path, deploy_name='devel', env_pos='last') -> Config:
    """Assemble a configuration from a tree of config files. In this case
    (* different from get_config) the config is split into multiple 
    parts, and the parts are assembled into a single config object, and all of the
    configurations are stored in a set of subdirs of a common root directory.
    
    For each of the dirs 'config' and secret', the function will look for a file
    names 'config.env' and then '{deploy_name}.env' ( either 'devel' or 'prod' ) 
    and combine them into a single config object.
    
    So, if they exist, these files will be read and combined: 
    
        'config/config.env',
        '{deploy_name}.env',
        'secrets/config.env',
        'secrets/{deploy_name}.env',
    
    The env_pos parameter controls when the environment variables are loaded. If it is
    None, they are loaded last. If it is 'first', they are loaded first. If it is 'last',
    they are loaded last, overwriting any values that were loaded from the files. If None
     the env vars are not loaded. Defaults to 'last'
    
    """
    
    root = Path(config_root).resolve()
    
    tree = [
        'config/config.env',
        'config/{deploy_name}.env',
        'secrets/secret.env',
        'secrets/{deploy_name}.env',
    ]
    
    d = {}
    
    if env_pos == 'first':
        d.update(os.environ)
    
    configs = []
    
    for e in tree:
        f =  root / Path(e.format(deploy_name=deploy_name))
             
        if f.exists():
            d.update(dotenv_values(f))
            configs.append(f)
    if env_pos == 'last':
        d.update(os.environ)
        
    d['__CONFIG_PATH'] = configs
    
    return Config(d)
            
    

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
            params[key.rstrip('_')] = value

    return path, params
