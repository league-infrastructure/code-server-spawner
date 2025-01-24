
import uuid
from functools import wraps

from flask import (abort, current_app, render_template, session, g)
from flask_login import (current_user, login_required)
from app import app


def ensure_session():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        current_app.logger.info(f"New session created with ID: {session['session_id']}")
    else:
        pass

@app.before_request
def before_request():
    ensure_session()
    app.load_user(current_app)

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def staff_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_staff", False
        ):
            current_app.logger.warning(
                f"Unauthorized access attempt by user {current_user.id if current_user.is_authenticated else 'Anonymous'}"
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_admin", False
        ):
            current_app.logger.warning(
                f"Unauthorized access attempt by user {current_user.id if current_user.is_authenticated else 'Anonymous'}"
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated_function

@app.route("/")
def index():
    return render_template("index.html", current_user = current_user)


@app.route("/hello")
def hello_world():
    from  datetime import datetime
    session['counter'] = session.get('counter', 0) + 1
    
    kv = current_app.kvstore
    
    kv['counter'] = kv.get('counter', 0) + 1
    
    # Get the current time and session info
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    session_info = dict(session)
    
    return render_template(
        'hello_world.html',
        current_time=current_time,
        session_info=session_info,
        scounter=session['counter'],
        kvcounter=kv['counter']
    )

@app.route("/private")
@login_required
def private():
    return render_template("private.html", current_user = current_user)


@app.route("/private/staff")
@staff_required
def staff():
    return render_template("private-staff.html", current_user = current_user)


@app.route("/private/admin")
@staff_required
def admin():
    return render_template("private-admin.html", current_user = current_user)


@app.route("/public")
def public():
    return render_template("public.html", current_user = current_user)