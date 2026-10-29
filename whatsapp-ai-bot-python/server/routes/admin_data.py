from fastapi import APIRouter, Depends, HTTPException

from server import permissions
from server.repositories import crm
from server.routes.deps import get_current_poc

router = APIRouter()


@router.get("/permissions")
async def my_permissions(current=Depends(get_current_poc)):
    """What the current role may do. The UI hides other buttons; the backend rejects them anyway."""
    return {"role": current["role"], "actions": permissions.for_role(current["role"])}


@router.get("/permissions/matrix")
async def matrix(current=Depends(get_current_poc)):
    permissions.require(current, "audit.view")
    return {a: sorted(r) for a, r in permissions.MATRIX.items()}


@router.get("/deleted/{entity}")
async def deleted(entity: str, current=Depends(get_current_poc)):
    """Soft-deleted records (admin) so they can be restored."""
    permissions.require(current, "audit.view")
    if entity not in crm.SOFT_TABLES:
        raise HTTPException(status_code=404, detail="Unknown entity")
    return await crm.list_deleted(entity)
