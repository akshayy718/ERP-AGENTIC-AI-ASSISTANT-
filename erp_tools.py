"""
erp_tools.py  (real SAP CAP backend version)
-------------
Same tools, same names, same confirmation-gate behavior as the mock-ERP
version - the only thing that changed is WHAT they talk to: instead of the
local FastAPI mock, every tool now calls the real, deployed SAP CAP service
on Cloud Foundry, using a real OAuth client-credentials token.

WHY THIS MATTERS: nothing in agent_app.py or agent_api.py needed to change.
The tools' names, arguments, and return-value shape (a JSON string, or a
string starting with "ERROR") are identical to before - only the internals
of how each tool fetches/sends data are different. This is the payoff of
having kept that interface stable from the start.

CONFIGURATION (read from environment variables - see .env):
  CAP_API_BASE_URL  e.g. https://<your-app>.cfapps.us10-001.hana.ondemand.com/odata/v4/erp
  CAP_TOKEN_URL     e.g. https://<your-subdomain>.authentication.us10.hana.ondemand.com/oauth/token
  CAP_CLIENT_ID     from `cf service-key <auth-instance> <key-name>`
  CAP_CLIENT_SECRET from the same service key

AUTHENTICATION:
We use the OAuth2 "client credentials" grant - the same flow you tested by
hand with get_token.sh. The token is cached in memory and reused across
calls (tokens last ~12 hours), and automatically refreshed:
  - proactively, a minute before it's due to expire,
  - reactively, if a request ever comes back 401 anyway.

REAL-SERVICE QUIRKS WORTH KNOWING (things that differ from the mock):
  - Field names use the CDS model's exact casing: "ID" (not "id"),
    "vendor_ID" (not "vendor_id"). The mock used lowercase; the real
    service does not. The LLM doesn't care about this - it just reads
    whatever JSON comes back - but it's worth knowing if you read raw output.
  - The real service does NOT auto-generate IDs on create (the mock did).
    create_vendor / create_purchase_order now look up the highest existing
    V/PO number and pick the next one. This is a fine, simple approach for
    a low-traffic demo, but is NOT safe against two creates happening at
    the exact same instant (a real production system would let the
    database generate primary keys instead).
  - The reject action is called "rejectOrder" on the real service, not
    "reject" - "reject" turned out to collide with a reserved method name
    in the CAP framework when we built the service, so we renamed it then.
  - Search and status-filtering now use OData's $filter query syntax
    (e.g. contains(name,'IT')) instead of the mock's custom ?search= param.

ERROR HANDLING (same idea as before, still always returns a string):
Every tool goes through _api_request(), which:
  - transparently attaches a fresh bearer token to every call,
  - catches network/timeout problems,
  - catches HTTP errors (401, 404, etc.) and turns them into a clear
    "ERROR: ..." message the agent can read and explain to the user.

THE CONFIRMATION GATE IS UNCHANGED:
Write actions (approve/reject/update) still only STAGE a proposal in
PENDING_WRITES; the real call happens later, from plain Python code in
agent_app.py, only after the user's next message is a literal "yes".
"""

import os
import time
import json
import re
import urllib.parse
import requests
from dotenv import load_dotenv
from langchain_core.tools import tool

# Load .env ourselves, right here - don't rely on whatever file imports this
# module to have already called load_dotenv() first. (This bit us once:
# agent_app.py used to import erp_tools BEFORE calling load_dotenv(), so the
# CAP_* variables below were still empty at the moment this file read them.)
load_dotenv()

# --- Configuration: where the REAL deployed CAP service lives -----------
CAP_API_BASE_URL = os.getenv("CAP_API_BASE_URL", "").rstrip("/")
CAP_TOKEN_URL = os.getenv("CAP_TOKEN_URL", "")
CAP_CLIENT_ID = os.getenv("CAP_CLIENT_ID", "")
CAP_CLIENT_SECRET = os.getenv("CAP_CLIENT_SECRET", "")

# ===========================================================================
# FALLBACK MODE
# ---------------------------------------------------------------------------
# The real SAP service is on a free trial - it can be asleep, mid-restart,
# or just temporarily unreachable. Rather than show a broken demo to anyone
# visiting at the wrong moment, every tool below automatically falls back to
# a small in-memory mock dataset that mirrors the real service's exact data
# shapes (same field casing, same OData response wrapping).
#
# IMPORTANT - this is NOT silent. agent_api.py exposes is_using_fallback()
# to the dashboard, which shows a clear banner whenever fallback data is in
# use. The whole point of this project is "this is a REAL SAP backend, not
# a mock" - silently substituting mock data without saying so would quietly
# undermine that. Anyone testing this should always know which one they're
# looking at.
#
# A simple "circuit breaker" avoids retrying the real (possibly very slow
# or down) service on every single tool call once it's known to be down:
# once a real network-level failure happens, fallback mode stays on for a
# cooldown window, then automatically tries the real service again.
# ===========================================================================

_FALLBACK_COOLDOWN_SECONDS = 60

_circuit = {"open_until": 0.0, "reason": ""}


def _open_circuit(reason: str) -> None:
    _circuit["open_until"] = time.time() + _FALLBACK_COOLDOWN_SECONDS
    _circuit["reason"] = reason


def is_using_fallback() -> bool:
    """True if we're currently serving mock data because the real SAP
    service was unreachable recently. Used by agent_api.py to show a
    banner on the dashboard - this state is never hidden from the user."""
    return time.time() < _circuit["open_until"]


def fallback_reason() -> str:
    """Why we're in fallback mode right now (empty string if we're not)."""
    return _circuit["reason"] if is_using_fallback() else ""


class _MockNotFound(Exception):
    """Raised inside the mock backend to mean 'this would be a 404 on the
    real service too' - caught by _dispatch_mock and formatted the same way
    a real 404 would be."""
    pass


# Mock data mirrors the CAP service's original seed data exactly - same
# field names, same casing, same starting values - so a demo running on
# fallback data looks identical in shape to one running on the real thing.
_MOCK_VENDORS = [
    {"ID": "V001", "name": "ACME Trading LLC", "category": "Supplies", "status": "active"},
    {"ID": "V002", "name": "Gulf Logistics", "category": "Transport", "status": "active"},
    {"ID": "V003", "name": "Emirates Tech", "category": "IT", "status": "active"},
]
_MOCK_PURCHASE_ORDERS = [
    {"ID": "PO1001", "vendor_ID": "V001", "amount": 4500.0, "status": "pending", "description": "Office supplies Q2"},
    {"ID": "PO1002", "vendor_ID": "V002", "amount": 12000.0, "status": "pending", "description": "Fleet maintenance"},
    {"ID": "PO1003", "vendor_ID": "V003", "amount": 3000.0, "status": "approved", "description": "Laptops"},
    {"ID": "PO1004", "vendor_ID": "V001", "amount": 800.0, "status": "pending", "description": "Stationery"},
]


def _mock_get_vendors(filter_expr: str | None) -> str:
    results = _MOCK_VENDORS
    if filter_expr and "'" in filter_expr:
        needle = filter_expr.split("'")[1].lower()
        results = [v for v in _MOCK_VENDORS if needle in v["name"].lower() or needle in v["category"].lower()]
    return json.dumps({"@odata.context": "$metadata#Vendors", "value": results})


def _mock_create_vendor(body: dict) -> str:
    nums = [int(v["ID"][1:]) for v in _MOCK_VENDORS if v["ID"][1:].isdigit()]
    new_id = f"V{(max(nums) + 1):03d}" if nums else "V001"
    new_vendor = {"ID": new_id, "name": body["name"], "category": body.get("category", "General"), "status": "active"}
    _MOCK_VENDORS.append(new_vendor)
    return json.dumps(new_vendor)


def _mock_get_purchase_orders(filter_expr: str | None) -> str:
    results = _MOCK_PURCHASE_ORDERS
    if filter_expr and "status eq" in filter_expr:
        wanted = filter_expr.split("'")[1]
        results = [p for p in _MOCK_PURCHASE_ORDERS if p["status"] == wanted]
    return json.dumps({"@odata.context": "$metadata#PurchaseOrders", "value": results})


def _mock_create_po(body: dict) -> str:
    vendor_id = body.get("vendor_ID")
    if not any(v["ID"] == vendor_id for v in _MOCK_VENDORS):
        raise _MockNotFound(f"Cannot create purchase order: vendor {vendor_id} does not exist")
    nums = [int(p["ID"][2:]) for p in _MOCK_PURCHASE_ORDERS if p["ID"][2:].isdigit()]
    new_id = f"PO{(max(nums) + 1)}" if nums else "PO1001"
    new_po = {"ID": new_id, "vendor_ID": vendor_id, "amount": body["amount"],
              "status": "pending", "description": body.get("description", "")}
    _MOCK_PURCHASE_ORDERS.append(new_po)
    return json.dumps(new_po)


def _mock_find_po(po_id: str) -> dict:
    po = next((p for p in _MOCK_PURCHASE_ORDERS if p["ID"] == po_id), None)
    if not po:
        raise _MockNotFound(f"PurchaseOrder {po_id} not found")
    return po


def _mock_approve(po_id: str) -> str:
    po = _mock_find_po(po_id)
    po["status"] = "approved"
    return json.dumps(po)


def _mock_reject(po_id: str) -> str:
    po = _mock_find_po(po_id)
    po["status"] = "rejected"
    return json.dumps(po)


def _mock_update_amount(po_id: str, new_amount) -> str:
    po = _mock_find_po(po_id)
    po["amount"] = new_amount
    return json.dumps(po)


def _mock_spend_summary() -> str:
    summary = {"pending": {"count": 0, "totalAmount": 0}, "approved": {"count": 0, "totalAmount": 0},
               "rejected": {"count": 0, "totalAmount": 0}}
    for po in _MOCK_PURCHASE_ORDERS:
        bucket = summary.get(po["status"])
        if bucket:
            bucket["count"] += 1
            bucket["totalAmount"] += po["amount"]
    return json.dumps({"@odata.context": "$metadata#ERPService.return_ERPService_getSpendSummary", **summary})


_PO_ACTION_RE = re.compile(r"^/PurchaseOrders\(ID='([^']+)'\)/ERPService\.(approve|rejectOrder)$")
_PO_PATCH_RE = re.compile(r"^/PurchaseOrders\(ID='([^']+)'\)$")


def _dispatch_mock(method: str, path: str, json_body) -> str:
    """Route a request to the matching mock handler, mirroring the real
    service's URL structure closely enough that callers can't tell the
    difference except by checking is_using_fallback()."""
    try:
        base_path, _, query = path.partition("?")
        filter_expr = None
        if query.startswith("$filter="):
            filter_expr = urllib.parse.unquote(query[len("$filter="):])

        if base_path == "/Vendors" and method == "GET":
            return _mock_get_vendors(filter_expr)
        if base_path == "/Vendors" and method == "POST":
            return _mock_create_vendor(json_body)
        if base_path == "/PurchaseOrders" and method == "GET":
            return _mock_get_purchase_orders(filter_expr)
        if base_path == "/PurchaseOrders" and method == "POST":
            return _mock_create_po(json_body)
        if base_path == "/getSpendSummary()" and method == "GET":
            return _mock_spend_summary()

        m = _PO_ACTION_RE.match(base_path)
        if m and method == "POST":
            po_id, action = m.group(1), m.group(2)
            return _mock_approve(po_id) if action == "approve" else _mock_reject(po_id)

        m = _PO_PATCH_RE.match(base_path)
        if m and method == "PATCH":
            return _mock_update_amount(m.group(1), json_body.get("amount") if json_body else None)

        return f"ERROR: (demo mode) no mock handler for {method} {path}"
    except _MockNotFound as e:
        return f"ERROR (404): {e}"

# Write actions the agent has PROPOSED but not yet executed.
# Cleared and re-filled by agent_app.py's chat_fn on every turn.
PENDING_WRITES: list[dict] = []


def _stage_write(tool: str, args: dict) -> None:
    """Add a write proposal to PENDING_WRITES - but skip it if an identical
    one (same tool + same arguments) is already staged. The agent sometimes
    calls the same tool twice within a single turn (e.g. it isn't sure its
    first call registered), and without this check that would show the same
    proposal twice and double up the confirmation summary."""
    for w in PENDING_WRITES:
        if w["tool"] == tool and w["args"] == args:
            return  # already staged - don't add a duplicate
    PENDING_WRITES.append({"tool": tool, "args": args})


# --- OAuth token handling -------------------------------------------------
_token_cache = {"access_token": None, "expires_at": 0.0}


def _get_token() -> str:
    """Return a cached access token, fetching a fresh one if we don't have
    one yet or it's about to expire. Raises on failure - callers decide how
    to turn that into a user-facing message."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    resp = requests.post(
        CAP_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": CAP_CLIENT_ID,
            "client_secret": CAP_CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def _api_request(method: str, path: str, **kwargs) -> str:
    """
    Make an authenticated request to the real CAP service and ALWAYS return
    a string the agent can read. 'path' is appended to CAP_API_BASE_URL, e.g.
    _api_request("GET", "/Vendors").
    On any problem, return a message starting with 'ERROR' so the agent
    knows the action did not succeed.

    FALLBACK: if the real service is genuinely unreachable (connection
    error, timeout, or a 502/503/504 gateway-level failure - the kind of
    thing a sleeping or restarting Cloud Foundry app produces), this
    transparently serves mock data instead of failing outright. A real
    application-level error (401, 404, a validation 400) is NOT a fallback
    trigger - the service is clearly up and answering, so that error is
    real and gets shown as-is.
    """
    json_body = kwargs.get("json")

    if is_using_fallback():
        return _dispatch_mock(method, path, json_body)

    try:
        token = _get_token()
    except requests.exceptions.RequestException as e:
        # ANY failure to even talk to the auth service - connection refused,
        # DNS failure, timeout, SSL error, whatever - means it's unreachable,
        # so this should always fall back, not just the two narrow subtypes
        # we originally checked for.
        _open_circuit(f"Cannot reach the SAP authentication service ({e.__class__.__name__})")
        return _dispatch_mock(method, path, json_body)
    except (KeyError, ValueError) as e:
        return f"ERROR: Authentication response from the ERP system was unexpected ({e})."

    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    url = f"{CAP_API_BASE_URL}{path}"

    try:
        resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
    except requests.exceptions.RequestException as e:
        _open_circuit(f"Cannot reach the SAP service ({e.__class__.__name__})")
        return _dispatch_mock(method, path, json_body)

    if resp.status_code == 401:
        # The token may have just been revoked/expired early - clear the
        # cache so the *next* call fetches a fresh one instead of reusing
        # the same bad token forever. This is a real auth error, not an
        # outage, so it does NOT trigger fallback.
        _token_cache["access_token"] = None
        return "ERROR (401): Authentication with the ERP system failed or expired."

    if resp.status_code in (502, 503, 504):
        # Gateway-level failures - the service itself (not just our request)
        # is the problem, typically a sleeping/restarting free-tier app.
        _open_circuit(f"SAP service returned HTTP {resp.status_code} (likely asleep or restarting)")
        return _dispatch_mock(method, path, json_body)

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        return f"ERROR ({resp.status_code}): {detail}"

    return resp.text


def _raw_get(path: str) -> dict:
    """Like _api_request, but returns parsed JSON and RAISES on failure.
    Only used internally (e.g. to figure out the next free vendor/PO id) -
    never exposed directly to the agent. Also respects fallback mode."""
    if is_using_fallback():
        return json.loads(_dispatch_mock("GET", path, None))

    try:
        token = _get_token()
        resp = requests.get(f"{CAP_API_BASE_URL}{path}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        if isinstance(e, requests.exceptions.HTTPError):
            raise  # a real 4xx/5xx from a reachable service - let it propagate as a real error
        _open_circuit(f"Cannot reach the SAP service ({e.__class__.__name__})")
        return json.loads(_dispatch_mock("GET", path, None))


def _next_vendor_id() -> str:
    """The real service does not auto-generate ids, so pick the next free
    'V0xx' by looking at what already exists. Simple and fine for a
    low-traffic demo; not safe against two creates at the exact same instant."""
    data = _raw_get("/Vendors")
    nums = [int(v["ID"][1:]) for v in data.get("value", []) if v["ID"][1:].isdigit()]
    return f"V{(max(nums) + 1):03d}" if nums else "V001"


def _next_po_id() -> str:
    """Same idea as _next_vendor_id, but for 'PO1xxx' purchase order ids."""
    data = _raw_get("/PurchaseOrders")
    nums = [int(p["ID"][2:]) for p in data.get("value", []) if p["ID"][2:].isdigit()]
    return f"PO{(max(nums) + 1)}" if nums else "PO1001"


def _get_filtered(path: str, filter_expr: str) -> str:
    """Like _api_request("GET", path), but with an OData $filter applied.

    IMPORTANT: we build this URL by hand rather than using requests' params=
    dict. requests' params= encodes spaces as '+' (standard form-encoding),
    but the real CAP/OData server's strict parser rejects '+' - it wants
    '%20' instead. Using params= here causes a real 400 error against the
    live service ("Expected '&', a whitespace, or end of input but '+'
    found"), even though it looked completely fine in earlier, simpler tests."""
    encoded_filter = urllib.parse.quote(filter_expr, safe="(),'")
    return _api_request("GET", f"{path}?$filter={encoded_filter}")


@tool
def list_vendors(note: str = "") -> str:
    """List all vendors in the ERP system, including their id, name, category and status.
    Use this when the user asks about vendors, or when you need to find a vendor's id
    before creating a purchase order for them.
    The 'note' parameter is optional and not used - leave it blank."""
    return _api_request("GET", "/Vendors")


@tool
def create_vendor(name: str, category: str = "General") -> str:
    """Create a new vendor. Provide the vendor 'name' and optionally a 'category'
    (e.g. Supplies, Transport, IT). Returns the new vendor including its generated id.
    Use this when the user wants to add or register a new vendor."""
    try:
        new_id = _next_vendor_id()
    except requests.exceptions.RequestException as e:
        return f"ERROR: Could not determine a new vendor id ({e})."
    return _api_request("POST", "/Vendors", json={"ID": new_id, "name": name, "category": category})


@tool
def list_purchase_orders(status: str = "") -> str:
    """List purchase orders. Optionally filter by status, which can be
    'pending', 'approved', or 'rejected'. Leave status empty to get all of them.
    Use this when the user asks about purchase orders, POs, or pending approvals."""
    if status:
        return _get_filtered("/PurchaseOrders", f"status eq '{status}'")
    return _api_request("GET", "/PurchaseOrders")


@tool
def create_purchase_order(vendor_id: str, amount: float, description: str = "") -> str:
    """Create a new purchase order for an existing vendor.
    You MUST pass a real vendor_id (like 'V001'). If you only know the vendor's name,
    call list_vendors first to find the id. 'amount' is a number.
    Returns the new purchase order, which starts with status 'pending'.
    If the result starts with ERROR (e.g. the vendor does not exist), do NOT retry
    with a made-up id; tell the user what went wrong."""
    try:
        new_id = _next_po_id()
    except requests.exceptions.RequestException as e:
        return f"ERROR: Could not determine a new purchase order id ({e})."
    return _api_request(
        "POST", "/PurchaseOrders",
        json={"ID": new_id, "vendor_ID": vendor_id, "amount": amount, "description": description},
    )


@tool
def approve_purchase_order(po_id: str) -> str:
    """Propose approving a purchase order by its id (like 'PO1001').
    IMPORTANT: this does NOT actually approve it yet - it stages the change and
    asks the user to confirm. Tell the user what you are proposing and ask them
    to reply 'yes' to proceed. The real approval only happens after that."""
    _stage_write("approve_purchase_order", {"po_id": po_id})
    return (f"PROPOSED (not yet done): approve {po_id}. "
            f"Ask the user to confirm with 'yes' before this takes effect.")


@tool
def reject_purchase_order(po_id: str) -> str:
    """Propose rejecting a purchase order by its id (like 'PO1001').
    IMPORTANT: this does NOT actually reject it yet - it stages the change and
    asks the user to confirm. Tell the user what you are proposing and ask them
    to reply 'yes' to proceed. The real rejection only happens after that."""
    _stage_write("reject_purchase_order", {"po_id": po_id})
    return (f"PROPOSED (not yet done): reject {po_id}. "
            f"Ask the user to confirm with 'yes' before this takes effect.")


def execute_approve_purchase_order(po_id: str) -> str:
    """The REAL approval call. Only ever invoked by agent_app.py's code-level
    confirmation gate, never directly by the LLM."""
    return _api_request("POST", f"/PurchaseOrders(ID='{po_id}')/ERPService.approve")


def execute_reject_purchase_order(po_id: str) -> str:
    """The REAL rejection call. Only ever invoked by agent_app.py's code-level
    confirmation gate, never directly by the LLM.
    NOTE: the action is called 'rejectOrder' on the real service, not 'reject' -
    'reject' collides with a reserved method name in the CAP framework, so we
    renamed it when we built the service."""
    return _api_request("POST", f"/PurchaseOrders(ID='{po_id}')/ERPService.rejectOrder")


@tool
def search_vendors(query: str) -> str:
    """Search for vendors whose name or category contains the given text.
    Use this when the user looks for a vendor by a partial name or by category
    (e.g. 'find IT vendors', 'is there a vendor called Gulf')."""
    filter_expr = f"contains(name,'{query}') or contains(category,'{query}')"
    return _get_filtered("/Vendors", filter_expr)


@tool
def update_purchase_order_amount(po_id: str, new_amount: float) -> str:
    """Propose changing the amount of a pending purchase order to new_amount.
    IMPORTANT: this does NOT actually change it yet - it stages the change and
    asks the user to confirm. Tell the user what you are proposing (the po_id
    and new_amount) and ask them to reply 'yes' to proceed. The real change
    only happens after that. Only pending purchase orders can be edited."""
    _stage_write("update_purchase_order_amount", {"po_id": po_id, "new_amount": new_amount})
    return (f"PROPOSED (not yet done): change {po_id} to ${new_amount}. "
            f"Ask the user to confirm with 'yes' before this takes effect.")


def execute_update_purchase_order_amount(po_id: str, new_amount: float) -> str:
    """The REAL update call. Only ever invoked by agent_app.py's code-level
    confirmation gate, never directly by the LLM."""
    return _api_request(
        "PATCH", f"/PurchaseOrders(ID='{po_id}')",
        json={"amount": new_amount},
    )


@tool
def get_spend_summary(note: str = "") -> str:
    """Get a summary of purchase-order spend: the count and total amount of orders
    in each status (pending, approved, rejected). Use this when the user asks for
    totals, a summary, an overview, or 'how much' is pending/approved.
    The 'note' parameter is optional and not used - leave it blank."""
    return _api_request("GET", "/getSpendSummary()")


ALL_TOOLS = [
    list_vendors,
    create_vendor,
    list_purchase_orders,
    create_purchase_order,
    approve_purchase_order,
    reject_purchase_order,
    search_vendors,
    update_purchase_order_amount,
    get_spend_summary,
]

# Maps a staged write's "tool" name to the function that actually performs it.
# Used by agent_app.py's code-level confirmation gate - never used by the LLM.
EXECUTORS = {
    "approve_purchase_order": execute_approve_purchase_order,
    "reject_purchase_order": execute_reject_purchase_order,
    "update_purchase_order_amount": execute_update_purchase_order_amount,
}
