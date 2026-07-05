"""
Core billing logic for the CSP Billing Azure Function.
Adapted from csp_billing.py: fetches billed/unbilled reconciliation data from
Microsoft Graph partner billing API, applies per-customer markup, and writes
dashboard.html / billing_data.json / customer_charges.csv to blob storage.

Exports are asynchronous on Microsoft's side (1-15 min), so this module uses a
resumable design: start_* saves the operation URL to a state blob, and
process_pending() (called by a frequent timer) polls until done, then builds
the outputs. This keeps every function invocation short enough for the
Consumption plan.
"""
import csv
import gzip
import io
import json
import logging
import os
import random
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from azure.storage.blob import BlobServiceClient, ContentSettings

GRAPH = "https://graph.microsoft.com/v1.0"
PKG = Path(__file__).resolve().parent
CONTAINER = "billing"

STATE_BLOB = "state.json"          # pending export operation
STATUS_BLOB = "status.json"        # last run status (for /api/status)
DASHBOARD_BLOB = "dashboard.html"
DATA_BLOB = "billing_data.json"    # legacy single-dataset blob (kept for /api/data.json)
CSV_BLOB = "customer_charges.csv"
PRICING_BLOB = "pricing.json"      # editable copy in blob storage

# Dual datasets: last billed invoice + current unbilled estimate
DATA_BLOBS = {"billed": "data_billed.json", "unbilled": "data_unbilled.json"}
CSV_BLOBS = {"billed": "customer_charges_billed.csv",
             "unbilled": "customer_charges_unbilled.csv"}

log = logging.getLogger("billing_core")


# ---------------------------------------------------------------- blob helpers

def _container():
    conn = os.environ["AzureWebJobsStorage"]
    svc = BlobServiceClient.from_connection_string(conn)
    c = svc.get_container_client(CONTAINER)
    try:
        c.create_container()
    except Exception:
        pass  # already exists
    return c


def read_blob_text(name):
    try:
        return _container().download_blob(name).readall().decode("utf-8")
    except Exception:
        return None


def write_blob(name, data, content_type="application/octet-stream"):
    if isinstance(data, str):
        data = data.encode("utf-8")
    _container().upload_blob(
        name, data, overwrite=True,
        content_settings=ContentSettings(content_type=content_type))


def delete_blob(name):
    try:
        _container().delete_blob(name)
    except Exception:
        pass


def set_status(state, message):
    write_blob(STATUS_BLOB, json.dumps({
        "state": state, "message": message,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }, indent=2), "application/json")
    log.info("status: %s - %s", state, message)


# ---------------------------------------------------------------- config

def get_config():
    cfg = {
        "tenant_id": os.environ.get("CSP_TENANT_ID", ""),
        "client_id": os.environ.get("CSP_CLIENT_ID", ""),
        "client_secret": os.environ.get("CSP_CLIENT_SECRET", ""),
        "default_currency": os.environ.get("CSP_DEFAULT_CURRENCY", "USD"),
    }
    missing = [k for k in ("tenant_id", "client_id", "client_secret") if not cfg[k]]
    if missing:
        raise RuntimeError(f"Missing app settings: {', '.join('CSP_' + m.upper() for m in missing)}")
    return cfg


def get_pricing():
    """Pricing from blob (editable without redeploy), falling back to the packaged file."""
    text = read_blob_text(PRICING_BLOB)
    if text:
        try:
            return json.loads(text)
        except ValueError:
            log.warning("pricing.json blob is invalid JSON; using packaged copy")
    packaged = (PKG / "pricing.json").read_text(encoding="utf-8-sig")
    write_blob(PRICING_BLOB, packaged, "application/json")  # seed editable copy
    return json.loads(packaged)


# ---------------------------------------------------------------- HTTP / Graph

def http_json(url, method="GET", body=None, headers=None, raw=False, timeout=300):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read()
            if raw:
                return r.status, dict(r.headers), content
            return r.status, dict(r.headers), (json.loads(content) if content else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"HTTP {e.code} calling {url}\n{detail}")


def get_token(cfg):
    url = f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token"
    form = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(url, data=form, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Authentication failed (HTTP {e.code}): {e.read().decode(errors='replace')[:1000]}")


# ---------------------------------------------------------------- export flow

def start_export(invoice_id=None, unbilled_period=None, currency=None):
    """Kick off an export and persist the operation URL for the poller."""
    cfg = get_config()
    token = get_token(cfg)
    auth = {"Authorization": f"Bearer {token}"}
    if invoice_id:
        url = f"{GRAPH}/reports/partners/billing/reconciliation/billed/export"
        body = {"invoiceId": invoice_id, "attributeSet": "full"}
        label = f"Invoice {invoice_id}"
        slot = "billed"
    else:
        cur = currency or cfg["default_currency"]
        url = f"{GRAPH}/reports/partners/billing/reconciliation/unbilled/export"
        body = {"billingPeriod": unbilled_period or "current",
                "currencyCode": cur, "attributeSet": "full"}
        label = f"Unbilled ({unbilled_period or 'current'} period, {cur})"
        slot = "unbilled"
    status, headers, _ = http_json(url, "POST", body, auth)
    location = headers.get("Location") or headers.get("location")
    if status != 202 or not location:
        raise RuntimeError(f"Export request not accepted (HTTP {status})")
    write_blob(STATE_BLOB, json.dumps({
        "operation_url": location, "label": label, "slot": slot,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }), "application/json")
    set_status("running", f"Export started: {label}. Data will appear in ~5-15 minutes.")
    return label


def process_pending():
    """Called by the poller timer. Checks the pending export; builds outputs when ready."""
    text = read_blob_text(STATE_BLOB)
    if not text:
        return "idle"
    state = json.loads(text)
    cfg = get_config()
    token = get_token(cfg)
    auth = {"Authorization": f"Bearer {token}"}
    _, _, op = http_json(state["operation_url"], "GET", None, auth)
    op_status = (op.get("status") or "").lower()
    log.info("export operation status: %s", op_status)

    if op_status == "failed":
        delete_blob(STATE_BLOB)
        set_status("failed", f"Export failed: {json.dumps(op.get('error', op))[:800]}")
        return "failed"
    if op_status != "succeeded":
        set_status("running", f"{state['label']}: export still {op_status or 'running'}...")
        return "running"

    rows = download_export(token, op["resourceLocation"])
    delete_blob(STATE_BLOB)
    if not rows:
        set_status("failed", f"{state['label']}: export succeeded but contained no line items")
        return "empty"
    build_outputs(rows, state["label"], state.get("slot", "unbilled"))
    return "done"


def download_export(token, resource_location):
    auth = {"Authorization": f"Bearer {token}"}
    if isinstance(resource_location, dict):
        manifest = resource_location  # Graph sometimes embeds the manifest inline
    else:
        _, _, manifest = http_json(resource_location, "GET", None, auth)
    root = manifest.get("rootDirectory", "").rstrip("/")
    sas = (manifest.get("sasToken") or "").lstrip("?")
    rows = []
    for blob in manifest.get("blobs", []):
        url = f"{root}/{blob['name']}"
        if sas:
            url += f"?{sas}"
        log.info("downloading %s", blob["name"])
        _, _, content = http_json(url, "GET", None, None, raw=True)
        rows.extend(parse_content(content, blob["name"]))
    log.info("downloaded %d line items from %d file(s)",
             len(rows), len(manifest.get("blobs", [])))
    return rows


# ---------------------------------------------------------------- parsing

def maybe_gunzip(content, name=""):
    if content[:2] == b"\x1f\x8b" or name.endswith(".gz"):
        return gzip.decompress(content)
    return content


def parse_content(content, name=""):
    data = maybe_gunzip(content, name)
    text = data.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    if text[0] in "[{":
        if text[0] == "[":
            return json.loads(text)
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    return list(csv.DictReader(io.StringIO(text)))


FIELD_ALIASES = {
    "customer_id": ["customerid", "customer_id", "customertenantid"],
    "customer_name": ["customername", "customer_name", "customercompanyname"],
    "customer_domain": ["customerdomainname", "customer_domain", "domainname"],
    "product": ["productname", "product_name", "offername", "product"],
    "sku": ["skuname", "sku_name", "sku"],
    "charge_type": ["chargetype", "charge_type"],
    "quantity": ["billablequantity", "quantity", "billable_quantity"],
    "unit_price": ["effectiveunitprice", "unitprice", "effective_unit_price", "unit_price"],
    "subtotal": ["subtotal", "sub_total", "pretaxtotal"],
    "tax": ["taxtotal", "tax", "tax_total"],
    "total": ["totalforcustomer", "total", "posttaxtotal"],
    "currency": ["billingcurrency", "currency", "currencycode", "pricingcurrency"],
    "start": ["chargestartdate", "charge_start_date", "usagedate"],
    "end": ["chargeenddate", "charge_end_date"],
    "invoice": ["invoicenumber", "invoice_number", "invoiceid"],
    "subscription": ["subscriptiondescription", "subscription_description", "subscriptionid"],
}


def get_field(row_lc, key, default=""):
    for alias in FIELD_ALIASES[key]:
        if alias in row_lc and row_lc[alias] not in (None, ""):
            return row_lc[alias]
    return default


def to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------- aggregation

def markup_for(customer, pricing):
    table = {k.strip().lower(): v for k, v in (pricing.get("customers") or {}).items()}
    for key in (customer["id"], customer["domain"], customer["name"]):
        entry = table.get(str(key).strip().lower())
        if entry is not None:
            return float(entry.get("markup_percent", pricing.get("default_markup_percent", 0)))
    return float(pricing.get("default_markup_percent", 0))


def aggregate(rows, pricing, source_label):
    customers = {}
    currency = ""
    period_start, period_end = "", ""
    invoices = set()
    for raw in rows:
        row = {str(k).lower().replace(" ", ""): v for k, v in raw.items()}
        cid = str(get_field(row, "customer_id", "unknown"))
        c = customers.setdefault(cid, {
            "id": cid,
            "name": get_field(row, "customer_name", cid),
            "domain": get_field(row, "customer_domain"),
            "cost_subtotal": 0.0, "cost_tax": 0.0, "cost_total": 0.0,
            "line_count": 0, "products": {},
        })
        subtotal = to_float(get_field(row, "subtotal"))
        tax = to_float(get_field(row, "tax"))
        total = to_float(get_field(row, "total")) or (subtotal + tax)
        c["cost_subtotal"] += subtotal
        c["cost_tax"] += tax
        c["cost_total"] += total
        c["line_count"] += 1
        currency = get_field(row, "currency", currency) or currency

        start = str(get_field(row, "start", ""))[:10]
        end = str(get_field(row, "end", ""))[:10]
        if start:
            period_start = min(period_start, start) if period_start else start
        if end:
            period_end = max(period_end, end) if period_end else end
        inv = get_field(row, "invoice")
        if inv:
            invoices.add(str(inv))

        sub = get_field(row, "subscription")
        ctype = get_field(row, "charge_type")
        pkey = f'{get_field(row, "product", "Unknown product")} | {get_field(row, "sku")} | {sub} | {ctype}'
        p = c["products"].setdefault(pkey, {
            "product": get_field(row, "product", "Unknown product"),
            "sku": get_field(row, "sku"),
            "subscription": sub,
            "charge_type": ctype,
            "start": start, "end": end,
            "quantity": 0.0, "cost": 0.0,
        })
        p["quantity"] += to_float(get_field(row, "quantity"))
        p["cost"] += total
        if start and (not p["start"] or start < p["start"]):
            p["start"] = start
        if end and (not p["end"] or end > p["end"]):
            p["end"] = end

    result = []
    for c in customers.values():
        mk = markup_for(c, pricing)
        charge = c["cost_total"] * (1 + mk / 100)
        prods = []
        for p in c["products"].values():
            p_charge = p["cost"] * (1 + mk / 100)
            prods.append({**p, "cost": round(p["cost"], 2),
                          "charge": round(p_charge, 2),
                          "quantity": round(p["quantity"], 4)})
        prods.sort(key=lambda x: -x["cost"])
        result.append({
            "id": c["id"], "name": c["name"], "domain": c["domain"],
            "line_count": c["line_count"],
            "cost_subtotal": round(c["cost_subtotal"], 2),
            "cost_tax": round(c["cost_tax"], 2),
            "cost_total": round(c["cost_total"], 2),
            "markup_percent": mk,
            "charge_total": round(charge, 2),
            "margin": round(charge - c["cost_total"], 2),
            "products": prods,
        })
    result.sort(key=lambda x: -x["cost_total"])
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": source_label,
        "period": {"start": period_start, "end": period_end},
        "invoices": sorted(invoices),
        "currency": currency or "USD",
        "totals": {
            "cost": round(sum(c["cost_total"] for c in result), 2),
            "charge": round(sum(c["charge_total"] for c in result), 2),
            "margin": round(sum(c["margin"] for c in result), 2),
            "customers": len(result),
        },
        "customers": result,
    }


# ---------------------------------------------------------------- outputs

def build_outputs(rows, label, slot="unbilled"):
    pricing = get_pricing()
    data = aggregate(rows, pricing, label)

    write_blob(DATA_BLOBS[slot], json.dumps(data, indent=2), "application/json")
    write_blob(DATA_BLOB, json.dumps(data, indent=2), "application/json")  # legacy

    csv_text = make_csv(data)
    write_blob(CSV_BLOBS[slot], csv_text, "text/csv; charset=utf-8")
    write_blob(CSV_BLOB, csv_text, "text/csv; charset=utf-8")  # legacy = most recent run

    rebuild_dashboard()

    t = data["totals"]
    set_status("done", f"{label} [{slot}]: {t['customers']} customers | cost {t['cost']:,.2f} "
                       f"| charge {t['charge']:,.2f} | margin {t['margin']:,.2f} {data['currency']}")
    return data


def make_csv(data):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Customer", "Domain", "Currency", "Microsoft Cost",
                "Markup %", "Charge to Customer", "Margin"])
    for c in data["customers"]:
        w.writerow([c["name"], c["domain"], data["currency"], c["cost_total"],
                    c["markup_percent"], c["charge_total"], c["margin"]])
    return "﻿" + buf.getvalue()


def rebuild_dashboard():
    """Render the dashboard with both datasets (billed invoice + unbilled estimate)."""
    combined = {}
    for slot, blob in DATA_BLOBS.items():
        text = read_blob_text(blob)
        combined[slot] = json.loads(text) if text else None
    template = (PKG / "dashboard_template.html").read_text(encoding="utf-8")
    write_blob(DASHBOARD_BLOB, template.replace("__DATA__", json.dumps(combined)),
               "text/html; charset=utf-8")


# ---------------------------------------------------------------- sample data

def run_sample():
    random.seed(42)
    demo = [
        ("Alpha Tech Ltd", "alphatech.onmicrosoft.com"),
        ("Ben David & Co", "bendavid.onmicrosoft.com"),
        ("Carmel Insurance", "carmelins.onmicrosoft.com"),
        ("Delta Logistics", "deltalog.onmicrosoft.com"),
        ("Eshkol Group", "eshkol.onmicrosoft.com"),
    ]
    products = [
        ("Microsoft 365 Business Premium", "M365 Bus Prem", 22.0),
        ("Microsoft 365 Business Standard", "M365 Bus Std", 12.5),
        ("Exchange Online (Plan 1)", "EXO P1", 4.0),
        ("Microsoft Defender for Business", "MDB", 3.0),
        ("Azure Plan", "Consumption", 1.0),
    ]
    rows = []
    for i, (name, domain) in enumerate(demo):
        for prod, sku, price in random.sample(products, k=random.randint(2, 4)):
            qty = random.randint(3, 60) if sku != "Consumption" else round(random.uniform(50, 900), 2)
            unit = price if sku != "Consumption" else 1.0
            sub = round(qty * unit, 2)
            tax = round(sub * 0.17, 2)
            rows.append({
                "CustomerId": f"cust-{i+1:03d}", "CustomerName": name,
                "CustomerDomainName": domain, "ProductName": prod, "SkuName": sku,
                "ChargeType": "new" if sku != "Consumption" else "usage",
                "BillableQuantity": qty, "EffectiveUnitPrice": unit,
                "Subtotal": sub, "TaxTotal": tax, "TotalForCustomer": round(sub + tax, 2),
                "BillingCurrency": "USD", "InvoiceNumber": "G-SAMPLE-001",
                "ChargeStartDate": "2026-06-01", "ChargeEndDate": "2026-06-30",
            })
    return build_outputs(rows, "Sample data (demo)")
