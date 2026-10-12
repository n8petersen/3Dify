import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_session
from models.audit_log import AuditLog
from models.job import Job, JobStatus
from models.user import User
from services import image_validator, queue, rate_limiter, storage
from services.auth import get_optional_user, require_user

router = APIRouter(prefix="/api")


class GenerateRequest(BaseModel):
    prompt: str = PydanticField(..., min_length=1, max_length=500)


def _client_ip(request: Request) -> str:
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _safe_download_name(name: str) -> str:
    """Sanitize a name for use in Content-Disposition headers.

    Strips characters that could corrupt or inject into HTTP headers.
    Keeps only word chars (letters, digits, underscore), whitespace, and hyphens.
    Collapses runs of whitespace into a single space and falls back to 'download'
    if nothing remains.
    """
    safe = re.sub(r'[^\w\s-]', '', name)
    safe = re.sub(r'\s+', ' ', safe).strip()
    return safe or 'download'


@router.post("/upload")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    user: Optional[User] = Depends(get_optional_user),
):
    ip = _client_ip(request)

    # Check ban
    if await rate_limiter.is_banned(session, ip):
        raise HTTPException(403, "IP banned")

    # Rate limit by user_id if logged in, otherwise by IP
    allowed, remaining = await rate_limiter.check_rate_limit(session, ip, user_id=user.id if user else None)
    if not allowed:
        raise HTTPException(429, f"Rate limit exceeded. Try again in 24 hours.")

    # Check queue capacity
    pending = await queue.pending_count(session)
    if pending >= settings.max_pending_jobs:
        raise HTTPException(503, "Queue is full. Please try again later.")

    # Read file data
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")

    # Validate image
    try:
        cleaned, sha256, ext = image_validator.validate_and_process(
            data, file.filename or "upload"
        )
    except image_validator.ImageValidationError as e:
        raise HTTPException(400, str(e))

    # Create job record first to get ID
    job = Job(
        original_filename=file.filename or "upload",
        upload_path="",  # filled below
        image_hash=sha256,
        client_ip=ip,
        user_agent=request.headers.get("user-agent"),
        user_id=user.id if user else None,
        settings={
            "steps": settings.default_steps,
            "guidance": settings.default_guidance,
            "octree_res": settings.default_octree_res,
            "seed": settings.default_seed,
            "height_mm": settings.default_height_mm,
        },
    )
    job = await queue.enqueue(session, job)

    # Save file with job ID in path
    upload_rel = f"{job.id}/input.{ext}"
    storage.save_upload(cleaned, upload_rel)
    job.upload_path = upload_rel

    # Generate thumbnail
    thumb_rel = f"{job.id}/thumb.jpg"
    thumb_path = storage.get_upload_path(thumb_rel)
    try:
        image_validator.make_thumbnail(cleaned, thumb_path)
        job.thumbnail_path = thumb_rel
    except Exception:
        pass  # Non-critical

    await session.commit()
    await session.refresh(job)

    # Audit log
    session.add(AuditLog(action="upload", client_ip=ip, job_id=job.id))
    await session.commit()
    rate_limiter.invalidate_cache(f"user:{user.id}" if user else ip)

    # Notify admin activity feed
    try:
        await request.app.state.worker_bridge.notify_job_created(job)
    except Exception:
        pass  # never break upload on admin-feed failure

    return {
        "job_id": job.id,
        "status": job.status.value,
        "queue_position": pending + 1,
        "remaining_uploads": remaining - 1,
    }


@router.post("/generate")
async def generate_from_text(
    request: Request,
    body: GenerateRequest,
    session: AsyncSession = Depends(get_session),
    user: Optional[User] = Depends(get_optional_user),
):
    ip = _client_ip(request)

    # Check ban
    if await rate_limiter.is_banned(session, ip):
        raise HTTPException(403, "IP banned")

    # Rate limit (same pool as uploads)
    allowed, remaining = await rate_limiter.check_rate_limit(session, ip, user_id=user.id if user else None)
    if not allowed:
        raise HTTPException(429, "Rate limit exceeded. Try again in 24 hours.")

    # Check queue capacity
    pending = await queue.pending_count(session)
    if pending >= settings.max_pending_jobs:
        raise HTTPException(503, "Queue is full. Please try again later.")

    prompt = body.prompt.strip()
    if len(prompt) > settings.max_prompt_length:
        raise HTTPException(400, f"Prompt too long (max {settings.max_prompt_length} chars)")

    # Create text job — sentinel values for image-only NOT NULL columns
    job = Job(
        job_type="text",
        prompt=prompt,
        original_filename=prompt[:80],
        upload_path="",
        image_hash="",
        client_ip=ip,
        user_agent=request.headers.get("user-agent"),
        user_id=user.id if user else None,
        settings={
            "steps": settings.default_steps,
            "guidance": settings.default_guidance,
            "octree_res": settings.default_octree_res,
            "seed": settings.default_seed,
            "height_mm": settings.default_height_mm,
            "flux_steps": settings.default_flux_steps,
            "flux_guidance": settings.default_flux_guidance,
        },
    )
    job = await queue.enqueue(session, job)

    # Audit log
    session.add(AuditLog(action="generate_text", client_ip=ip, job_id=job.id))
    await session.commit()
    rate_limiter.invalidate_cache(f"user:{user.id}" if user else ip)

    # Notify admin activity feed
    try:
        await request.app.state.worker_bridge.notify_job_created(job)
    except Exception:
        pass

    return {
        "job_id": job.id,
        "status": job.status.value,
        "queue_position": pending + 1,
        "remaining_uploads": remaining - 1,
    }


@router.get("/job/{job_id}")
async def get_job(job_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    resp = {
        "job_id": job.id,
        "status": job.status.value,
        "job_type": job.job_type,
        "original_filename": job.original_filename,
        "settings": job.settings,
        "current_step": job.current_step,
        "progress_pct": job.progress_pct,
        "progress_message": job.progress_message,
        "created_at": job.created_at.isoformat(),
    }

    if job.job_type == "text":
        resp["prompt"] = job.prompt
        if job.generated_image_path:
            resp["generated_image_url"] = f"/api/job/{job.id}/generated-image"

    # Add queue position for pending jobs
    if job.status == JobStatus.pending:
        from sqlalchemy import func
        pos_result = await session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.status == JobStatus.pending, Job.created_at < job.created_at)
        )
        resp["queue_position"] = pos_result.scalar_one() + 1  # 1-indexed

    if job.status == JobStatus.complete:
        resp.update({
            "vertex_count": job.vertex_count,
            "face_count": job.face_count,
            "is_watertight": job.is_watertight,
            "generation_time_s": job.generation_time_s,
            "gpu_metrics": job.gpu_metrics,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "stl_url": f"/api/job/{job.id}/stl",
            "glb_url": f"/api/job/{job.id}/glb" if job.glb_path else None,
        })
    elif job.status == JobStatus.failed:
        resp.update({
            "error": job.error_message,
            "error_step": job.error_step,
        })

    return resp


@router.post("/job/{job_id}/cancel")
async def cancel_job(
    request: Request, job_id: str, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status in (JobStatus.complete, JobStatus.failed, JobStatus.expired):
        raise HTTPException(400, "Job already finished")

    if job.backend and job.backend != "local" and job.backend_job_id:
        from backends import BACKENDS

        backend = BACKENDS.get(job.backend)
        if backend is not None:
            try:
                await backend.cancel(job.backend_job_id)
            except Exception:
                pass  # best-effort — still mark cancelled locally below

    job.status = JobStatus.failed
    job.error_message = "Cancelled by user"
    job.completed_at = datetime.now(timezone.utc)
    await session.commit()

    bridge = request.app.state.worker_bridge
    await bridge._fan_out(job_id, {"type": "failed", "job_id": job_id, "error": "Cancelled by user"})
    await bridge.broadcast_admin({"type": "job_failed", "job_id": job_id, "error": "Cancelled by user"})

    return {"status": "cancelled"}


@router.get("/job/{job_id}/thumbnail")
async def get_thumbnail(job_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.thumbnail_path:
        raise HTTPException(404, "Thumbnail not available")

    path = storage.get_upload_path(job.thumbnail_path)
    if not path.exists():
        raise HTTPException(404, "Thumbnail file missing")

    return FileResponse(path, media_type="image/jpeg")


@router.get("/job/{job_id}/generated-image")
async def get_generated_image(job_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.job_type != "text" or not job.generated_image_path:
        raise HTTPException(404, "Generated image not available")

    path = storage.get_upload_path(job.generated_image_path)
    if not path.exists():
        raise HTTPException(404, "Generated image file missing")

    return FileResponse(path, media_type="image/png")


@router.get("/job/{job_id}/stl")
async def download_stl(job_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.complete or not job.stl_path:
        raise HTTPException(404, "STL not available")

    path = storage.get_output_path(job.stl_path)
    if not path.exists():
        raise HTTPException(404, "STL file missing")

    return FileResponse(
        path,
        media_type="application/sla",
        filename=f"{_safe_download_name(job.original_filename.rsplit('.', 1)[0])}.stl",
    )


@router.get("/job/{job_id}/glb")
async def download_glb(job_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.complete or not job.glb_path:
        raise HTTPException(404, "GLB not available")

    path = storage.get_output_path(job.glb_path)
    if not path.exists():
        raise HTTPException(404, "GLB file missing")

    return FileResponse(
        path,
        media_type="model/gltf-binary",
        filename=f"{_safe_download_name(job.original_filename.rsplit('.', 1)[0])}.glb",
    )


@router.get("/my-jobs")
async def my_jobs(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    offset = (page - 1) * limit

    count_result = await session.execute(
        select(func.count()).select_from(Job).where(Job.user_id == user.id)
    )
    total = count_result.scalar_one()

    result = await session.execute(
        select(Job)
        .where(Job.user_id == user.id)
        .order_by(Job.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    jobs = result.scalars().all()

    return {
        "jobs": [
            {
                "job_id": j.id,
                "status": j.status.value,
                "job_type": j.job_type,
                "original_filename": j.original_filename,
                "prompt": j.prompt if j.job_type == "text" else None,
                "vertex_count": j.vertex_count,
                "face_count": j.face_count,
                "created_at": j.created_at.isoformat(),
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                "thumbnail_url": f"/api/job/{j.id}/thumbnail" if j.thumbnail_path else None,
            }
            for j in jobs
        ],
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit if total > 0 else 1,
    }


@router.get("/queue")
async def queue_status(session: AsyncSession = Depends(get_session)):
    summary = await queue.get_queue_summary(session)
    return {"queue": summary}
