from .init import init_app, db
from flask_migrate import Migrate

app = init_app()

migrate = Migrate(app, db)

from main.routes.main import *
from main.routes.cron import *


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")