
import json
import logging
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

import docker
import requests
from cspawn.__version__ import __version__ as version
from cspawn.app import CI_FILE, app, get_db
from cspawn.db import insert_keystroke_data, update_container_status
from flask import (abort, current_app, g, jsonify, redirect, render_template,
                   request, session, url_for, Blueprint, flash, redirect, render_template, 
    request, url_for)
from flask_login import current_user, login_required, logout_user
from jtlutil.docker.dctl import (container_list, container_status,
                                 create_cs_pair, logger)
from jtlutil.flask.flaskapp import insert_query_arg
from slugify import slugify

from datetime import datetime, timezone
import sqlite3


def check_registration_code(code: str) -> bool:
    return code == 'Code4Life'

@app.route("/auth/register", methods=['GET', 'POST'])
def auth_register():
    
    from cspawn.db import get_user_account, insert_user_account
    
    form_data = {
        'username': '',
        'registration_code': '',
        # Don't restore passwords for security
    }
    
    if request.method == 'POST':
        # Get form data
        form_data['username'] = request.form.get('username', '')
        form_data['registration_code'] = request.form.get('registration_code', '')
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        
        db = get_db()

        # Validate all fields are provided
        if not all([form_data['username'], password, password_confirm, form_data['registration_code']]):
            flash('All fields are required', 'error')
            return render_template('register.html', form=form_data)

        # Check registration code
        if not check_registration_code(form_data['registration_code']):
            flash('Invalid registration code', 'error')
            return render_template('register.html', form=form_data)

        # Check username availability
        try:
            existing_user = get_user_account(db, form_data['username'])
           
            if existing_user:
                flash('Username already exists', 'error')
                return render_template('register.html', form=form_data)
        except Exception as e:
            flash('Error checking username availability', 'error')
            current_app.logger.error(f"Error checking username availability: {e}")
            return render_template('register.html', form=form_data)

        # Verify passwords match and length
        if password != password_confirm:
            flash('Passwords do not match', 'error')
            return render_template('register.html', form=form_data)
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return render_template('register.html', form=form_data)

        # Create the account
        try:
         
            create_time = datetime.now(timezone.utc)
            insert_user_account(db, form_data['username'], password, create_time)
          
            flash('Account created. You can Login', 'success')
           
            return redirect(url_for('login'))
        
        except Exception as e:
            
            flash('Error creating account', 'error')
            current_app.logger.error(f"Error creating account: {e}")
         
            return render_template('register.html', form=form_data)

    # GET request - display the registration form
    return render_template('register.html', form=form_data)

@app.route("/auth/up_login", methods=['POST'])
def auth_uplogin():
    
    from cspawn.db import get_user_account
    from jtlutil.flask.flaskapp import User
    from jtlutil.flask.auth import login_user
    
    username = request.form.get('username')
    password = request.form.get('password')
    db = get_db()

    # Validate input
    if not username or not password:
        flash('Username and password are required', 'error')
        return redirect(url_for('login'))

    # Get user account
    user_account = get_user_account(db, username)
    
    if not user_account:
        flash('Invalid username or password', 'error')
        return redirect(url_for('login'))

    # Direct password comparison
    if user_account['password'] != password:
        flash('Invalid username or password', 'error')
        return redirect(url_for('login'))

    # Create user data dictionary
    user_data = {
        'id': username,
        'primaryEmail': username,
        'groups': [],
        'orgUnitPath': '',
        'isAdmin': False
    }

    # Create User object and log in

    login_user(User(user_data))

    # Redirect to home or next page
    next_page = request.args.get('next')
    if not next_page:
        next_page = url_for('index')
        
    return redirect(next_page)