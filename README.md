# CSP Billing Control Panel

Pulls billing data from Microsoft Partner Center, applies your per-customer markup, and generates a dashboard showing cost, charge, and margin per customer.

## Files

| File | Purpose |
|---|---|
| `csp_billing.py` | Main script (no dependencies, Python 3.8+) |
| `config.json` | Your API credentials (copy from `config.sample.json`) |
| `pricing.json` | Per-customer markup % |
| `dashboard_template.html` | Dashboard design (edit to customize) |
| `dashboard.html` | **Output** — open in browser |
| `customer_charges.csv` | **Output** — per-customer summary for invoicing (opens in Excel) |
| `billing_data.json` | **Output** — full aggregated data |

## One-time setup: register an app in your partner tenant

1. Go to [entra.microsoft.com](https://entra.microsoft.com) → **App registrations** → **New registration**. Name it e.g. `CSP-Billing-Reader`, single tenant, no redirect URI.
2. In the app: **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions** → search `PartnerBilling.Read.All` → add it.
3. Click **Grant admin consent** (requires Global Admin).
4. **Certificates & secrets** → **New client secret** → copy the secret **value** immediately.
5. Copy `config.sample.json` to `config.json` and fill in:
   - `tenant_id` — your partner tenant ID (Entra → Overview)
   - `client_id` — the app's Application (client) ID
   - `client_secret` — the secret value from step 4

Keep `config.json` private — it grants read access to your billing data.

## Set your markups

Edit `pricing.json`. Match customers by name, domain, or customer ID:

```json
{
  "default_markup_percent": 15,
  "customers": {
    "customer.onmicrosoft.com": { "markup_percent": 20 }
  }
}
```

## Monthly usage

```bash
# Billed reconciliation for a specific invoice (invoice ID from Partner Center → Billing)
python3 csp_billing.py --invoice G016907411

# Or unbilled charges for the current/last billing period
python3 csp_billing.py --unbilled current --currency USD

# Or process a reconciliation file downloaded manually from Partner Center
python3 csp_billing.py --input reconciliation.csv.gz

# Demo with sample data
python3 csp_billing.py --sample
```

Then open `dashboard.html` in your browser. `customer_charges.csv` has the per-customer amounts for invoicing.

Note: the export is asynchronous on Microsoft's side — the script polls until it's ready, typically 1–15 minutes for large invoices.

## Where to find the invoice ID

Partner Center → **Billing** workspace → **Billing history** → the invoice number (starts with `G`).
