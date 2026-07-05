# CSP Billing Control Panel — Azure Deployment

Runs the billing system in the cloud: an Azure Function pulls charges from Partner Center **daily at 05:00 UTC** and serves a key-protected dashboard you can open from any browser.

**Cost: ~$0/month** — the consumption plan's free grant covers this workload; storage is pennies.

## Architecture

```
Timer (daily 05:00 UTC) ──► start unbilled export (Microsoft Graph)
Timer (every 10 min)    ──► poll export → aggregate + markup → blob storage
Browser ──► /api/dashboard?code=KEY ──► dashboard.html from blob storage
```

## Deploy (one time, ~5 minutes)

1. Install the [Azure CLI](https://aka.ms/azure-cli) and run `az login` (use your partner-tenant account with an Azure subscription).
2. Have your API credentials ready — same values as `config.json` (tenant ID, client ID, client secret; see main README for the app-registration steps).
3. Run:

```bash
cd azure
chmod +x deploy.sh
./deploy.sh
```

The script prints your dashboard URL at the end — **save it, the `?code=` key is the password.**

Optional: set region or names before running, e.g. `LOCATION=israelcentral ./deploy.sh`

## Verify it works

Open `.../api/refresh?mode=sample&code=KEY` in your browser — this loads demo data instantly. Then open the dashboard URL. When ready for real data, open `.../api/refresh?code=KEY` and wait ~10–15 minutes (Microsoft's export is asynchronous).

## URLs

| URL | Purpose |
|---|---|
| `/api/dashboard?code=KEY` | The control panel |
| `/api/charges.csv?code=KEY` | Per-customer summary for invoicing (Excel) |
| `/api/data.json?code=KEY` | Full aggregated data |
| `/api/status?code=KEY` | Last run status |
| `/api/refresh?code=KEY` | Refresh unbilled charges now (`&period=last`, `&currency=ILS`) |
| `/api/refresh?invoice=G016907411&code=KEY` | Billed reconciliation for an invoice |

## Changing markups (no redeploy needed)

Markups live in blob `pricing.json` in the storage account (container `billing`). Edit it in Azure Portal → storage account → Containers → billing → pricing.json → Edit, then trigger `/api/refresh`.

## Updating the code

Edit files in `function_app/` and re-run `./deploy.sh` — it detects existing resources and only pushes code.

## Changing the schedule

Edit the cron expressions in `function_app/function_app.py` (`daily_refresh` = `0 0 5 * * *` is 05:00 UTC daily) and redeploy.

## Security notes

- Credentials are stored as encrypted Function App settings, never in code.
- All endpoints require the function key (`?code=`). Regenerate it in Portal → Function App → App keys if it leaks.
- The client secret expires (per what you set at creation) — when it does, create a new secret in Entra and update the `CSP_CLIENT_SECRET` app setting in the portal.

## Teardown

```bash
az group delete -n rg-csp-billing
```
