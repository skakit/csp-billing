"""
CSP Billing Control Panel — Azure Functions app (Python v2 model).

Timers:
  daily_refresh   05:00 UTC daily  — starts an unbilled export for the current period
  poll_pending    every 10 min     — completes any pending export and rebuilds outputs

HTTP endpoints (key-protected; append ?code=<function key>):
  GET /api/dashboard              — the control panel (HTML)
  GET /api/charges.csv            — per-customer summary for invoicing
  GET /api/data.json              — full aggregated data
  GET /api/status                 — last run status
  GET /api/refresh                — start unbilled refresh now (&period=current|last, &currency=USD)
  GET /api/refresh?invoice=G...   — start billed reconciliation for an invoice
  GET /api/refresh?mode=sample    — populate with demo data (instant, for testing)
"""
import json
import logging

import azure.functions as func

import billing_core as core

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.timer_trigger(schedule="0 0 5 * * *", arg_name="timer", run_on_startup=False)
def daily_refresh(timer: func.TimerRequest):
    try:
        core.start_export(unbilled_period="current")
    except Exception as e:
        logging.exception("daily refresh failed")
        core.set_status("failed", f"Daily refresh failed to start: {e}")


@app.timer_trigger(schedule="0 */10 * * * *", arg_name="timer", run_on_startup=False)
def poll_pending(timer: func.TimerRequest):
    try:
        core.process_pending()
    except Exception as e:
        logging.exception("poller failed")
        core.set_status("failed", f"Polling failed: {e}")


@app.route(route="dashboard", methods=["GET"])
def dashboard(req: func.HttpRequest) -> func.HttpResponse:
    html = core.read_blob_text(core.DASHBOARD_BLOB)
    if not html:
        status = core.read_blob_text(core.STATUS_BLOB) or "{}"
        html = ("<html><body style='font-family:sans-serif;background:#0f1420;color:#e8ecf5;"
                "padding:40px'><h1>No billing data yet</h1>"
                "<p>Run <code>/api/refresh</code> (or wait for the daily 05:00 UTC pull), "
                "then reload this page in ~10-15 minutes.</p>"
                f"<pre>{status}</pre></body></html>")
    return func.HttpResponse(html, mimetype="text/html")


@app.route(route="charges.csv", methods=["GET"])
def charges_csv(req: func.HttpRequest) -> func.HttpResponse:
    text = core.read_blob_text(core.CSV_BLOB)
    if not text:
        return func.HttpResponse("No data yet. Run /api/refresh first.", status_code=404)
    return func.HttpResponse(text, mimetype="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=customer_charges.csv"})


@app.route(route="data.json", methods=["GET"])
def data_json(req: func.HttpRequest) -> func.HttpResponse:
    text = core.read_blob_text(core.DATA_BLOB)
    if not text:
        return func.HttpResponse('{"error": "No data yet"}', status_code=404,
                                 mimetype="application/json")
    return func.HttpResponse(text, mimetype="application/json")


@app.route(route="status", methods=["GET"])
def status(req: func.HttpRequest) -> func.HttpResponse:
    text = core.read_blob_text(core.STATUS_BLOB) or '{"state": "never_run"}'
    return func.HttpResponse(text, mimetype="application/json")


@app.route(route="refresh", methods=["GET", "POST"])
def refresh(req: func.HttpRequest) -> func.HttpResponse:
    try:
        if req.params.get("mode") == "sample":
            data = core.run_sample()
            return _json({"started": False, "done": True,
                          "message": "Sample data generated. Open /api/dashboard.",
                          "totals": data["totals"]})
        invoice = req.params.get("invoice")
        if invoice:
            label = core.start_export(invoice_id=invoice)
        else:
            label = core.start_export(
                unbilled_period=req.params.get("period", "current"),
                currency=req.params.get("currency"))
        return _json({"started": True, "label": label,
                      "message": "Export started. Microsoft takes ~5-15 minutes; "
                                 "the dashboard updates automatically. "
                                 "Check /api/status for progress."})
    except Exception as e:
        logging.exception("refresh failed")
        return _json({"error": str(e)}, 500)


def _json(obj, status_code=200):
    return func.HttpResponse(json.dumps(obj, indent=2), status_code=status_code,
                             mimetype="application/json")
