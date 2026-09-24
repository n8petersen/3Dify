"""Backend registry.

BACKENDS is populated once at startup by init_backends(), after the
WorkerBridge singleton exists (LocalWebSocketBackend holds a reference to
it). Everything else — dispatch loop, admin routes — looks backends up by
name through get_backend() rather than importing a concrete class.
"""

from typing import Optional

from config import settings

from .base import BackendError, WorkerBackend
from .local import LocalWebSocketBackend
from .runpod import RunPodBackend

BACKENDS: dict[str, WorkerBackend] = {}


def init_backends(bridge) -> None:
    BACKENDS.clear()
    for name in settings.enabled_backends:
        if name == "local":
            BACKENDS["local"] = LocalWebSocketBackend(bridge)
        elif name == "runpod":
            BACKENDS["runpod"] = RunPodBackend()
        else:
            raise ValueError(f"Unknown backend in ENABLED_BACKENDS: {name!r}")


def get_backend(name: Optional[str] = None) -> WorkerBackend:
    name = name or settings.worker_backend
    backend = BACKENDS.get(name)
    if backend is None:
        raise BackendError(f"Backend {name!r} is not enabled (ENABLED_BACKENDS)")
    return backend
