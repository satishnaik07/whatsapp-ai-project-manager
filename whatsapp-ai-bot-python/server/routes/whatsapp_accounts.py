from fastapi import APIRouter, Depends, HTTPException

from server.repositories import crm
from server.routes.deps import get_current_poc, require_admin, require_manager_role
from server.services import audit

router = APIRouter()


@router.get("/whatsapp-accounts")
async def list_whatsapp_accounts(current=Depends(get_current_poc)):
    require_manager_role(current)
    return await crm.list_accounts_with_users()


@router.put("/whatsapp-accounts/{account_id}/user")
async def link_account_user(account_id: str, payload: dict, current=Depends(get_current_poc)):
    """Map a WhatsApp account to a system user (or unlink with user_id=null)."""
    require_admin(current)
    accounts = {a["id"] for a in await crm.list_accounts_with_users()}
    if account_id not in accounts:
        raise HTTPException(status_code=404, detail="WhatsApp account not found")
    user_id = payload.get("user_id")
    if user_id is None:
        await crm.unmap_account(account_id)
    else:
        if not await crm.get_user(int(user_id)):
            raise HTTPException(status_code=404, detail="User not found")
        await crm.map_account_user(account_id, int(user_id))
    await audit.record(current["id"], "whatsapp_account_linked", "account", account_id, str(user_id))
    return {"ok": True}
