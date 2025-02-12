from cspawn.main.models import *
from faker import Faker

def create_users(app):
    """Function to load demo data into the database."""
    # Implement your demo data loading logic here
    # Delete all users with email addresses in the 'example.com' domain
    
    faker = Faker()
    users = [
        {'user_id': faker.bothify(text='?????'), 'username': 'EricAdmin', 'email': 'eric.busboom@jointheleague.org', 'password': 'password'},
        {'user_id': faker.bothify(text='?????'), 'username': 'BobStaff', 'email': 'bob.staff@jointheleague.org', 'password': 'password'},
        {'user_id': faker.bothify(text='?????'), 'username': 'sally', 'email': 'sally.forth@students.jointheleague.org', 'password': 'password'}
    ]

    for user in users:
        user = User(**user)
        db.session.add(user)
        
    db.session.commit()

    assert len( User.query.all()) >= 3
      
    
def create_images(app):
    
    host_images = [
        {
            'name': 'Ubuntu 20.04 LTS',
            'image_uri': 'https://example.com/images/ubuntu-20.04.img',
            'repo_uri': 'https://example.com/repos/ubuntu-20.04',
            'startup_script': '#!/bin/bash\napt-get update -y',
            'is_public': True
        },
        {
            'name': 'CentOS 8',
            'image_uri': 'https://example.com/images/centos-8.img',
            'repo_uri': 'https://example.com/repos/centos-8',
            'startup_script': '#!/bin/bash\nyum update -y',
            'is_public': False
        },
        {
            'name': 'Debian 10',
            'image_uri': 'https://example.com/images/debian-10.img',
            'repo_uri': None,
            'startup_script': None,
            'is_public': True
        }
    ]

    for image in host_images:
        host_image = HostImage(**image)
        db.session.add(host_image)
    
    db.session.commit()
    
    assert len(HostImage.query.all()) >= 3

def create_code_hosts(app):

    """
    Create CodeHost records with fake data and associate them with HostImage records.

    :param session: SQLAlchemy Session object.
    :param num_records: Number of CodeHost records to create.
    """
    fake = Faker()
    with app.app_context():
        # Fetch all HostImage records
        host_images = db.session.query(HostImage).all()
        
        # Fetch all User records
        users = db.session.query(User).all()
        
        if not host_images:
            print("No HostImage records found. Please ensure they exist before creating CodeHost records.")
            return

     
        code_hosts = []

        for i in range(5):
            host_image = host_images[i %  len(host_images)]  # Cycle through HostImage records
            user = users[i % len(users)]
            code_host = CodeHost(
                service_id=fake.uuid4(),
                user_id=user.id,
                service_name=fake.uuid4(),
                container_id=fake.uuid4(),
                container_name=fake.domain_word(),
                state='unknown',  # Default state
                host_image_id=host_image.id
            )
            code_hosts.append(code_host)

        db.session.bulk_save_objects(code_hosts)
        db.session.commit()
    
def make_data(app):
        
        faker = Faker()
        
        with app.app_context():
           
         
            create_users(app)
            create_images(app)
            create_code_hosts(app)
