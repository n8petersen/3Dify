"""RunPod Serverless backend — REST job submission + streaming progress.

Design: docs/ai-3d-model-generation.md §5.3 and §6 in the homelab repo.

The job-submission/streaming API used here is api.runpod.ai/v2/{endpoint_id}
— this is NOT the management REST API at rest.runpod.io/v1 that retires
2026-11-15 (that one is only used for provisioning endpoints/pods/volumes,
not for running jobs against an existing endpoint).

NOTE: the exact shape of /stream and /status responses (in particular
whether /status "output" for a generator handler with return_aggregate_stream
is the last yielded dict or a list of all yielded dicts) is not something
that could be verified without a live RunPod endpoint. The status-fallback
path below is defensive but should be exercised against a real endpoint
before relying on it — see docs/ai-3d-model-generation.md §10.1.
"""

import asyncio
import base64
import logging
import time
from typing import AsyncIterator

import httpx

from config import settings
from models.job import Job
from services import storage

logger = logging.getLogger("backends.runpod")

_BASE = "https://api.runpod.ai/v2"

STREAM_POLL_INTERVAL_S = 1.0
STATUS_POLL_INTERVAL_S = 2.0


def _build_job_assign_payload(job: Job) -> dict:
    if job.job_type == "text":
        return {
            "type": "job_assign",
            "job_id": job.id,
            "job_type": "text",
            "prompt": job.prompt,
            "settings": job.settings,
        }
    upload_file = storage.get_upload_path(job.upload_path)
    if not upload_file.exists():
        raise FileNotFoundError(f"Upload file missing for job {job.id}")
    image_b64 = base64.b64encode(upload_file.read_bytes()).decode()
    return {
        "type": "job_assign",
        "job_id": job.id,
        "job_type": "image",
        "image_filename": job.original_filename,
        "image_base64": image_b64,
        "settings": job.settings,
    }


class RunPodBackend:
    name = "runpod"

    def __init__(self):
        self.endpoint_id = settings.runpod_endpoint_id
        self._headers = {"Authorization": f"Bearer {settings.runpod_api_key}"}

    @property
    def cost_per_second_usd(self) -> float:
        return settings.runpod_cost_per_hour_usd / 3600

    async def available(self) -> bool:
        if not (settings.runpod_api_key and self.endpoint_id):
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{_BASE}/{self.endpoint_id}/health", headers=self._headers
                )
                return r.status_code == 200
        except Exception:
            logger.warning("RunPod health check failed", exc_info=True)
            return False

    async def dispatch(self, job: Job) -> str:
        body = {
            "input": _build_job_assign_payload(job),
            "policy": {
                "executionTimeout": settings.job_timeout_cloud_s * 1000,
                "ttl": 3600 * 1000,
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{_BASE}/{self.endpoint_id}/run", json=body, headers=self._headers
            )
            r.raise_for_status()
            return r.json()["id"]

    async def stream(self, handle: str) -> AsyncIterator[dict]:
        last_activity = time.monotonic()
        stalled = False
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                data = {}
                try:
                    r = await client.get(
                        f"{_BASE}/{self.endpoint_id}/stream/{handle}",
                        headers=self._headers,
                    )
                    r.raise_for_status()
                    data = r.json()
                except Exception:
                    logger.warning(
                        "RunPod stream poll failed for %s", handle, exc_info=True
                    )

                for chunk in data.get("stream") or []:
                    output = chunk.get("output")
                    if isinstance(output, dict) and output.get("type"):
                        last_activity = time.monotonic()
                        stalled = False
                        yield output
                        if output["type"] in ("job_complete", "job_failed"):
                            return

                status = data.get("status")
                if status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                    async for msg in self._status_fallback(client, handle, status):
                        yield msg
                    return

                if time.monotonic() - last_activity > settings.stream_stall_s:
                    if not stalled:
                        stalled = True
                        yield {
                            "type": "job_progress",
                            "step": "running",
                            "progress_pct": 50,
                            "message": "Streaming stalled — falling back to status polling on RunPod…",
                        }
                    async for msg in self._status_fallback(client, handle, None):
                        yield msg
                        if msg.get("type") in ("job_complete", "job_failed"):
                            return
                    await asyncio.sleep(STATUS_POLL_INTERVAL_S)
                    continue

                await asyncio.sleep(STREAM_POLL_INTERVAL_S)

    async def _status_fallback(
        self, client: httpx.AsyncClient, handle: str, status: str | None
    ) -> AsyncIterator[dict]:
        """Degraded mode C from §6 — plain /status polling."""
        if status is None:
            r = await client.get(
                f"{_BASE}/{self.endpoint_id}/status/{handle}", headers=self._headers
            )
            r.raise_for_status()
            data = r.json()
            status = data.get("status")
        else:
            data = {"status": status}

        if status == "COMPLETED":
            output = data.get("output")
            if isinstance(output, dict) and output.get("type") == "job_complete":
                yield output
            elif isinstance(output, dict):
                yield {"type": "job_complete", **output}
            else:
                yield {
                    "type": "job_failed",
                    "error": "RunPod job completed but returned no usable output",
                    "step": "runpod",
                }
        elif status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            yield {
                "type": "job_failed",
                "error": data.get("error") or f"RunPod job ended with status {status}",
                "step": "runpod",
            }

    async def cancel(self, handle: str) -> bool:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{_BASE}/{self.endpoint_id}/cancel/{handle}", headers=self._headers
            )
            return r.status_code == 200

    async def billed_seconds(self, handle: str) -> float | None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{_BASE}/{self.endpoint_id}/status/{handle}", headers=self._headers
                )
                r.raise_for_status()
                data = r.json()
            exec_ms = data.get("executionTime")
            return exec_ms / 1000 if exec_ms is not None else None
        except Exception:
            logger.warning(
                "Failed to fetch billed seconds for %s", handle, exc_info=True
            )
            return None
