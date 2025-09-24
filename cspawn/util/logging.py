from cspawn.util.app_support import is_running_under_gunicorn


import logging


def init_logger_devel(app, log_level):
    """Initialize development logging for Flask app."""

    
    # Remove all handlers from app logger and propagate to root
    app.logger.handlers.clear()
    app.logger.setLevel(log_level)
    app.logger.propagate = True
    app.logger.debug("Logger initialized for development mode")


    # Suppress Alembic logs
    alembic_logger = logging.getLogger("alembic")
    alembic_logger.setLevel(logging.WARNING)
    alembic_logger.propagate = False


    #werkzeug_logger = logging.getLogger("werkzeug")
    #werkzeug_logger.setLevel(logging.INFO)
    #werkzeug_logger.propagate = True
   
    return 
    # Ensure root logger has a StreamHandler for all logs
    root_logger = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        stream_handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    return 
    cspawn_logger = logging.getLogger("cspawn")
    cspawn_logger.handlers.clear()
    for handler in app.logger.handlers:
        cspawn_logger.addHandler(handler)
    cspawn_logger.setLevel(logging.DEBUG)
    cspawn_logger.propagate = False

    app.logger.info("Development loggers initialized.")

def init_logger_production(app, log_level):
    """Initialize production logging for Gunicorn app."""
    gunicorn_logger = logging.getLogger("gunicorn.error")

    app.logger.handlers.clear()
    for handler in gunicorn_logger.handlers:
        app.logger.addHandler(handler)
    app.logger.setLevel(log_level or gunicorn_logger.level or logging.INFO)
    app.logger.propagate = False

    # Suppress Alembic logs
    #alembic_logger = logging.getLogger("alembic")
    #alembic_logger.setLevel(logging.WARNING)
    #alembic_logger.propagate = False

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.handlers.clear()
    for handler in gunicorn_logger.handlers:
        werkzeug_logger.addHandler(handler)
    werkzeug_logger.setLevel(logging.INFO)
    werkzeug_logger.propagate = False

    cspawn_logger = logging.getLogger("cspawn")
    cspawn_logger.handlers.clear()
    for handler in gunicorn_logger.handlers:
        cspawn_logger.addHandler(handler)
    cspawn_logger.setLevel(logging.DEBUG)
    cspawn_logger.propagate = False

    app.logger.info(f"Production loggers initialized for gunicorn with level {app.logger.level}")

def init_logger(app, log_level=None):
    """Initialize the logger for the app, choosing devel or production."""

    if is_running_under_gunicorn():
        init_logger_production(app, log_level)
    else:
        init_logger_devel(app, log_level)


    app.logger.info(f"Development loggers initialized with level {app.logger.level}")
    
    return 
    # Log summary of logger levels
    for logger_name in logging.root.manager.loggerDict:
        logger = logging.getLogger(logger_name)
        if logger_name.startswith("cspawn") or 'gunicorn' in logger_name or 'werkzeug' in logger_name:
            app.logger.info(f"    Logger: {logger_name}, Level: {logging.getLevelName(logger.level)}")