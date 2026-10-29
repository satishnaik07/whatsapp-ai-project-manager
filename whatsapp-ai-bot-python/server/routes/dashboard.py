from fastapi import APIRouter, Depends

from server.repositories import crm
from server.routes.deps import get_current_poc, require_manager_role

router = APIRouter()


@router.get("/dashboard/manager")
async def manager_dashboard(current=Depends(get_current_poc)):
    require_manager_role(current)
    return await crm.manager_dashboard(current)


@router.get("/dashboard/department")
async def department_dashboard(current=Depends(get_current_poc)):
    return await crm.department_dashboard(current)
