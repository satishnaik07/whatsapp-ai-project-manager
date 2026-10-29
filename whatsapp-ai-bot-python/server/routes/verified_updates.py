from fastapi import APIRouter, Depends, HTTPException

from server.repositories import crm
from server.routes.deps import get_current_poc, load_project
from server.services import audit, verified_updates as vu

router = APIRouter()


@router.get("/projects/{project_id}/verified-updates")
async def list_project_verified(project_id: int, current=Depends(get_current_poc)):
    project = await load_project(project_id, current)
    return await vu.project_view(current, project)


@router.post("/projects/{project_id}/verified-updates")
async def add_project_verified(project_id: int, payload: dict, current=Depends(get_current_poc)):
    """Main Manager, a department POC or an admin confirms a fact. visibility: internal | client | both."""
    project = await load_project(project_id, current)
    if not await vu.can_add_to_project(current, project_id):
        raise HTTPException(status_code=403, detail="Only the Main Manager, a POC of this project or an admin can add a verified update")
    result = await vu.post(current, payload.get("text"), payload.get("visibility"), group=None, project=project)
    return {**result, **await vu.project_view(current, project)}


@router.delete("/verified-updates/{update_id}")
async def delete_verified(update_id: int, current=Depends(get_current_poc)):
    row = await crm.get_verified_update(update_id)
    if not row:
        raise HTTPException(status_code=404, detail="Verified update not found")
    if not await vu.can_delete(current, row):
        raise HTTPException(status_code=403, detail="Only the author, the project's Main Manager or an admin can remove it")
    await crm.delete_verified_update(update_id)
    await audit.record(current["id"], "verified_update_removed", "project" if row.get("project_id") else "group",
                       row.get("project_id") or row.get("group_id"), f"verified_update={update_id}: {str(row['text'])[:200]}",
                       project_id=row.get("project_id"))
    return {"ok": True}
