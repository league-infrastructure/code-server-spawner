
from cspawn.init import init_app
import logging


app = init_app()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
