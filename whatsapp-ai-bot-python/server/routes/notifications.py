from fastapi import APIRouter, Depends, HTTPException

from server import group_db
from server.db import query
from server.repositories import crm
from server.routes.deps import get_current_poc

router = APIRouter()


@router.get("/notifications")
async def list_notifications(unread_only: bool = False, limit: int = 50, offset: int = 0, current=Depends(get_current_poc)):
    """The caller's own notifications only (history = unread_only false)."""
    return await crm.list_user_notifications(current["id"], unread_only, min(max(limit, 1), 200), max(offset, 0))


@router.get("/notifications/unread-count")
async def unread_count(current=Depends(get_current_poc)):
    return {"unread": await crm.unread_count(current["id"])}


@router.post("/notifications/read-all")
async def read_all(current=Depends(get_current_poc)):
    await crm.mark_all_read(current["id"])
    return {"ok": True}


@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: int, current=Depends(get_current_poc)):
    rows = await query("SELECT poc_id FROM poc_notifications WHERE id = %s", (notification_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Notification not found")
    if rows[0]["poc_id"] != current["id"]:
        raise HTTPException(status_code=403, detail="Not your notification")
    await group_db.mark_notification_read(notification_id)
    return {"ok": True}
