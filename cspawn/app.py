
from cspawn.init import init_app
import logging


# sweep_node_ops=True is intentional and MUST stay only here. This module is
# the one true process-boot call site: Gunicorn's preload_app=True
# (docker/gunicorn_config.py) guarantees it is imported exactly once per
# container start, before workers fork. That uniqueness is what makes the
# boot-time NodeOp sweep (cspawn/models.py::sweep_interrupted_node_ops) safe
# to run unconditionally here but unsafe anywhere else: every other
# init_app(...) call site (cspawn/cli/util.py::get_app, used by every
# cspawnctl subcommand including op-run itself) keeps the default
# sweep_node_ops=False, or a real in-flight op-run could be falsely marked
# 'interrupted' by an unrelated CLI invocation. See sprint 010's
# architecture-update.md, Step 6, for the full rationale.
app = init_app(sweep_node_ops=True)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
