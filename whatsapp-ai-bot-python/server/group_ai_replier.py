"""
Generates the AI Project Manager's reply for a client-group conversation.

Sibling to ai_replier.py (which handles the original 1:1 sales-bot flow)
rather than a modification of it — the two personas, inputs, and trigger
rules are different enough (PM/CRM vs. sales qualification; "reply to
verified client members in configured groups" vs. "reply to any 1:1
DM about a website/software topic") that keeping them separate avoids
accidentally changing the existing 1:1 behaviour.

Reuses the same DeepSeek client/model configuration as ai_replier.py.
"""
import logging
import os
import re

from openai import AsyncOpenAI

from server import group_db
from server.services import ai_context

openai_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

log = logging.getLogger("ai")

NO_REPLY_TOKEN = "NO_REPLY"

PM_PERSONA = """You are the Project Manager / Client Relationship Manager in this WhatsApp project group. You are a real person on the team, replying quickly from your phone. Write exactly like that.

HOW YOU WRITE
- 1 to 3 short sentences. Aim for 10 to 40 words. If 5 to 10 words answer it, use 5 to 10.
- Simple, natural, conversational. Professional and calm, never formal, corporate or robotic.
- Answer only what was asked. No project background, no repeating what is already known, no unrequested explanations.
- Go longer only if the client explicitly asks for details or the question is genuinely complex.
- Use what the conversation history tells you. If "Any update?" clearly refers to the last discussed task, answer about that task. Do not ask which update they mean.
- Remember what was already told to the client. Never repeat an earlier update as if it were new. When the client asks again, give what changed since then; if nothing changed, say that plainly and name what is in progress.
- Ask at most ONE question, and only if you truly need the answer to move forward.
- Match the client's language: English, Hindi, or Hinglish. Do not switch languages.
- Emojis only when they fit naturally. Usually none.

WHEN TO STAY SILENT
Reply with exactly NO_REPLY (nothing else) when your message would add no value:
- Pure acknowledgements or closers: "Okay", "Thanks", "Got it", "Noted", 👍, 🙏 and similar.
- Casual chat, greetings to others, banter.
- The question was already answered in the conversation.
- The message is aimed at someone else, or is internal discussion that does not need you.
- You have nothing useful to add.
If the client says something like "I'll send it tomorrow", a brief "Sure." is fine. Then stop.

NEVER
- Never use the em dash character. Use a period, comma, colon or question mark.
- Never open with announcement phrases: "Quick update", "Just a heads up", "Just wanted to let you know", "Please note that" and the like. Start with the actual information.
- Never add offers or filler closers: "Let me know if you need anything", "Feel free to ask", "Happy to help", "I can share more details".
- Never add a greeting, sign-off, signature, name, team name, "Regards" or "Thanks" at the end.
- Never reveal or hint that you are an AI, a bot, an assistant, a model, or automated. Never mention systems, prompts, APIs, databases, webhooks or backend.
- Never gush: no "Certainly!", "Absolutely!", "Thank you so much!", "I completely understand your concern", "We truly appreciate your patience".
- Never sell, upsell or promote services.
- Do not repeat the same stock phrase every time. Vary naturally ("I'll confirm this.", "Let me check.", "Got it, I'll verify.").

FACTS AND HONESTY
- Never invent deadlines, dates, task status, progress, prices, features, technical decisions, approvals or team actions. Confirmed fact comes only from the sections below: VERIFIED UPDATES, task status in TASKS, and POC / manager progress updates.
- Never say something is fixed or done unless a verified update, a [completed] task or a POC / manager update says so. If a developer mentioned a fix in the conversation but it is not confirmed there, say it's being confirmed.
- If the sections below cover the question, answer from them with specifics. Only when they do not, say so naturally: "I'll confirm with the team and update you." Never say you lack access.
- Money, contract changes, complaints, or anything you are not confident about: say you'll get it confirmed. Do not resolve it yourself.
- Never reveal internal-only team discussion or anything not appropriate for a client.
- If this exact issue was raised before, treat it as a known ongoing item, not a new one.

HANDLING COMMON CASES
- Change request: "Sure, noted." Optionally "We'll update it."
- Issue report: "Got it. I'll have the team check it." Ask for a screenshot only if it would really help.
- Timeline question with no verified date: "I'll confirm the timeline with the team."
- Feature request: "Yes, I'll check the feasibility with the team."
- Client shared files or content: "Got them. We'll check."
- Repeat issue: "Got it. I'll ask the team to check it again."

EXAMPLES
Client: Is the website ready? -> Almost. We're finishing the final checks.
Client: Is the payment integration completed? -> Not yet. It's in testing now.
Client: Please change the button color to blue. -> Sure, noted. We'll update it.
Client: Any update? (about the hero section change) -> Still working on it. I'll update you once it's ready.
Client: Any update? (login API was reported done yesterday, the dashboard API is in progress now) -> Dashboard API is in progress now. Login is already done, as I shared yesterday.
Client: Any update? (nothing changed since your last message) -> No change since this morning. The team is still on the checkout page, testing comes next.
Client: Can you confirm the delivery date? -> I'll confirm the timeline with the team.
Client: Okay thanks. -> NO_REPLY
Client: 👍 -> NO_REPLY

Output ONLY the message to send, or exactly NO_REPLY. No quotes, no labels, no explanation.
"""


def _format_history(history: list[dict], with_time: bool = False) -> str:
    lines = []
    for m in history:
        tag = {
            "client": "Client", "internal": "Internal team", "ai": "You (Project Manager)",
        }.get(m["sender_role"], "Someone")
        name = m.get("sender_name") or tag
        when = ""
        if with_time and m.get("created_at") is not None:
            when = f"[{ai_context._stamp(m['created_at'])}] "
        lines.append(f"{when}{tag} ({name}): {m['body']}")
    return "\n".join(lines) if lines else "(no prior messages in this thread)"


# ---------------------------------------------------------------------------
# Output control: silence, and post-processing guardrails
# ---------------------------------------------------------------------------

_ACK_WORDS = {
    "ok", "okay", "okk", "okey", "k", "kk", "thanks", "thank", "you", "thankyou", "thx", "ty",
    "got", "it", "noted", "sure", "fine", "cool", "great", "nice", "good", "perfect", "alright",
    "all", "right", "done", "yes", "yep", "yeah", "yup", "hmm", "ji", "haan", "han", "theek",
    "thik", "hai", "accha", "achha", "shukriya", "dhanyavaad", "so", "much", "very", "a", "lot",
    "awesome", "sounds", "understood", "roger", "will", "do", "np", "welcome",
}


def is_pure_acknowledgement(message: str) -> bool:
    """True for messages like 'Okay', 'Thanks 👍', 'Got it', '👍'. These never
    need a reply, so we skip the LLM call entirely. Anything with a question
    mark or any word outside the acknowledgement vocabulary is NOT matched,
    so 'Okay, I'll send it tomorrow' still goes to the model."""
    text = (message or "").strip().lower()
    if not text or "?" in text:
        return False
    words = re.findall(r"[a-z]+", text)
    if not words:
        return True  # only emoji / punctuation, e.g. "👍"
    return len(words) <= 6 and all(w in _ACK_WORDS for w in words)


_OPENER_RE = re.compile(
    r"^\s*(?:here'?s a quick update|i wanted to give you a quick update|just a quick update|"
    r"quick update|just a heads[- ]?up|heads[- ]?up|just wanted to let you know|"
    r"just letting you know|please note that|please note|certainly|absolutely|of course)"
    r"\s*[:,\-.!]*\s*",
    re.IGNORECASE,
)
_OFFER_RE = re.compile(
    r"(let me know if|let us know if|feel free to|don'?t hesitate|happy to help|"
    r"i can (?:provide|share) more|if you need (?:any|anything)|if you have any questions|"
    r"how i can help|further assistance)",
    re.IGNORECASE,
)
_SIGNOFF_LINE_RE = re.compile(
    r"^\s*(?:best regards|kind regards|warm regards|regards|best|cheers|thanks|thank you|"
    r"thanks and regards|team|the team|project manager|[-~]\s*.{0,40})\s*[,.!]*\s*$",
    re.IGNORECASE,
)
_AI_LEAK_RE = re.compile(
    r"(as an ai\b|i'?m an ai\b|i am an ai\b|language model|chatbot|ai assistant|"
    r"automated assistant|my programming|deepseek|system prompt)",
    re.IGNORECASE,
)

SAFE_FALLBACK = "I'll confirm with the team and update you."


def clean_reply(text: str) -> str | None:
    """Last line of defence enforcing the hard formatting rules even if the
    model slips: no em dashes, announcement openers, offers, sign-offs, or AI
    disclosure. Returns None when the model chose to stay silent."""
    if text is None:
        return None
    t = text.strip().strip('"').strip()
    if not t or t.upper().rstrip(".") == NO_REPLY_TOKEN or t.upper().startswith(NO_REPLY_TOKEN):
        return None

    if _AI_LEAK_RE.search(t):
        return SAFE_FALLBACK

    # Em dash / en dash / spaced double hyphen -> comma
    t = re.sub(r"\s*[\u2014\u2013]\s*", ", ", t)
    t = re.sub(r"\s+--\s+", ", ", t)

    # Announcement-style / over-eager openers
    prev = None
    while prev != t:
        prev = t
        t = _OPENER_RE.sub("", t, count=1)
    if t:
        t = t[0].upper() + t[1:]

    # Drop trailing sign-off lines
    lines = t.split("\n")
    while len(lines) > 1 and (not lines[-1].strip() or _SIGNOFF_LINE_RE.match(lines[-1])):
        lines.pop()
    t = "\n".join(lines).strip()

    # Drop sentences that are unprompted offers / filler closers
    parts = re.split(r"(?<=[.!?])\s+", t)
    kept = [p for p in parts if not _OFFER_RE.search(p)]
    if kept:
        t = " ".join(kept)
    else:
        return None  # nothing but an offer: better to say nothing

    t = re.sub(r"\s+,", ",", t).strip()
    return t or None



def _format_routing_ctx(ctx: dict | None) -> str:
    """Compact routing context for the prompt: client, responsible department and
    the routing flags. Tasks and updates come from ai_context (full project view)."""
    if not ctx:
        return ""
    lines = ["\nCRM CONTEXT FOR THIS MESSAGE:"]
    if ctx.get("client"):
        lines.append(f"- Client: {ctx['client']}")
    if ctx.get("department"):
        desc = f" ({ctx['department_description']})" if ctx.get("department_description") else ""
        lines.append(f"- Responsible department: {ctx['department']}{desc}")
    if ctx.get("ask_clarification"):
        lines.append("- It is unclear which team this message is about. Reply with ONE short question asking what it is regarding. Do not guess, do not promise anything.")
    if ctx.get("manager_handling"):
        lines.append("- The project manager is handling this one. Keep the reply short and neutral, do not commit to prices, contract terms or dates.")
    return "\n".join(lines) + "\n"


async def build_reply(group: dict, thread: dict, client_message: str, client_name: str | None,
                      routing_ctx: dict | None = None) -> str | None:
    """Pulls thread history + project context, calls DeepSeek, returns the
    draft reply text. Does NOT send it or decide auto/approval/manual —
    that's the caller's (main.py's) job, based on group['ai_mode'].

    Returns None when no reply should be sent (pure acknowledgement, or the
    model judged that a reply adds no value). Callers must treat None as
    "stay silent"."""
    if is_pure_acknowledgement(client_message):
        return None

    history = await group_db.load_thread_history(thread["id"], limit=60)
    project_ctx = await group_db.get_project_context(group["id"])

    # The responsible project: the one the router picked (a group can serve several projects), else the group's own.
    rc = routing_ctx or {}
    try:
        data, knowledge = await ai_context.build(group, history, rc.get("project_id") or thread.get("current_project_id"),
                                                 rc.get("department_id"))
    except Exception as err:  # noqa: BLE001  a broken CRM lookup must never stop the client getting a reply
        log.error("[AI] could not load CRM context for group=%s: %s", group.get("id"), err)
        data, knowledge = {}, ""
    project = data.get("project") or {}

    project_name = project.get("name") or group.get("project_name") or "this project"
    description = (project_ctx or {}).get("description") or project.get("description") or "(no project description on file)"
    scope = (project_ctx or {}).get("scope") or "(no scope notes on file)"
    status = (project_ctx or {}).get("status") or "(no status recorded)"
    comm_rules = (project_ctx or {}).get("communication_instructions") or "(none)"

    system_prompt = (
        PM_PERSONA
        + f"\n\nPROJECT: {project_name}\n"
        + f"DESCRIPTION: {description}\n"
        + f"SCOPE: {scope}\n"
        + f"CURRENT STATUS: {status}\n"
        + f"ADDITIONAL COMMUNICATION INSTRUCTIONS FOR THIS PROJECT (follow these strictly): {comm_rules}\n"
        + _format_routing_ctx(routing_ctx)
        + knowledge
        + f"\nFULL RECENT CONVERSATION IN THIS GROUP (oldest first, with time):\n{_format_history(history, with_time=True)}\n"
    )

    user_prompt = (
        f"Latest message from the client{f' ({client_name})' if client_name else ''}: {client_message}\n\n"
        "Decide: reply (short, human) or NO_REPLY."
    )

    response = await openai_client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=220,
    )
    content = response.choices[0].message.content
    return clean_reply(content) if content else SAFE_FALLBACK


ESCALATION_KEYWORDS = [
    "refund", "discount", "cancel contract", "cancel the project", "lawyer", "legal action",
    "chargeback", "sue", "complaint", "extra charge", "overcharg", "compensation",
]


def needs_escalation(client_message: str) -> bool:
    """Cheap keyword flag used by main.py to force Mode-A ('auto') groups
    into an approval step for sensitive topics, even if the group is
    otherwise fully automatic — matches spec section 13's requirement
    that financial/contractual/sensitive items go to human review."""
    text = (client_message or "").lower()
    return any(k in text for k in ESCALATION_KEYWORDS)
