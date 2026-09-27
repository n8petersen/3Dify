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

import boto3
import httpx

from config import settings
from models.job import Job
from services import storage

logger = logging.getLogger("backends.runpod")

_BASE = "https://api.runpod.ai/v2"

STREAM_POLL_INTERVAL_S = 1.0
STATUS_POLL_INTERVAL_S = 2.0


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.runpod_s3_endpoint,
        region_name=settings.runpod_s3_region,
        aws_access_key_id=settings.runpod_s3_access_key,
        aws_secret_access_key=settings.runpod_s3_secret_key,
    )


def _fetch_and_delete(key: str) -> bytes:
    """Blocking — boto3 has no async client. Call via asyncio.to_thread.

    Deletes the object from the network volume immediately after reading it,
    so results don't accumulate there indefinitely (runpod_handler.py writes
    the STL/GLB there because inlining them as base64 in the job-result
    payload is too large for RunPod's job-result API — see stream() below).
    """
    client = _s3_client()
    bucket = settings.runpod_network_volume_id
    data = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    client.delete_object(Bucket=bucket, Key=key)
    return data


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

    async def _resolve_job_complete(self, output: dict) -> dict:
        """job_complete arrives with stl_key/glb_key — files runpod_handler.py
        wrote to the network volume — instead of stl_base64/glb_base64,
        because a real mesh's base64 STL (tens of MB) exceeds RunPod's
        job-result API size limit. Fetch by key via the volume's
        S3-compatible API, delete once we have the bytes, and reassemble the
        base64 shape worker_bridge.py already expects so nothing downstream
        needs to change."""
        if output.get("type") != "job_complete" or "stl_key" not in output:
            return output
        resolved = dict(output)
        stl_key = resolved.pop("stl_key")
        glb_key = resolved.pop("glb_key", None)
        try:
            stl_bytes = await asyncio.to_thread(_fetch_and_delete, stl_key)
            resolved["stl_filename"] = "model.stl"
            resolved["stl_base64"] = base64.b64encode(stl_bytes).decode()
            if glb_key:
                glb_bytes = await asyncio.to_thread(_fetch_and_delete, glb_key)
                resolved["glb_filename"] = "model.glb"
                resolved["glb_base64"] = base64.b64encode(glb_bytes).decode()
            else:
                resolved["glb_filename"] = None
                resolved["glb_base64"] = None
        except Exception:
            logger.exception(
                "Failed to fetch job result from RunPod network volume (key=%s)",
                stl_key,
            )
            return {
                "type": "job_failed",
                "error": "Failed to retrieve generated model from RunPod storage",
                "step": "runpod",
            }
        return resolved

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
                        output = await self._resolve_job_complete(output)
                        yield output
                        if output["type"] in ("job_complete", "job_failed"):
                            return

                status = data.get("status")
                if status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                    # Re-fetch from /status rather than reusing `status` bare —
                    # the /stream response doesn't carry the "output"/"error"
                    # payload, so passing it through would lose the real error
                    # detail (or completed output) in favor of a generic message.
                    async for msg in self._status_fallback(client, handle, None):
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
                yield await self._resolve_job_complete(output)
            elif isinstance(output, dict):
                yield await self._resolve_job_complete({"type": "job_complete", **output})
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
