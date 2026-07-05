#!/usr/bin/env python3
"""
CSP Billing Control Panel
Fetches billed/unbilled reconciliation data from the Microsoft Graph partner
billing API, applies per-customer markup from pricing.json, and generates:
  - dashboard.html        interactive control panel
  - billing_data.json     aggregated data
  - customer_charges.csv  per-customer summary for invoicing

Usage:
  python3 csp_billing.py --sample                    # demo with sample data
  python3 csp_billing.py --invoice G016907411        # billed reconciliation for an invoice
  python3 csp_billing.py --unbilled current          # unbilled recon (current|last period)
  python3 csp_billing.py --input recon.json.gz       # process a manually downloaded file (.json/.json.gz/.csv/.csv.gz)

No external dependencies (Python 3.8+ standard library only).
"""
import argparse
import csv
import gzip
import io
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
GRAPH = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------- utilities

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_json_file(path, required=True):
    p = Path(path)
    if not p.exists():
        if required:
            die(f"Missing file: {p}")
        return None
    with open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def http_json(url, method="GET", body=None, headers=None, raw=False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            content = r.read()
            if raw:
                return r.status, dict(r.headers), content
            parsed = json.loads(content) if content else {}
            return r.status, dict(r.headers), parsed
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        die(f"HTTP {e.code} calling {url}\n{detail}")


# ---------------------------------------------------------------- Graph API

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
        die(f"Authentication failed (HTTP {e.code}): {e.read().decode(errors='replace')[:1000]}")


def start_export(token, invoice_id=None, unbilled_period=None, currency=None):
    auth = {"Authorization": f"Bearer {token}"}
    if invoice_id:
        url = f"{GRAPH}/reports/partners/billing/reconciliation/billed/export"
        body = {"invoiceId": invoice_id, "attributeSet": "full"}
    else:
        url = f"{GRAPH}/reports/partners/billing/reconciliation/unbilled/export"
        body = {"billingPeriod": unbilled_period, "currencyCode": currency, "attributeSet": "full"}
    status, headers, _ = http_json(url, "POST", body, auth)
    location = headers.get("Location") or headers.get("location")
    if status != 202 or not location:
        die(f"Export request not accepted (HTTP {status})")
    print(f"Export started: {location}")
    return location


def poll_operation(token, op_url, timeout_s=1800):
    auth = {"Authorization": f"Bearer {token}"}
    start = time.time()
    while True:
        _, _, op = http_json(op_url, "GET", None, auth)
        status = (op.get("status") or "").lower()
        print(f"  operation status: {status}")
        if status == "succeeded":
            return op["resourceLocation"]
        if status == "failed":
            die(f"Export failed: {json.dumps(op.get('error', op))[:1000]}")
        if time.time() - start > timeout_s:
            die("Timed out waiting for export to complete")
        time.sleep(15)


def download_export(token, resource_location):
    """Download all blobs from the export manifest; return list of line-item dicts."""
    auth = {"Authorization": f"Bearer {token}"}
    _, _, manifest = http_json(resource_location, "GET", None, auth)
    root = manifest.get("rootDirectory", "").rstrip("/")
    sas = (manifest.get("sasToken") or "").lstrip("?")
    fmt = (manifest.get("dataFormat") or "compressedJSON").lower()
    rows = []
    for blob in manifest.get("blobs", []):
        url = f"{root}/{blob['name']}"
        if sas:
            url += f"?{sas}"
        print(f"  downloading {blob['name']} ...")
        _, _, content = http_json(url, "GET", None, None, raw=True)
        rows.extend(parse_content(content, blob["name"], fmt))
    print(f"Downloaded {len(rows)} line items from {len(manifest.get('blobs', []))} file(s)")
    return rows


# ---------------------------------------------------------------- parsing

def maybe_gunzip(content, name=""):
    if content[:2] == b"\x1f\x8b" or name.endswith(".gz"):
        return gzip.decompress(content)
    return content


def parse_content(content, name="", fmt=""):
    """Parse export content: gzip/plain, JSON-lines, JSON array, or CSV."""
    data = maybe_gunzip(content, name)
    text = data.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    if text[0] in "[{":
        if text[0] == "[":
            return json.loads(text)
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


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


# ---------------------------------------------------------------- pricing

def markup_for(customer, pricing):
    """Look up markup % by customer id, domain, or name (case-insensitive)."""
    table = {k.strip().lower(): v for k, v in (pricing.get("customers") or {}).items()}
    for key in (customer["id"], customer["domain"], customer["name"]):
        entry = table.get(str(key).strip().lower())
        if entry is not None:
            return float(entry.get("markup_percent", pricing.get("default_markup_percent", 0)))
    return float(pricing.get("default_markup_percent", 0))


# ---------------------------------------------------------------- aggregate

def aggregate(rows, pricing, source_label):
    customers = {}
    currency = ""
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

        pkey = f'{get_field(row, "product", "Unknown product")} | {get_field(row, "sku")}'
        p = c["products"].setdefault(pkey, {
            "product": get_field(row, "product", "Unknown product"),
            "sku": get_field(row, "sku"),
            "charge_type": get_field(row, "charge_type"),
            "quantity": 0.0, "cost": 0.0,
        })
        p["quantity"] += to_float(get_field(row, "quantity"))
        p["cost"] += total

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

def write_outputs(data):
    (BASE / "billing_data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    with open(BASE / "customer_charges.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Customer", "Domain", "Currency", "Microsoft Cost",
                    "Markup %", "Charge to Customer", "Margin"])
        for c in data["customers"]:
            w.writerow([c["name"], c["domain"], data["currency"], c["cost_total"],
                        c["markup_percent"], c["charge_total"], c["margin"]])

    template = (BASE / "dashboard_template.html").read_text(encoding="utf-8")
    html = template.replace("__DATA__", json.dumps(data))
    (BASE / "dashboard.html").write_text(html, encoding="utf-8")
    print(f"\nWrote: dashboard.html, billing_data.json, customer_charges.csv (in {BASE})")


# ---------------------------------------------------------------- sample data

def sample_rows():
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
    return rows


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="CSP billing control panel generator")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--invoice", help="Invoice ID for billed reconciliation (e.g. G016907411)")
    g.add_argument("--unbilled", choices=["current", "last"], help="Unbilled reconciliation period")
    g.add_argument("--input", help="Process a local reconciliation file (.json/.json.gz/.csv/.csv.gz)")
    g.add_argument("--sample", action="store_true", help="Generate dashboard with sample data")
    ap.add_argument("--currency", help="Currency code for unbilled export (default from config)")
    args = ap.parse_args()

    pricing = load_json_file(BASE / "pricing.json")

    if args.sample:
        rows, label = sample_rows(), "Sample data (demo)"
    elif args.input:
        content = Path(args.input).read_bytes()
        rows, label = parse_content(content, args.input), f"Local file: {Path(args.input).name}"
    else:
        cfg = load_json_file(BASE / "config.json")
        token = get_token(cfg)
        print("Authenticated with Microsoft Graph")
        if args.invoice:
            op = start_export(token, invoice_id=args.invoice)
            label = f"Invoice {args.invoice}"
        else:
            cur = args.currency or cfg.get("default_currency", "USD")
            op = start_export(token, unbilled_period=args.unbilled, currency=cur)
            label = f"Unbilled ({args.unbilled} period, {cur})"
        resource = poll_operation(token, op)
        rows = download_export(token, resource)

    if not rows:
        die("No line items found")
    data = aggregate(rows, pricing, label)
    write_outputs(data)
    t = data["totals"]
    print(f"\nSummary: {t['customers']} customers | cost {t['cost']:,.2f} "
          f"| charge {t['charge']:,.2f} | margin {t['margin']:,.2f} {data['currency']}")


if __name__ == "__main__":
    main()
