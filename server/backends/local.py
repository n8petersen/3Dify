"""Wraps the existing worker-WebSocket flow behind the WorkerBackend seam.

The worker still holds one persistent outbound WebSocket connection.
WorkerBridge.handle_worker() routes job-scoped messages (job_progress,
job_generated_image, job_complete, job_failed) into per-job asyncio.Queues;
stream() below just drains the queue for its job.
"""

import asyncio
import base64
from typing import AsyncIterator

from models.job import Job
from services import storage


class LocalWebSocketBackend:
    name = "local"
    cost_per_second_usd = 0.0

    def __init__(self, bridge):
        self._bridge = bridge

    async def available(self) -> bool:
        b = self._bridge
        return bool(
            b.worker_connected
            and not b.paused
            and b.gpu_status.get("available", True)
        )

    async def dispatch(self, job: Job) -> str:
        b = self._bridge
        b._job_queues[job.id] = asyncio.Queue()

        if job.job_type == "text":
            await b.worker_ws.send_json({
                "type": "job_assign",
                "job_id": job.id,
                "job_type": "text",
                "prompt": job.prompt,
                "settings": job.settings,
            })
        else:
            upload_file = storage.get_upload_path(job.upload_path)
            if not upload_file.exists():
                b._job_queues.pop(job.id, None)
                raise FileNotFoundError(f"Upload file missing for job {job.id}")
            image_b64 = base64.b64encode(upload_file.read_bytes()).decode()
            await b.worker_ws.send_json({
                "type": "job_assign",
                "job_id": job.id,
                "job_type": "image",
                "image_filename": job.original_filename,
                "image_base64": image_b64,
                "settings": job.settings,
            })
        return job.id

    async def stream(self, handle: str) -> AsyncIterator[dict]:
        q = self._bridge._job_queues.get(handle)
        if q is None:
            return
        while True:
            msg = await q.get()
            yield msg
            if msg.get("type") in ("job_complete", "job_failed"):
                self._bridge._job_queues.pop(handle, None)
                return

    async def cancel(self, handle: str) -> bool:
        return await self._bridge.send_command("cancel", job_id=handle)

    async def billed_seconds(self, handle: str) -> float | None:
        return None
