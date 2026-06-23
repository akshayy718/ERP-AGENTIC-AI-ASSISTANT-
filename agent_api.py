"""
agent_api.py
------------
This server does two jobs:
  1. Exposes the agent (from agent_app.py) as a small web API, so a normal
     HTML/JS frontend can talk to it (browsers can't run our Python agent
     directly - they can only call HTTP endpoints).
  2. Serves the dashboard webpage itself (the files in static/).

Run it with:
    py -3.12 -m uvicorn agent_api:app --reload --port 8001
Then open: http://127.0.0.1:8001

RATE LIMITING (only relevant once this is deployed publicly):
The Groq API key behind this agent has a small SHARED daily token quota -
shared across every single visitor, not per-person. Without any limit, one
visitor (or just heavy testing) can exhaust the whole day's quota for
everyone, including the rest of the day for the developer themselves.

So /api/chat - the only endpoint that actually calls the LLM - is protected
by two simple counters:
  - PER_IP_DAILY_LIMIT: how many messages any single visitor gets per day.
    This exists so one visitor can't accidentally (or deliberately) use up
    the entire shared quota, leaving nothing for anyone else.
  - GLOBAL_DAILY_LIMIT: a hard cap on total messages across ALL visitors
    combined per day. This is the actual backstop tied to the real shared
    resource (the Groq quota itself).
/api/state is NOT limited - it only reads from the ERP service directly and
never calls the LLM, so it costs nothing from the Groq quota.

NOTE on the counters: they live in memory and reset automatically each day
(the dictionary keys include today's date). They are NOT persisted across a
server restart - on a free hosting tier that occasionally restarts/sleeps,
this is an ACCEPTABLE failure mode: a restart can only ever LOOSEN the
limit early, it can never unfairly block someone who hasn't actually used
their share yet.
"""

import os
import json
from datetime import datetime, timezone
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_app import run_agent_turn
import erp_tools

app = FastAPI(title="ERP Agent API")

PER_IP_DAILY_LIMIT = int(os.getenv("PER_IP_DAILY_LIMIT", "6"))
GLOBAL_DAILY_LIMIT = int(os.getenv("GLOBAL_DAILY_LIMIT", "20"))

_turn_counts_by_ip: dict[tuple[str, str], int] = defaultdict(int)
_global_turn_counts: dict[str, int] = defaultdict(int)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _client_ip(request: Request) -> str:
    """Get the REAL visitor's IP, not the reverse proxy's. Render (and most
    hosting platforms) sit in front of the app and forward the actual
    visitor's address in the X-Forwarded-For header - request.client.host
    alone would just show the proxy's internal address, making every
    visitor look like the same "person" for rate-limiting purposes."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(client_ip: str) -> str | None:
    """Returns a friendly message if this request should be BLOCKED, or
    None if it's allowed (in which case this also records the turn)."""
    today = _today()

    if _global_turn_counts[today] >= GLOBAL_DAILY_LIMIT:
        return ("This demo has reached its shared daily usage limit (this "
                "keeps the free AI quota available for everyone). Please "
                "check back tomorrow.")

    if _turn_counts_by_ip[(today, client_ip)] >= PER_IP_DAILY_LIMIT:
        return (f"You've reached this demo's limit of {PER_IP_DAILY_LIMIT} "
                f"messages per day (this keeps the shared free AI quota "
                f"available for other visitors too). Please check back "
                f"tomorrow.")

    _global_turn_counts[today] += 1
    _turn_counts_by_ip[(today, client_ip)] += 1
    return None


class ChatRequest(BaseModel):
    message: str
    history: list = []


class ChatResponse(BaseModel):
    reply: str
    steps: list = []
    pending: list = []


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    """The dashboard calls this every time the user sends a message."""
    limit_message = _check_rate_limit(_client_ip(request))
    if limit_message:
        return ChatResponse(reply=limit_message, steps=[], pending=[])
    result = run_agent_turn(req.message, req.history)
    return ChatResponse(**result)


def _safe_json(raw: str):
    """erp_tools' tools return JSON strings on success, or 'ERROR: ...' text
    on failure. Try to parse; fall back to an empty list if it's an error."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


@app.get("/api/state")
def get_state():
    """The dashboard calls this to fill the live vendors/POs/summary panel,
    both on first load and after every chat turn."""
    vendors_raw = erp_tools.list_vendors.invoke({})
    pos_raw = erp_tools.list_purchase_orders.invoke({})
    summary_raw = erp_tools.get_spend_summary.invoke({})

    summary = _safe_json(summary_raw)
    if not isinstance(summary, dict):
        summary = {}

    return {
        "vendors": _safe_json(vendors_raw),
        "purchase_orders": _safe_json(pos_raw),
        "summary": summary,
        "erp_reachable": not vendors_raw.startswith("ERROR"),
    }


# Serve the dashboard's HTML/CSS/JS files. This must be LAST - it mounts
# a catch-all at "/", so any route added after this would never be reached.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
