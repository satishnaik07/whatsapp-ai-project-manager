"""
AI department classifier. Decides which of a project's configured
departments a client message belongs to, using conversation context, the
department descriptions (configured in the dashboard, never hard-coded) and
the thread's current department.

Returns a dict:
    {"department": "<name or null>", "confidence": 0.0-1.0, "reason": str,
     "needs_manager": bool, "cross_department": bool}
Raises on provider failure. The router decides what to do (fallback).
"""
import json
import os
import re

from openai import AsyncOpenAI

_client = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY") or "missing",
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _client


MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

CLASSIFIER_PROMPT = """You route client messages in a project WhatsApp group to the right internal department.

Departments for this WhatsApp group (name: what they handle). If a name looks like "Project / Department", the group serves several projects and you must pick the right project AND department:
{departments}

Rules:
- Pick exactly ONE department name from the list, or null if the message does not clearly belong to one.
- Understand English, Hindi and Hinglish.
- Use the conversation. If CURRENT DEPARTMENT is set and the new message is a natural continuation of that topic (even without any department keyword), keep it.
- If the client clearly moves to a different topic, switch to the department for the new topic.
- If you cannot tell (e.g. only "payment ke regarding baat karni hai", or a vague greeting), return department null with low confidence.
- Set needs_manager true for pricing/budget/contract/legal talk, anger or complaints, or when the client asks for the manager.
- Set cross_department true if the message clearly needs more than one department, and list the other ones in also_departments (exact names from the list, never invented).
- Set needs_clarification true if you would have to guess what the client means.
- intent: a few words (for example "report login bug"). category: one of bug, change_request, question, status_request, feedback, billing, other.
- urgency: low, normal, high or urgent (urgent only for outages or clear deadlines).
- confidence is your honest 0 to 1 certainty. Do not inflate it. Only use department names from the list, or null.

Reply with ONLY compact JSON:
{{"department": "<name or null>", "confidence": 0.0, "reason": "<short>", "needs_manager": false, "cross_department": false, "also_departments": [], "needs_clarification": false, "intent": "", "category": "other", "urgency": "normal"}}
"""


CATEGORIES = {"bug", "change_request", "question", "status_request", "feedback", "billing", "other"}
URGENCIES = {"low", "normal", "high", "urgent"}


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError("classifier returned no JSON")
    data = json.loads(m.group(0))
    dept = data.get("department")
    if isinstance(dept, str) and dept.strip().lower() in ("", "null", "none", "unknown"):
        dept = None
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    urgency = str(data.get("urgency", "normal")).lower()
    category = str(data.get("category", "other")).lower()
    also = data.get("also_departments") or []
    return {
        "department": dept,
        "confidence": max(0.0, min(1.0, conf)),
        "reason": str(data.get("reason", ""))[:300],
        "needs_manager": bool(data.get("needs_manager", False)),
        "cross_department": bool(data.get("cross_department", False)),
        "also_departments": [str(x) for x in also if isinstance(x, str)][:4] if isinstance(also, list) else [],
        "needs_clarification": bool(data.get("needs_clarification", False)),
        "intent": str(data.get("intent", ""))[:120],
        "category": category if category in CATEGORIES else "other",
        "urgency": urgency if urgency in URGENCIES else "normal",
    }


async def classify(message: str, departments: list[dict], history_text: str,
                   current_department: str | None = None, routing_status: str | None = None) -> dict:
    dept_lines = "\n".join(f"- {d['name']}: {d.get('description') or '(no description)'}" for d in departments)
    system = CLASSIFIER_PROMPT.format(departments=dept_lines)
    user = (
        f"CURRENT DEPARTMENT: {current_department or 'none'}\n"
        f"THREAD ROUTING STATUS: {routing_status or 'new'}\n\n"
        f"RECENT CONVERSATION:\n{history_text or '(none)'}\n\n"
        f"NEW CLIENT MESSAGE: {message}"
    )
    resp = await _get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        max_tokens=220,
    )
    return _parse(resp.choices[0].message.content or "")
