#!/usr/bin/env bash
# ============================================================================
# CSP Billing Control Panel - one-command Azure deployment
#
# Prerequisites: Azure CLI (https://aka.ms/azure-cli), logged in via `az login`
# Usage:         ./deploy.sh            (re-run any time to update the code)
#
# Creates (first run): resource group, storage account, consumption-plan
# Function App. Prompts for your Partner Center API credentials.
# Estimated cost: ~$0/month (consumption free grant covers this workload).
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# ------------------------------------------------------- editable settings
LOCATION="${LOCATION:-westeurope}"          # e.g. westeurope, israelcentral, northeurope
RG="${RG:-rg-csp-billing}"
APP_NAME="${APP_NAME:-}"                    # leave empty to auto-generate

# --------------------------------------------------------------- checks
command -v az >/dev/null || { echo "ERROR: Azure CLI not found. Install: https://aka.ms/azure-cli"; exit 1; }
az account show >/dev/null 2>&1 || { echo "ERROR: not logged in. Run: az login"; exit 1; }
command -v zip >/dev/null || { echo "ERROR: 'zip' not found. Install it (e.g. brew install zip)"; exit 1; }

SUB=$(az account show --query name -o tsv)
echo "==> Deploying to subscription: $SUB (region: $LOCATION)"

# Stable names per subscription (so re-runs update the same resources)
SUFFIX=$(az account show --query id -o tsv | tr -d '-' | cut -c1-8)
[ -z "$APP_NAME" ] && APP_NAME="csp-billing-$SUFFIX"
STORAGE=$(echo "cspbill$SUFFIX" | tr -cd 'a-z0-9' | cut -c1-24)

# --------------------------------------------------------------- resources
echo "==> Resource group: $RG"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> Storage account: $STORAGE"
az storage account create -n "$STORAGE" -g "$RG" -l "$LOCATION" \
  --sku Standard_LRS --min-tls-version TLS1_2 --allow-blob-public-access false -o none

if ! az functionapp show -n "$APP_NAME" -g "$RG" >/dev/null 2>&1; then
  echo "==> Function App: $APP_NAME (Linux, Python 3.11, consumption plan)"
  az functionapp create -n "$APP_NAME" -g "$RG" \
    --storage-account "$STORAGE" --consumption-plan-location "$LOCATION" \
    --runtime python --runtime-version 3.11 --functions-version 4 \
    --os-type Linux -o none
else
  echo "==> Function App $APP_NAME already exists - updating code only"
fi

# --------------------------------------------------------------- credentials
HAS_CREDS=$(az functionapp config appsettings list -n "$APP_NAME" -g "$RG" \
  --query "[?name=='CSP_TENANT_ID'] | length(@)" -o tsv)
if [ "$HAS_CREDS" = "0" ]; then
  echo ""
  echo "==> Partner Center API credentials (from your Entra app registration,"
  echo "    see README - stored as encrypted Function App settings)"
  read -rp "    Partner tenant ID: " TENANT_ID
  read -rp "    App client ID:     " CLIENT_ID
  read -rsp "    Client secret:     " CLIENT_SECRET; echo ""
  read -rp "    Default currency [USD]: " CURRENCY
  az functionapp config appsettings set -n "$APP_NAME" -g "$RG" -o none --settings \
    "CSP_TENANT_ID=$TENANT_ID" "CSP_CLIENT_ID=$CLIENT_ID" \
    "CSP_CLIENT_SECRET=$CLIENT_SECRET" "CSP_DEFAULT_CURRENCY=${CURRENCY:-USD}"
else
  echo "==> Credentials already configured (delete CSP_* app settings in the portal to reset)"
fi

az functionapp config appsettings set -n "$APP_NAME" -g "$RG" -o none --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true ENABLE_ORYX_BUILD=true

# --------------------------------------------------------------- deploy code
echo "==> Packaging and deploying code (remote build takes ~2-3 min)..."
rm -f app.zip
(cd function_app && zip -rq ../app.zip . -x "__pycache__/*" "local.settings.json" ".venv/*")
az functionapp deployment source config-zip -n "$APP_NAME" -g "$RG" --src app.zip --build-remote true -o none
rm -f app.zip

# --------------------------------------------------------------- output URLs
echo "==> Retrieving access key..."
sleep 10
KEY=$(az functionapp keys list -n "$APP_NAME" -g "$RG" --query "functionKeys.default" -o tsv 2>/dev/null || true)
[ -z "$KEY" ] || [ "$KEY" = "None" ] && KEY=$(az functionapp keys list -n "$APP_NAME" -g "$RG" --query "masterKey" -o tsv)
BASE="https://$APP_NAME.azurewebsites.net/api"

echo ""
echo "============================================================================"
echo " DEPLOYED - save these URLs (the ?code= key is your password)"
echo "============================================================================"
echo " Dashboard:      $BASE/dashboard?code=$KEY"
echo " Invoicing CSV:  $BASE/charges.csv?code=$KEY"
echo " Refresh now:    $BASE/refresh?code=$KEY"
echo " Status:         $BASE/status?code=$KEY"
echo " Test w/ demo:   $BASE/refresh?mode=sample&code=$KEY"
echo "============================================================================"
echo " Billing data refreshes automatically every day at 05:00 UTC."
echo " Markups: edit blob 'billing/pricing.json' in storage account $STORAGE."
echo "============================================================================"
