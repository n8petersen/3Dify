from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import _is_sqlite
from models.job import Job, JobStatus
from services import storage


async def enqueue(session: AsyncSession, job: Job) -> Job:
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_next_pending(session: AsyncSession) -> Job | None:
    """Atomically claim the oldest pending job.

    Uses FOR UPDATE SKIP LOCKED on Postgres for concurrency safety.
    Falls back to simple select on SQLite (single-writer anyway).
    """
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.pending)
        .order_by(Job.created_at)
        .limit(1)
    )
    if not _is_sqlite:
        stmt = stmt.with_for_update(skip_locked=True)
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()
    if job:
        job.status = JobStatus.assigned
        job.assigned_at = datetime.now(timezone.utc)
        job.current_step = "waiting_gpu"
        await session.commit()
        await session.refresh(job)
    return job


async def mark_processing(session: AsyncSession, job_id: str) -> None:
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if job:
        job.status = JobStatus.processing
        await session.commit()


async def mark_complete(
    session: AsyncSession,
    job_id: str,
    *,
    stl_path: str,
    glb_path: str | None = None,
    vertex_count: int,
    face_count: int,
    is_watertight: bool,
    generation_time_s: float,
    gpu_metrics: dict | None = None,
) -> Job | None:
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        return None
    job.status = JobStatus.complete
    job.stl_path = stl_path
    job.glb_path = glb_path
    job.vertex_count = vertex_count
    job.face_count = face_count
    job.is_watertight = is_watertight
    job.generation_time_s = generation_time_s
    job.gpu_metrics = gpu_metrics
    job.completed_at = datetime.now(timezone.utc)
    job.progress_pct = 100
    job.current_step = "complete"
    await session.commit()
    await session.refresh(job)
    return job


async def mark_failed(
    session: AsyncSession, job_id: str, *, error: str, step: str | None = None
) -> Job | None:
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        return None
    job.status = JobStatus.failed
    job.error_message = error
    job.error_step = step
    job.completed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(job)
    return job


async def expire_stale_jobs(session: AsyncSession) -> list[str]:
    """Mark assigned/processing jobs as expired if they've timed out.

    Cloud backends get a longer cutoff (job_timeout_cloud_s) — cold starts
    and endpoint queueing can legitimately take longer than a warm local
    worker picking a job off the queue. Cutoffs differ per row so this
    filters in Python rather than in the query.
    """
    now = datetime.now(timezone.utc)
    local_cutoff = now - timedelta(seconds=settings.job_timeout_s)
    cloud_cutoff = now - timedelta(seconds=settings.job_timeout_cloud_s)
    result = await session.execute(
        select(Job).where(Job.status.in_([JobStatus.assigned, JobStatus.processing]))
    )
    expired_ids = []
    for job in result.scalars().all():
        cutoff = local_cutoff if (job.backend or "local") == "local" else cloud_cutoff
        if job.assigned_at and job.assigned_at < cutoff:
            job.status = JobStatus.expired
            job.error_message = "Job timed out"
            job.completed_at = datetime.now(timezone.utc)
            expired_ids.append(job.id)
    if expired_ids:
        await session.commit()
    return expired_ids


async def set_backend(session: AsyncSession, job_id: str, *, backend: str, backend_job_id: str) -> None:
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if job:
        job.backend = backend
        job.backend_job_id = backend_job_id
        await session.commit()


async def reset_to_pending(session: AsyncSession, job_id: str) -> None:
    """Put a claimed job back in the queue (e.g. its backend turned out unavailable)."""
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if job:
        job.status = JobStatus.pending
        job.assigned_at = None
        job.current_step = None
        await session.commit()


async def set_cost(session: AsyncSession, job_id: str, *, billed_seconds: float, backend_cost_usd: float) -> None:
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if job:
        job.billed_seconds = billed_seconds
        job.backend_cost_usd = backend_cost_usd
        await session.commit()


async def pending_count(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.pending)
    )
    return result.scalar_one()


async def get_queue_summary(session: AsyncSession) -> dict:
    """Return counts by status."""
    result = await session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    return {s.value: counts.get(s, 0) for s in JobStatus}


async def cleanup_old_job_files(session: AsyncSession) -> int:
    """Delete files for old completed/failed/expired jobs, keeping gallery items."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.file_retention_days)
    result = await session.execute(
        select(Job).where(
            Job.status.in_([JobStatus.complete, JobStatus.failed, JobStatus.expired]),
            Job.completed_at < cutoff,
            or_(Job.feedback_rating.is_(None), Job.feedback_rating < 4),
            # Has at least one file path still set
            or_(
                Job.upload_path != "",
                Job.stl_path.isnot(None),
                Job.glb_path.isnot(None),
                Job.thumbnail_path.isnot(None),
                Job.generated_image_path.isnot(None),
            ),
        )
    )
    cleaned = 0
    for job in result.scalars().all():
        storage.delete_job_files(
            upload_path=job.upload_path or None,
            stl_path=job.stl_path,
            glb_path=job.glb_path,
            thumbnail_path=job.thumbnail_path,
            generated_image_path=job.generated_image_path,
        )
        job.upload_path = ""  # NOT NULL column — use empty string
        job.stl_path = None
        job.glb_path = None
        job.thumbnail_path = None
        job.generated_image_path = None
        cleaned += 1
    if cleaned:
        await session.commit()
    return cleaned
