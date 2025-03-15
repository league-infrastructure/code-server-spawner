from cspawn.models import User, Class, CodeHost
from typing import Tuple


def host_class_state(user: User, class_: Class) -> str:
    """Return the state of the host for the given class."""

    assert class_ is not None

    host = CodeHost.query.filter_by(user_id=user.id).first()  # extant code host

    class_image = class_.image_id
    host_image = host.host_image_id if host else None

    if not host:
        # There is no host running
        return "stopped"

    elif host and class_image == host_image:
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
    else:
        return []
