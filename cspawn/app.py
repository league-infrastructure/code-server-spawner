from .init import init_app

app = init_app()

from .routes.main import *
from .routes.cron import *
from .routes.auth import *

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
