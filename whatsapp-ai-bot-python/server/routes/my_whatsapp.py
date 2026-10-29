from fastapi import APIRouter, Depends, HTTPException

from server.routes.deps import get_current_poc, is_member_role
from server.services import whatsapp_link

router = APIRouter()


def _can_link(user):
    if is_member_role(user):
        raise HTTPException(status_code=403, detail="Members do not link WhatsApp; only the Main Manager and POCs send messages")


@router.get("/my/whatsapp")
async def my_whatsapp(current=Depends(get_current_poc)):
    _can_link(current)
    return await whatsapp_link.status_for(current["id"])


@router.post("/my/whatsapp/connect")
async def connect(current=Depends(get_current_poc)):
    _can_link(current)
    st = await whatsapp_link.status_for(current["id"])
    if st["account"] and st["account"]["status"] == "connected":
        raise HTTPException(status_code=409, detail="Your WhatsApp is already linked. Disconnect it first to link another number.")
    return {"account_id": await whatsapp_link.start_link(current["id"])}


@router.delete("/my/whatsapp")
async def disconnect(current=Depends(get_current_poc)):
    _can_link(current)
    return {"removed": await whatsapp_link.unlink(current["id"])}
