from flask_migrate import Migrate

from .init import db, init_app

app = init_app()

migrate = Migrate(app, db)

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
