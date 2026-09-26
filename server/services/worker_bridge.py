import asyncio
import base64
import logging
import time
from datetime import datetime, timezone

from fastapi import WebSocket
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from config import settings
from database import engine
from models.audit_log import AuditLog
from services import queue, storage

logger = logging.getLogger("worker_bridge")

_JOB_SCOPED_TYPES = {"job_progress", "job_generated_image", "job_complete", "job_failed"}


def _serialize_job_for_admin(job) -> dict:
    """Compact job summary for the admin activity feed."""
    return {
        "id": job.id,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "job_type": job.job_type,
        "prompt": job.prompt,
        "original_filename": job.original_filename,
        "client_ip": job.client_ip,
        "thumbnail_url": f"/api/job/{job.id}/thumbnail" if job.thumbnail_path else None,
        "generated_image_url": f"/api/job/{job.id}/generated-image" if job.generated_image_path else None,
        "glb_url": f"/api/job/{job.id}/glb" if job.glb_path else None,
        "stl_url": f"/api/job/{job.id}/stl" if job.stl_path else None,
        "vertex_count": job.vertex_count,
        "face_count": job.face_count,
        "is_watertight": job.is_watertight,
        "generation_time_s": job.generation_time_s,
        "progress_pct": job.progress_pct,
        "current_step": job.current_step,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


class WorkerBridge:
    """Manages the worker WebSocket connection, job dispatch, and client fan-out."""

    def __init__(self):
        self.worker_ws: WebSocket | None = None
        self.worker_info: dict = {}
        self.gpu_status: dict = {}
        self.paused: bool = False
        # Cloud backends (RunPod, etc.) have no persistent connection to be
        # "connected" — this tracks the last `backend.available()` health
        # check made by the dispatch loop instead, so status endpoints don't
        # have to make an extra live call on every request.
        self.backend_available: bool = False

        # Client progress subscriptions: job_id -> set of WebSocket connections
        self._subscribers: dict[str, set[WebSocket]] = {}
        # Admin activity-feed subscribers (receive ALL events)
        self._admin_subscribers: set[WebSocket] = set()
        self._dispatch_task: asyncio.Task | None = None

        # Per-job message queues, used by backends/local.py's stream() so
        # every backend (local WS worker or a cloud one) is consumed the
        # same way by _consume() below. Keyed by job_id.
        self._job_queues: dict[str, asyncio.Queue] = {}

    # ─── Client subscription ───────────────────────────────────────

    def subscribe(self, job_id: str, ws: WebSocket) -> None:
        self._subscribers.setdefault(job_id, set()).add(ws)

    def unsubscribe(self, job_id: str, ws: WebSocket) -> None:
        subs = self._subscribers.get(job_id)
        if subs:
            subs.discard(ws)
            if not subs:
                del self._subscribers[job_id]

    async def _fan_out(self, job_id: str, message: dict) -> None:
        """Send message to all client WebSockets subscribed to this job."""
        subs = self._subscribers.get(job_id, set()).copy()
        for ws in subs:
            try:
                await ws.send_json(message)
            except Exception:
                self.unsubscribe(job_id, ws)

    # ─── Admin activity feed ───────────────────────────────────────

    def subscribe_admin(self, ws: WebSocket) -> None:
        self._admin_subscribers.add(ws)

    def unsubscribe_admin(self, ws: WebSocket) -> None:
        self._admin_subscribers.discard(ws)

    async def broadcast_admin(self, message: dict) -> None:
        """Send an event to every admin activity-feed subscriber."""
        # Tag with timestamp so the UI can sort/age cards consistently
        message = {**message, "ts": datetime.now(timezone.utc).isoformat()}
        for ws in list(self._admin_subscribers):
            try:
                await ws.send_json(message)
            except Exception:
                self._admin_subscribers.discard(ws)

    # ─── Worker connection ─────────────────────────────────────────

    @property
    def worker_connected(self) -> bool:
        return self.worker_ws is not None

    async def handle_worker(self, ws: WebSocket) -> None:
        """Main loop for the worker WebSocket — call from the route handler."""
        if self.worker_ws is not None:
            await ws.close(code=4000, reason="Another worker already connected")
            return

        self.worker_ws = ws
        await ws.send_json({"type": "welcome", "message": "Connected to server"})
        logger.info("Worker connected")

        try:
            async for raw in ws.iter_json():
                msg_type = raw.get("type")
                job_id = raw.get("job_id")
                job_queue = self._job_queues.get(job_id) if job_id else None
                if msg_type in _JOB_SCOPED_TYPES and job_queue is not None:
                    # A backend's _consume() task is draining this job via
                    # stream() — hand it off instead of handling inline.
                    await job_queue.put(raw)
                else:
                    await self._handle_worker_message(raw)
        except Exception as e:
            logger.warning("Worker disconnected: %s", e)
        finally:
            self.worker_ws = None
            self.worker_info = {}
            self.gpu_status = {}
            logger.info("Worker disconnected, cleaned up")

    async def _handle_worker_message(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "worker_hello":
            self.worker_info = {
                "gpu_name": msg.get("gpu_name"),
                "vram_total_gb": msg.get("vram_total_gb"),
                "worker_version": msg.get("worker_version"),
            }
            logger.info("Worker hello: %s", self.worker_info)

        elif msg_type == "gpu_status":
            self.gpu_status = {
                "vram_free_gb": msg.get("vram_free_gb"),
                "vram_used_gb": msg.get("vram_used_gb"),
                "vram_total_gb": msg.get("vram_total_gb"),
                "utilization_pct": msg.get("utilization_pct"),
                "temp_c": msg.get("temp_c"),
                "available": msg.get("available"),
                "model_loaded": msg.get("model_loaded"),
            }

        elif msg_type in _JOB_SCOPED_TYPES:
            # Fallback path — normally these are routed to a per-job queue
            # in handle_worker() and consumed by _consume() instead. This
            # only runs if a job-scoped message arrives with no matching
            # queue (e.g. a race on dispatch/disconnect).
            await self._process_job_message(msg)

        elif msg_type == "pong":
            pass  # Heartbeat response

        elif msg_type == "worker_bye":
            logger.info("Worker sent bye: %s", msg.get("reason"))

    async def _process_job_message(self, msg: dict) -> None:
        """Apply one worker-protocol job message: DB update + client fan-out.

        Called for every backend, whether the message came from the local
        WS worker's queue or a cloud backend's stream().
        """
        msg_type = msg.get("type")

        if msg_type == "job_progress":
            job_id = msg.get("job_id")
            if job_id:
                await self._update_progress(
                    job_id,
                    step=msg.get("step"),
                    pct=msg.get("progress_pct", 0),
                    message=msg.get("message"),
                )
                progress_event = {
                    "type": "progress",
                    "job_id": job_id,
                    "step": msg.get("step"),
                    "progress_pct": msg.get("progress_pct", 0),
                    "message": msg.get("message"),
                }
                await self._fan_out(job_id, progress_event)
                await self.broadcast_admin({**progress_event, "type": "job_progress"})

        elif msg_type == "job_generated_image":
            await self._handle_generated_image(msg)

        elif msg_type == "job_complete":
            await self._handle_job_complete(msg)

        elif msg_type == "job_failed":
            await self._handle_job_failed(msg)

    # ─── Job lifecycle ─────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        """Periodically check for pending jobs and dispatch to a backend."""
        from backends import BACKENDS

        while True:
            try:
                await asyncio.sleep(2)

                if self.paused:
                    continue

                if not settings.allow_per_job_backend:
                    # Fast path — single configured backend, no per-job
                    # DB read needed to know which one to check.
                    backend = BACKENDS.get(settings.worker_backend)
                    self.backend_available = backend is not None and await backend.available()
                    if not self.backend_available:
                        continue
                    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                        job = await queue.get_next_pending(session)
                        if not job:
                            continue
                        await self._dispatch_to_backend(session, job, backend)
                    continue

                async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                    job = await queue.get_next_pending(session)
                    if not job:
                        continue
                    backend = BACKENDS.get(job.backend or settings.worker_backend)
                    if backend is None or not await backend.available():
                        # Not our turn — put it back for the next pass.
                        await queue.reset_to_pending(session, job.id)
                        continue
                    await self._dispatch_to_backend(session, job, backend)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in dispatch loop")
                await asyncio.sleep(5)

    async def _dispatch_to_backend(self, session, job, backend) -> None:
        try:
            if job.job_type == "image":
                upload_file = storage.get_upload_path(job.upload_path)
                if not upload_file.exists():
                    await queue.mark_failed(session, job.id, error="Upload file missing", step="queued")
                    return
            handle = await backend.dispatch(job)
        except FileNotFoundError as e:
            await queue.mark_failed(session, job.id, error=str(e), step="queued")
            return
        except Exception as e:
            logger.exception("Dispatch to backend %s failed for job %s", backend.name, job.id)
            await queue.mark_failed(session, job.id, error=f"Dispatch failed: {e}", step="queued")
            return

        await queue.set_backend(session, job.id, backend=backend.name, backend_job_id=handle)
        await self.broadcast_admin({"type": "job_assigned", "job_id": job.id, "backend": backend.name})
        logger.info("Dispatched %s job %s to backend %s (handle=%s)", job.job_type, job.id, backend.name, handle)

        asyncio.create_task(self._consume(backend, job.id, handle))

    async def _consume(self, backend, job_id: str, handle: str) -> None:
        """Drain a backend's stream for one job, applying each message the
        same way regardless of which backend produced it."""
        start = time.monotonic()
        try:
            async for msg in backend.stream(handle):
                await self._process_job_message(msg)
        except Exception:
            logger.exception("Error consuming stream for job %s on backend %s", job_id, backend.name)
            async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                await queue.mark_failed(session, job_id, error="Backend stream error", step=backend.name)
            await self._fan_out(job_id, {"type": "failed", "job_id": job_id, "error": "Backend stream error"})
            await self.broadcast_admin({"type": "job_failed", "job_id": job_id, "error": "Backend stream error"})
        finally:
            await self._record_cost(backend, job_id, handle, start)

    async def _record_cost(self, backend, job_id: str, handle: str, start: float) -> None:
        try:
            billed = await backend.billed_seconds(handle)
            if billed is None:
                billed = time.monotonic() - start
            cost = billed * backend.cost_per_second_usd
            async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                await queue.set_cost(session, job_id, billed_seconds=billed, backend_cost_usd=cost)
        except Exception:
            logger.exception("Failed to record cost for job %s", job_id)

    async def _update_progress(
        self, job_id: str, step: str | None, pct: int, message: str | None
    ) -> None:
        try:
            async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                from sqlalchemy import select
                from models.job import Job, JobStatus

                result = await session.execute(select(Job).where(Job.id == job_id))
                job = result.scalar_one_or_none()
                if job:
                    if job.status == JobStatus.assigned:
                        job.status = JobStatus.processing
                    job.current_step = step
                    job.progress_pct = pct
                    job.progress_message = message
                    await session.commit()
        except Exception:
            logger.exception("Failed to update progress for %s", job_id)

    async def _handle_generated_image(self, msg: dict) -> None:
        """Handle the FLUX-generated intermediate image from the worker."""
        job_id = msg.get("job_id")
        if not job_id:
            return

        try:
            image_b64 = msg.get("image_base64")
            if not image_b64:
                return

            # Save image to uploads dir
            image_data = base64.b64decode(image_b64)
            image_rel = f"{job_id}/generated.png"
            storage.save_upload(image_data, image_rel)

            # Update DB
            async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                from sqlalchemy import select as sa_select
                from models.job import Job as JobModel

                result = await session.execute(sa_select(JobModel).where(JobModel.id == job_id))
                job = result.scalar_one_or_none()
                if job:
                    job.generated_image_path = image_rel
                    await session.commit()

            # Fan out to subscribed clients
            await self._fan_out(job_id, {
                "type": "generated_image",
                "job_id": job_id,
                "url": f"/api/job/{job_id}/generated-image",
            })
            await self.broadcast_admin({
                "type": "job_generated_image",
                "job_id": job_id,
                "url": f"/api/job/{job_id}/generated-image",
            })
            logger.info("Saved generated image for job %s", job_id)

        except Exception:
            logger.exception("Error handling generated_image for %s", job_id)

    async def _handle_job_complete(self, msg: dict) -> None:
        job_id = msg.get("job_id")
        if not job_id:
            return

        try:
            # Save STL file
            stl_b64 = msg.get("stl_base64")
            stl_rel = None
            if stl_b64:
                stl_data = base64.b64decode(stl_b64)
                stl_rel = f"{job_id}/model.stl"
                storage.save_output(stl_data, stl_rel)

            # Save GLB file (optional)
            glb_b64 = msg.get("glb_base64")
            glb_rel = None
            if glb_b64:
                glb_data = base64.b64decode(glb_b64)
                glb_rel = f"{job_id}/model.glb"
                storage.save_output(glb_data, glb_rel)

            # Update DB
            async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                job = await queue.mark_complete(
                    session,
                    job_id,
                    stl_path=stl_rel,
                    glb_path=glb_rel,
                    vertex_count=msg.get("vertex_count", 0),
                    face_count=msg.get("face_count", 0),
                    is_watertight=msg.get("is_watertight", False),
                    generation_time_s=msg.get("generation_time_s", 0),
                    gpu_metrics=msg.get("gpu_metrics"),
                )

                # Audit log
                session.add(AuditLog(
                    action="job_complete", job_id=job_id,
                    detail=f"vertices={msg.get('vertex_count')}"
                ))
                await session.commit()

            # Notify clients
            await self._fan_out(job_id, {
                "type": "complete",
                "job_id": job_id,
                "vertex_count": msg.get("vertex_count"),
                "face_count": msg.get("face_count"),
                "is_watertight": msg.get("is_watertight"),
                "generation_time_s": msg.get("generation_time_s"),
            })
            await self.broadcast_admin({
                "type": "job_complete",
                "job_id": job_id,
                "vertex_count": msg.get("vertex_count"),
                "face_count": msg.get("face_count"),
                "is_watertight": msg.get("is_watertight"),
                "generation_time_s": msg.get("generation_time_s"),
                "glb_url": f"/api/job/{job_id}/glb" if glb_rel else None,
                "stl_url": f"/api/job/{job_id}/stl" if stl_rel else None,
            })
            logger.info("Job %s complete (%d vertices)", job_id, msg.get("vertex_count", 0))

        except Exception:
            logger.exception("Error handling job_complete for %s", job_id)

    async def _handle_job_failed(self, msg: dict) -> None:
        job_id = msg.get("job_id")
        if not job_id:
            return

        error = msg.get("error", "Unknown error")
        step = msg.get("step")

        try:
            async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                await queue.mark_failed(session, job_id, error=error, step=step)
                session.add(AuditLog(
                    action="job_failed", job_id=job_id, detail=error
                ))
                await session.commit()

            await self._fan_out(job_id, {
                "type": "failed",
                "job_id": job_id,
                "error": error,
                "step": step,
            })
            await self.broadcast_admin({
                "type": "job_failed",
                "job_id": job_id,
                "error": error,
                "step": step,
            })
            logger.warning("Job %s failed at %s: %s", job_id, step, error)

        except Exception:
            logger.exception("Error handling job_failed for %s", job_id)

    # ─── Admin commands ────────────────────────────────────────────

    async def send_command(self, action: str, job_id: str | None = None) -> bool:
        """Send a command to the worker. Returns True if sent."""
        if not self.worker_ws:
            return False
        msg = {"type": "command", "action": action}
        if job_id:
            msg["job_id"] = job_id
        await self.worker_ws.send_json(msg)
        return True

    async def send_ping(self) -> bool:
        if not self.worker_ws:
            return False
        await self.worker_ws.send_json({"type": "ping"})
        return True

    # ─── Public broadcast helpers (for routes) ─────────────────────

    async def notify_job_created(self, job) -> None:
        """Broadcast a newly-created job to admin activity-feed subscribers."""
        await self.broadcast_admin({
            "type": "job_created",
            "job": _serialize_job_for_admin(job),
        })
