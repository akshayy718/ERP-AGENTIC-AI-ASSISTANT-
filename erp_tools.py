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
    """
    try:
        token = _get_token()
    except requests.exceptions.RequestException as e:
        return f"ERROR: Could not authenticate with the ERP system ({e})."
    except (KeyError, ValueError) as e:
        return f"ERROR: Authentication response from the ERP system was unexpected ({e})."

    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    url = f"{CAP_API_BASE_URL}{path}"

    try:
        resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
    except requests.exceptions.ConnectionError:
        return ("ERROR: Cannot reach the ERP system. "
                "Check CAP_API_BASE_URL and your network connection.")
    except requests.exceptions.Timeout:
        return "ERROR: The ERP system took too long to respond (timeout)."
    except requests.exceptions.RequestException as e:
        return f"ERROR: Could not contact the ERP system ({e})."

    if resp.status_code == 401:
        # The token may have just been revoked/expired early - clear the
        # cache so the *next* call fetches a fresh one instead of reusing
        # the same bad token forever.
        _token_cache["access_token"] = None
        return "ERROR (401): Authentication with the ERP system failed or expired."

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
    never exposed directly to the agent."""
    token = _get_token()
    resp = requests.get(f"{CAP_API_BASE_URL}{path}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


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
