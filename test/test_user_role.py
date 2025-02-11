import unittest             
from pathlib import Path
from faker import Faker
from cspawn.hosts.models import CodeHost, KeystrokeReport, FileStat
from cspawn.auth.models import User
from cspawn.init import db
import logging 
import warnings
        
import unittest
from flask import Flask


class TestUserRole(unittest.TestCase):
    
    def setUp(self):

        import cspawn
        from cspawn.init import init_app

        this_dir  = Path(__file__).parent
        config_dir = Path(cspawn.__file__).parent.parent

        warnings.filterwarnings("ignore")
        self.app = init_app(config_dir=config_dir, log_level=logging.ERROR, sqlfile=this_dir/'test.db')
        
        self.fake = Faker()

    def test_user_basic(self):
        print(self.app.app_config['SECRET_KEY'])
        
    def test_user_role_basic(self):
        
        faker = Faker()
        
        with self.app.app_context():
           

            db.drop_all()
            db.create_all()
            
            users = []
            for _ in range(3):
                user = User(
                    username=faker.user_name(),
                    email=faker.email(),
                    password=faker.password()
                )
                db.session.add(user)
                users.append(user)
            
            db.session.commit()
            
            for user in users:
                code_host = CodeHost(
                    user_id=user.id,
                    service_id=faker.uuid4(),
                    service_name=faker.word(),
                    container_id=faker.uuid4(),
                    container_name=faker.word()
                )
         
         
if __name__ == '__main__':
    unittest.main()