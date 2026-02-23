from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from models.job import Job, JobStatus

router = APIRouter(prefix="/api")


@router.get("/gallery")
async def gallery(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Job)
        .where(Job.status == JobStatus.complete, Job.feedback_rating >= 4)
        .order_by(Job.completed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    jobs = result.scalars().all()
    return [
        {
            "job_id": j.id,
            "thumbnail_url": f"/api/job/{j.id}/thumbnail" if j.thumbnail_path else None,
            "vertex_count": j.vertex_count,
            "generation_time_s": j.generation_time_s,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
        }
        for j in jobs
    ]
