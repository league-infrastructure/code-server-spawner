import pytest
from pathlib import Path
from faker import Faker

@pytest.fixture
def app():

    import cspawn
    from cspawn.init import init_app
    
    this_dir  = Path(__file__).parent
    config_dir = Path(cspawn.__file__).parent.parent

    app = init_app(config_dir=config_dir, sqlfile=this_dir/'test.db')
    
    return app

@pytest.fixture
def fake():
    return Faker()