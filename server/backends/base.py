"""The WorkerBackend seam.

Every backend normalises its native protocol into 3Dify's existing
worker-WebSocket message dicts (job_progress / job_generated_image /
job_complete / job_failed). Everything downstream of
WorkerBridge._handle_worker_message() stays backend-agnostic.
"""

from typing import AsyncIterator, Protocol, runtime_checkable

from models.job import Job


@runtime_checkable
class WorkerBackend(Protocol):
    name: str
    cost_per_second_usd: float

    async def available(self) -> bool:
        """Can this backend accept a job right now?"""
        ...

    async def dispatch(self, job: Job) -> str:
        """Submit the job. Returns an opaque backend handle used by
        stream()/cancel()/billed_seconds()."""
        ...

    def stream(self, handle: str) -> AsyncIterator[dict]:
        """Yield worker-protocol message dicts until a terminal message
        (job_complete or job_failed)."""
        ...

    async def cancel(self, handle: str) -> bool:
        ...

    async def billed_seconds(self, handle: str) -> float | None:
        """Backend-reported billable time, if available. None means the
        caller should fall back to its own wall-clock measurement."""
        ...


class BackendError(Exception):
    """Raised when a backend fails outside the normal job_failed message path
    (e.g. the HTTP call to submit the job itself fails)."""
