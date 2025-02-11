import unittest             
from pathlib import Path
from faker import Faker

from cspawn.models import CodeHost, User, HostImage
from cspawn.init import db
import logging 
import warnings
        
import unittest
from flask import Flask



def make_data(app):
        
        faker = Faker()
        
        with app.app_context():
           
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
            
            host_images = []
            for _ in range(2):
                host_image = HostImage(
                    name=faker.word(),
                    repo_uri=faker.url(),
                    image_uri=faker.url()
                )
                db.session.add(host_image)
                host_images.append(host_image)
            
            db.session.commit()
            
            for i, user in enumerate(users):
                code_host = CodeHost(
                    user_id=user.id,
                    service_id=faker.uuid4(),
                    service_name=faker.word(),
                    container_id=faker.uuid4(),
                    container_name=faker.word(),
                    host_image_id=host_images[i%len(host_images)].id  # Linking to the first HostImage
                )
                
                db.session.add(code_host)
        
            db.session.commit()
            
            code_hosts = CodeHost.query.all()
            assert len(code_hosts) == 3


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
        
        make_data(self.app)
        
        with self.app.app_context():
            code_host = CodeHost.query.first()
            self.assertEqual(code_host.user.username, User.query.first().username)
            self.assertEqual(code_host.host_image.name, HostImage.query.first().name)
            

if __name__ == '__main__':
    unittest.main()