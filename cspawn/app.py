from .init import init_app, db
from flask_migrate import Migrate

app = init_app()

migrate = Migrate(app, db)

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
