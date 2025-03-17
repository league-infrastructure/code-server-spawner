from math import e
from cspawn.models import User, Class, CodeHost
from typing import Tuple


def host_class_state(user: User, class_: Class) -> str:
    """Return the state of the host for the given class."""

    assert class_ is not None

    host = CodeHost.query.filter_by(user_id=user.id).first()  # extant code host

    if not host:
        if class_.running:
            return "stopped"  # There is no host running
        else:
            return "waiting"  # Waiting for class to start

    elif host and class_.id == host.class_id:
        if host.app_state == "ready":
            # There is a host running, and it is for this class
            return "running"
        else:
            return "starting"
    else:
        # There is a host running, but it is not for this class"
        return "other"


def which_host_buttons(state: str) -> Tuple[str]:
    if state == "stopped":
        return ("start",)
    elif state == "running":
        return ("open", "stop")
    elif state == "starting":
        return ("spin",)
    elif state == "other":
        return ("stop", "other")
    elif state == "waiting":
        return ("waiting",)
    else:
        return []
