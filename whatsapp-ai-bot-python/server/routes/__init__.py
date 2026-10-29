"""CRM routers, all mounted under /api (existing /api/pm/* routes are untouched)."""
from fastapi import APIRouter

from server.routes import (admin_data, clients, conversations, dashboard, departments, notifications,
                           my_whatsapp, projects, tasks, updates, users, verified_updates, whatsapp_accounts)

crm_router = APIRouter(prefix="/api")
for _m in (users, departments, whatsapp_accounts, clients, projects, tasks, updates,
           notifications, dashboard, conversations, my_whatsapp, admin_data, verified_updates):
    crm_router.include_router(_m.router)
