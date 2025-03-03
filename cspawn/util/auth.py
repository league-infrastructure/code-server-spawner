
import bcrypt
import base64
import os

import string
import random


def basic_auth_hash(password):
    # Caddy uses bcrypt with a cost factor of 14 by default
    password_bytes = password.encode('utf-8')

    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=14))

    # Return the hash in the format Caddy expects
    return hashed.decode('utf-8')


def docker_label_escape(value):
    # Escape characters that are not allowed in Docker labels
    return value.replace("$", "$$")


def random_string(size: int = 32):
    characters = string.ascii_letters + string.digits + '-_'
    return ''.join(random.choice(characters) for _ in range(size))
