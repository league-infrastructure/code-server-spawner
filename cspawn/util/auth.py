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
    """Look for a unique username, preferring the account's name.

    League student Google accounts use numeric student-ID email addresses
    (e.g. ``52@students.jointheleague.org``), so deriving the username from the
    email local-part alone yields a bare number (``52``) and forks named
    ``Python-Apprentice-52``. When the email local-part has no letters, fall
    back to the user's display name so the username reflects who they are.
    """
    from slugify import slugify

    from cspawn.models import User

    def split_email(email):
        return slugify(email.split("@")[0]) if email else ""

    def username_exists(username):
        return User.query.filter_by(username=username).first() is not None

    email = user.email
    email_slug = split_email(email)
    name_slug = slugify(user.display_name) if getattr(user, "display_name", None) else ""

    # Prefer the email local-part only when it carries something name-like
    # (at least one letter); otherwise prefer the display name.
    if email_slug and any(c.isalpha() for c in email_slug):
        username = email_slug
    elif name_slug:
        username = name_slug
    else:
        username = email_slug or name_slug

    if not username:
        # No usable email or name; fall back to a random handle.
        return "user-" + secrets.token_urlsafe(8)

    if not username_exists(username):
        return username

    for i in range(1, 100):
        new_username = f"{username}_{i}"
        if not username_exists(new_username):
            return new_username

    return username + "_" + secrets.token_urlsafe(8)
