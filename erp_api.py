"""
erp_api.py
-----------
This is a FAKE (mock) ERP system, built with FastAPI.

Think of this as a tiny stand-in for Sage X3 or SAP. It stores vendors and
purchase orders in plain Python lists (an "in-memory database") and exposes
them over a normal REST API, exactly like a real ERP would.

The AI agent will NOT talk to this data directly. It will make HTTP calls
to these endpoints, just like it would to a real enterprise system. That
separation is the whole point of the project: the agent is a natural-language
LAYER on top of an ERP API.

Run it with:
    uvicorn erp_api:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import date

app = FastAPI(title="Mock ERP System")

# ---------------------------------------------------------------------------
# 1. Our fake "database" - just Python lists held in memory.
#    (When you stop the server, changes are lost. That's fine for a demo.)
# ---------------------------------------------------------------------------

vendors = [
    {"id": "V001", "name": "ACME Trading LLC", "category": "Supplies", "status": "active"},
    {"id": "V002", "name": "Gulf Logistics",   "category": "Transport", "status": "active"},
    {"id": "V003", "name": "Emirates Tech",     "category": "IT",        "status": "active"},
]

purchase_orders = [
    {"id": "PO1001", "vendor_id": "V001", "amount": 4500.0,  "status": "pending",  "description": "Office supplies Q2"},
    {"id": "PO1002", "vendor_id": "V002", "amount": 12000.0, "status": "pending",  "description": "Fleet maintenance"},
    {"id": "PO1003", "vendor_id": "V003", "amount": 3000.0,  "status": "approved", "description": "Laptops"},
    {"id": "PO1004", "vendor_id": "V001", "amount": 800.0,   "status": "pending",  "description": "Stationery"},
]

# Counters so new IDs don't clash with the seed data above.
_next_vendor_num = 4
_next_po_num = 1005


# ---------------------------------------------------------------------------
# 2. Request "shapes" for the data people send us when creating things.
#    Pydantic checks the incoming data matches this shape automatically.
# ---------------------------------------------------------------------------

class VendorCreate(BaseModel):
    name: str
    category: str = "General"


class PurchaseOrderCreate(BaseModel):
    vendor_id: str
    amount: float
    description: str = ""


class PurchaseOrderUpdate(BaseModel):
    amount: float


# ---------------------------------------------------------------------------
# 3. The endpoints (the actual API the agent will call).
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"message": "Mock ERP is running."}


# ----- Vendors -----

@app.get("/vendors")
def get_vendors(search: str | None = None):
    """Return every vendor, or only those whose name/category matches `search`."""
    if search:
        s = search.lower()
        return [v for v in vendors
                if s in v["name"].lower() or s in v["category"].lower()]
    return vendors


@app.get("/vendors/{vendor_id}")
def get_vendor(vendor_id: str):
    """Return one vendor by its ID, or 404 if it doesn't exist."""
    for v in vendors:
        if v["id"] == vendor_id:
            return v
    raise HTTPException(status_code=404, detail=f"Vendor {vendor_id} not found")


@app.post("/vendors")
def create_vendor(payload: VendorCreate):
    """Create a new vendor and return it (with its freshly generated ID)."""
    global _next_vendor_num
    new_vendor = {
        "id": f"V{_next_vendor_num:03d}",   # e.g. V004
        "name": payload.name,
        "category": payload.category,
        "status": "active",
    }
    vendors.append(new_vendor)
    _next_vendor_num += 1
    return new_vendor


# ----- Purchase Orders -----

@app.get("/purchase-orders")
def get_purchase_orders(status: str | None = None):
    """
    Return purchase orders.
    Optionally filter by status, e.g. /purchase-orders?status=pending
    """
    if status:
        return [po for po in purchase_orders if po["status"] == status]
    return purchase_orders


@app.post("/purchase-orders")
def create_purchase_order(payload: PurchaseOrderCreate):
    """
    Create a new purchase order for an EXISTING vendor.
    Fails with 404 if the vendor_id doesn't exist - this is on purpose,
    so the agent must look up the vendor first.
    """
    global _next_po_num

    # Guard: the vendor must exist.
    if not any(v["id"] == payload.vendor_id for v in vendors):
        raise HTTPException(
            status_code=404,
            detail=f"Cannot create PO: vendor {payload.vendor_id} does not exist",
        )

    new_po = {
        "id": f"PO{_next_po_num}",
        "vendor_id": payload.vendor_id,
        "amount": payload.amount,
        "status": "pending",
        "description": payload.description,
    }
    purchase_orders.append(new_po)
    _next_po_num += 1
    return new_po


@app.post("/purchase-orders/{po_id}/approve")
def approve_purchase_order(po_id: str):
    """Mark a purchase order as approved."""
    for po in purchase_orders:
        if po["id"] == po_id:
            po["status"] = "approved"
            return po
    raise HTTPException(status_code=404, detail=f"PO {po_id} not found")


@app.post("/purchase-orders/{po_id}/reject")
def reject_purchase_order(po_id: str):
    """Mark a purchase order as rejected."""
    for po in purchase_orders:
        if po["id"] == po_id:
            po["status"] = "rejected"
            return po
    raise HTTPException(status_code=404, detail=f"PO {po_id} not found")


# ----- Tier 3 additions -----

@app.patch("/purchase-orders/{po_id}")
def update_purchase_order_amount(po_id: str, payload: PurchaseOrderUpdate):
    """Change the amount of a purchase order. Only allowed while it is still pending."""
    for po in purchase_orders:
        if po["id"] == po_id:
            if po["status"] != "pending":
                raise HTTPException(
                    status_code=400,
                    detail=f"PO {po_id} is {po['status']}, only pending POs can be edited",
                )
            po["amount"] = payload.amount
            return po
    raise HTTPException(status_code=404, detail=f"PO {po_id} not found")


@app.get("/reports/spend-summary")
def spend_summary():
    """Return totals and counts grouped by purchase-order status."""
    summary = {
        "pending":  {"count": 0, "total_amount": 0.0},
        "approved": {"count": 0, "total_amount": 0.0},
        "rejected": {"count": 0, "total_amount": 0.0},
    }
    for po in purchase_orders:
        bucket = summary.get(po["status"])
        if bucket is not None:
            bucket["count"] += 1
            bucket["total_amount"] += po["amount"]
    return summary
