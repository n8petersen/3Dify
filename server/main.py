import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import select

from config import settings
from database import create_db
from services.worker_bridge import WorkerBridge
from services import queue as queue_service
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession
from database import engine
from backends import BACKENDS, init_backends
from models.job import Job, JobStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
)
logger = logging.getLogger("server")


async def _cleanup_loop():
    """Periodically expire stale jobs and clean up expired sessions."""
    while True:
        try:
            await asyncio.sleep(settings.cleanup_interval_s)
            async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
                expired = await queue_service.expire_stale_jobs(session)
                if expired:
                    logger.info("Expired %d stale jobs: %s", len(expired), expired)

                from services.auth import cleanup_expired_sessions
                removed = await cleanup_expired_sessions(session)
                if removed:
                    logger.info("Cleaned up %d expired sessions", removed)

                cleaned = await queue_service.cleanup_old_job_files(session)
                if cleaned:
                    logger.info("Cleaned files for %d old jobs", cleaned)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in cleanup loop")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure directories exist
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.output_dir).mkdir(parents=True, exist_ok=True)

    import models.user  # noqa: F401
    import models.session  # noqa: F401

    # Create tables (dev convenience — use alembic in production)
    await create_db()

    # Singletons — WorkerBridge must exist before backends are initialized
    # (LocalWebSocketBackend holds a reference to it) and before the orphan
    # reconciliation below, which may reattach consumers for cloud jobs.
    bridge = WorkerBridge()
    app.state.worker_bridge = bridge
    init_backends(bridge)

    # Reconcile jobs left assigned/processing at shutdown. A cloud-backend
    # job may still be running remotely — reattach a consumer instead of
    # resetting it to pending, otherwise the dispatch loop would re-submit
    # (and double-bill) a job that's still executing on RunPod/GCP. Only
    # local/unknown-backend jobs get reset to pending.
    async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
        result = await session.execute(
            select(Job).where(Job.status.in_([JobStatus.assigned, JobStatus.processing]))
        )
        orphaned = result.scalars().all()
        reattached, requeued = 0, 0
        for job in orphaned:
            if job.backend and job.backend != "local" and job.backend_job_id:
                backend = BACKENDS.get(job.backend)
                if backend is not None:
                    asyncio.create_task(bridge._consume(backend, job.id, job.backend_job_id))
                    reattached += 1
                    continue
            job.status = JobStatus.pending
            job.assigned_at = None
            job.current_step = None
            job.progress_pct = 0
            job.progress_message = None
            requeued += 1
        if orphaned:
            await session.commit()
            logger.info(
                "Startup: reattached %d cloud job(s), re-queued %d local/unknown job(s)",
                reattached, requeued,
            )

    # Background tasks
    cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info("Server started")

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("Server stopped")


app = FastAPI(title="PictureToPrintable", version="0.1.0", lifespan=lifespan)

# CORS — only allow credentials when origins are explicitly configured
_has_wildcard = "*" in settings.cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=not _has_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
from routes.worker_ws import router as worker_ws_router
from routes.client_ws import router as client_ws_router
from routes.jobs import router as jobs_router
from routes.auth import router as auth_router
from routes.admin import router as admin_router
from routes.admin_ws import router as admin_ws_router
from routes.feedback import router as feedback_router
from routes.gallery import router as gallery_router

app.include_router(worker_ws_router)
app.include_router(client_ws_router)
app.include_router(jobs_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(admin_ws_router)
app.include_router(feedback_router)
app.include_router(gallery_router)


@app.get("/health")
async def health():
    bridge = app.state.worker_bridge
    is_local = settings.worker_backend == "local"
    return {
        "status": "ok",
        "worker_connected": bridge.worker_connected if is_local else bridge.backend_available,
        "backend": settings.worker_backend,
        "paused": bridge.paused,
    }
