import os
from pathlib import Path
import pytest
import warnings


import logging
logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.INFO)

warnings.filterwarnings("ignore")

warnings.filterwarnings("ignore", module='passlib.handlers.bcrypt')


@pytest.fixture
def app():
    from cspawn.init import init_app
    # Set the environment variable for the config directory
    config_dir = Path(__file__).parent.parent 

    logging.debug(f"XXX config_dir: {config_dir}")

    app = init_app(config_dir=config_dir)
    
    return app

def test_secret_key(app):
    
    for e in app.app_config['__CONFIG_PATH']:
        logging.info(f'    config dir: {str(e)}')
    
    assert app.app_config['SECRET_KEY'] == 'gQU97yUgJ6rq@4!p7-ni'
    
    
def test_user_db(app):
    from cspawn.users.models import User, db    

    with app.app_context():
        db.create_all()
           
        # Delete all users with email addresses in the 'example.com' domain
        users_to_delete = User.query.filter(User.email.like('%@example.com')).all()
        for user in users_to_delete:
            db.session.delete(user)
        db.session.commit()
           
        bob = User(username='bob', email='bob@example.com', password='password')
        larry = User(username='larry', email='larry@example.com', password='password')
        sally = User(username='sally', email='sally@example.com', password='password')
        
        db.session.add(bob)
        db.session.add(larry)
        db.session.add(sally)
        db.session.commit()
           
        # Ensure there are three users with example.com emails
        example_users = User.query.filter(User.email.like('%@example.com')).all()
        assert len(example_users) == 3
           
                        
        user = User.query.filter_by(username='admin').first()
        logging.debug(f"XXX user: {user}")  
        #assert user is not None
        
  
  
def test_delete_all_users(app):
    from cspawn.users.models import User, db    

    with app.app_context():
        db.create_all()
           
        #Delete all users
        users_to_delete = User.query.all()
        for user in users_to_delete:
            db.session.delete(user)
        
  
if __name__ == "__main__":
    pytest.main(["-k", "test_user_db"])
    