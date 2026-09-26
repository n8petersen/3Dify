"""Admin WebSocket routes — mounted at root (no /api prefix) so nginx
proxies them through the /ws block which has Upgrade headers."""

import asyncio
import hmac

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from config import settings
from database import engine
from models.job import Job
from services.worker_bridge import WorkerBridge, _serialize_job_for_admin

router = APIRouter()


@router.websocket("/ws/admin/activity")
async def admin_activity_ws(ws: WebSocket, token: str = Query(...)):
    """Live feed of all job lifecycle events for the admin Activity view.

    Auth via ?token= query param (browsers can't send Authorization on WS handshake).
    On connect: sends a 'snapshot' with the most recent N jobs, then streams events.
    """
    if not hmac.compare_digest(token, settings.admin_auth_token):
        await ws.close(code=4401, reason="Invalid admin token")
        return

    await ws.accept()
    bridge: WorkerBridge = ws.app.state.worker_bridge

    # Initial snapshot — most recent 30 jobs across all statuses
    try:
        async with SQLModelAsyncSession(engine, expire_on_commit=False) as session:
            result = await session.execute(
                select(Job).order_by(Job.created_at.desc()).limit(30)
            )
            jobs = result.scalars().all()
        is_local = settings.worker_backend == "local"
        await ws.send_json({
            "type": "snapshot",
            "jobs": [_serialize_job_for_admin(j) for j in jobs],
            "worker_connected": bridge.worker_connected if is_local else bridge.backend_available,
            "backend": settings.worker_backend,
        })
    except Exception:
        pass

    bridge.subscribe_admin(ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Heartbeat through proxies
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        bridge.unsubscribe_admin(ws)
