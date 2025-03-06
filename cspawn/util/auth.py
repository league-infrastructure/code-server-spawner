import secrets
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


def find_username(user):
    """Look for a unique username"""
    from slugify import slugify

    from cspawn.models import User

    def split_email(email):
        return slugify(email.split("@")[0])

    def username_exists(username):
        return User.query.filter_by(username=username).first() is not None

    email = user.email
    username = split_email(email)

    if not username_exists(username):
        return username

    if not username_exists(email):
        return email

    for i in range(1, 100):
        new_username = f"{username}_{i}"
        if not username_exists(new_username):
            return new_username

    return username + "_" + secrets.token_urlsafe(8)
