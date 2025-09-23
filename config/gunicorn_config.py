# Called just after a worker has been forked.
# The callable needs to accept two instance variables for the Arbiter and new Worker.
def post_fork(server, worker):
    """
    Called after worker processes are forked.
    This ensures database connections are not shared between workers.
    """
    server.log.info(f"Worker spawned (pid: {worker.pid})")
    
    # Import here to avoid circular imports
    try:
        from flask import current_app
        from cspawn.models import db
        
        # Create new database connections for this worker
        # This disposes connections inherited from the parent process
        db.engine.dispose()
        server.log.info(f"Worker {worker.pid}: Database connections reset")
        
    except Exception as e:
        server.log.error(f"Worker {worker.pid}: Error resetting database connections: {e}")

# Command line: -b ADDRESS or --bind ADDRESS
# Default: ['127.0.0.1:8000']
# The socket to bind.
bind = "0.0.0.0:8000"

# Command line: -w INT or --workers INT
# Default: 1
# The number of worker processes for handling requests.
# A positive integer generally in the 2-4 x $(NUM_CORES) range.
# You’ll want to vary this a bit to find the best for your particular application’s work load.
# multiprocessing.cpu_count() * 2 + 1
workers = 2

# Command line: --threads INT
# Default: 1
# The number of worker threads for handling requests.
# Run each worker with the specified number of threads.
# If you try to use the sync worker type and set the threads setting to more than 1,
# the gthread worker type will be used instead.
# threads = 2

# Command line: -t INT or --timeout INT
# Default: 30
# Workers silent for more than this many seconds are killed and restarted.
timeout = 120

# Command line: --log-level LEVEL
# Default: 'info'
# The granularity of Error log outputs.
loglevel = "debug"

# Command line: -p FILE or --pid FILE
# Default: None
# A filename to use for the PID file.
# pidfile = "/var/log/docker_flask_gunicorn.pid"

# Command line: --access-logfile FILE
# Default: None
# The Access log file to write to.
# '-' means log to stdout.
accesslog = "-"

# Command line: --error-logfile FILE or --log-file FILE
# Default: '-'
# The Error log file to write to.
# Using '-' for FILE makes gunicorn log to stderr.
errorlog = "-"

# Default: '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
access_log_format = '%({X-Real-IP}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Command line: --capture-output
# Default: False
# Redirect stdout/stderr to specified file in errorlog.
capture_output = True

# Enable logging for all workers
# Default: sync
# The type of workers to spawn.
worker_class = "sync"

# Command line: --worker-connections INT
# Default: 1000
# The maximum number of simultaneous clients per worker.
worker_connections = 1000

# Command line: --preload  
# Default: False
# Load application code before the worker processes are forked.
# We use preload_app = True for better performance with proper post_fork handling
preload_app = True
