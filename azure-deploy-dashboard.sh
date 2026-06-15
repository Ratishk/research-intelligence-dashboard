#!/usr/bin/env bash
# azure-deploy-dashboard.sh — Build & deploy the Research Intelligence Dashboard to
# Azure Container Apps. Modeled on LogiqGPT's azure-deploy.sh.
#
# SAFETY: this script DOES NOTHING by default. The default invocation is a DRY RUN —
# it prints exactly what it WOULD do and never calls `az`. You must pass --confirm
# (and have a human review the printed plan) to actually create/modify Azure resources.
#
# Usage:
#   ./azure-deploy-dashboard.sh             # DRY RUN — prints the plan, calls no az, exits 0
#   ./azure-deploy-dashboard.sh --confirm   # REAL DEPLOY — requires human approval first
#   ./azure-deploy-dashboard.sh --help
#
# Prerequisites (only needed for --confirm):
#   - Azure CLI installed (brew install azure-cli) and logged in (az login)
#   - A populated .env in this directory (NOT committed) with the ingestion/LLM keys
#   - No Docker needed — the image is built inside Azure Container Registry (az acr build).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Configuration (edit these if you want different names) ─────────────────
AZURE_RESOURCE_GROUP="research-dashboard-rg"
AZURE_LOCATION="eastus"
ACR_NAME="researchdashacr"          # 5-50 chars, alphanumeric, globally unique
APP_NAME="research-dashboard"
STORAGE_ACCOUNT="researchdashstore"  # max 24 chars, lowercase, no hyphens
ENV_NAME="${APP_NAME}-env"
SHARE_NAME="research-dashboard-data"
IMAGE="${ACR_NAME}.azurecr.io/${APP_NAME}:latest"
# Persistent SQLite + data live on local disk seeded from the Azure Files share.
SHARE_MOUNT="/app/data"
LOCAL_DIR="/app/local"
# ───────────────────────────────────────────────────────────────────────────

# ── Arg parsing ────────────────────────────────────────────────────────────
CONFIRM=false
for arg in "$@"; do
  case "$arg" in
    --confirm) CONFIRM=true ;;
    -h | --help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 1
      ;;
  esac
done

# ── run(): the single choke point that either prints or executes ──────────
# In dry-run mode it ONLY echoes the command — `az` is never invoked. In confirm
# mode it executes. This is the hard stop that prevents accidental deploys.
run() {
  if [[ "$CONFIRM" == true ]]; then
    echo "+ $*"
    "$@"
  else
    echo "    WOULD RUN: $*"
  fi
}

echo ""
echo "================================================================"
echo "  Research Intelligence Dashboard — Azure Container Apps deploy"
echo "  Resource Group : $AZURE_RESOURCE_GROUP"
echo "  Location       : $AZURE_LOCATION"
echo "  App Name       : $APP_NAME"
if [[ "$CONFIRM" == true ]]; then
  echo "  Mode           : LIVE DEPLOY (--confirm given)"
else
  echo "  Mode           : DRY RUN (default) — no az calls, nothing will change"
fi
echo "================================================================"
echo ""

# ── HARD STOP: require explicit confirmation + human approval ──────────────
if [[ "$CONFIRM" != true ]]; then
  echo ">>> DRY RUN. The plan below would be executed with --confirm."
  echo ">>> Re-run with --confirm ONLY after a human has reviewed this plan."
  echo ""
fi

# ── 1. Resource group ──────────────────────────────────────────────────────
echo ">>> [1/7] Create resource group"
run az group create \
  --name "$AZURE_RESOURCE_GROUP" \
  --location "$AZURE_LOCATION" \
  --output none

# ── 2. Container Registry ──────────────────────────────────────────────────
echo ">>> [2/7] Create Azure Container Registry"
run az acr create \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --sku Basic \
  --admin-enabled true \
  --output none

# ── 3. Build & push image (cloud build — no local Docker) ──────────────────
echo ">>> [3/7] Build image in ACR (~3-5 min)"
run az acr build \
  --registry "$ACR_NAME" \
  --image "${APP_NAME}:latest" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  "$ROOT"

# ── 4. Storage account + Azure File Share (persistent master copy) ─────────
echo ">>> [4/7] Create storage account + file share"
run az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --location "$AZURE_LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --output none

run az storage share create \
  --name "$SHARE_NAME" \
  --account-name "$STORAGE_ACCOUNT" \
  --quota 50 \
  --output none

# Storage key is only fetched/used in confirm mode; in dry run it stays unset.
STORAGE_KEY=""
if [[ "$CONFIRM" == true ]]; then
  STORAGE_KEY=$(az storage account keys list \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --account-name "$STORAGE_ACCOUNT" \
    --query "[0].value" \
    --output tsv)
fi

# ── 5. Container Apps environment + mount the share ────────────────────────
echo ">>> [5/7] Create Container Apps environment + storage mount"
run az containerapp env create \
  --name "$ENV_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --location "$AZURE_LOCATION" \
  --output none

run az containerapp env storage set \
  --name "$ENV_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --storage-name "$SHARE_NAME" \
  --azure-file-account-name "$STORAGE_ACCOUNT" \
  --azure-file-account-key "${STORAGE_KEY:-<STORAGE_KEY>}" \
  --azure-file-share-name "$SHARE_NAME" \
  --access-mode ReadWrite \
  --output none

# ── 6. Split .env into env-vars and Container Apps secrets ─────────────────
echo ">>> [6/7] Read .env → env vars + secrets"
ENV_ARGS=()
SECRET_ARGS=()

# Keys matching these patterns become Container Apps secrets (never plain env values).
_is_secret_key() {  # return 0 = secret-looking
  case "$1" in
    *_API_KEY|*_SECRET|*_TOKEN|*PASSWORD|*_PASS|*_KEY) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ -f "${ROOT}/.env" ]]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line// }" ]] && continue
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*=.+ ]] || continue
    key="${line%%=*}"; val="${line#*=}"
    if _is_secret_key "$key"; then
      sname="env-$(printf '%s' "$key" | tr '[:upper:]_' '[:lower:]-')"
      SECRET_ARGS+=("${sname}=${val}")
      ENV_ARGS+=("${key}=secretref:${sname}")
    else
      ENV_ARGS+=("$line")
    fi
  done < "${ROOT}/.env"
else
  echo "    WARNING: no .env found in ${ROOT} — deploy will have no ingestion/LLM keys."
fi

# DASHBOARD_API_KEY: the shared key LogiqGPT sends as X-API-Key. Generated here if the
# operator didn't put one in .env. (NOTE: the app does not yet ENFORCE this header — see
# DEPLOY.md "Auth follow-up"; this wires the secret so enforcement is a one-file change.)
if ! printf '%s\n' "${ENV_ARGS[@]+"${ENV_ARGS[@]}"}" | grep -q '^DASHBOARD_API_KEY='; then
  if [[ "$CONFIRM" == true ]]; then
    DASHBOARD_API_KEY="$(openssl rand -hex 24)"
  else
    DASHBOARD_API_KEY="<generated-at-deploy: openssl rand -hex 24>"
  fi
  SECRET_ARGS+=("dashboard-api-key=${DASHBOARD_API_KEY}")
  ENV_ARGS+=("DASHBOARD_API_KEY=secretref:dashboard-api-key")
fi

# SQLite on LOCAL disk (NOT the SMB share — SMB breaks WAL). /app/data is the share,
# /app/local is local disk; run.py is expected to seed local from the share at startup.
ENV_ARGS+=("DATABASE_URL=sqlite:////app/local/research.db")
ENV_ARGS+=("DASHBOARD_SHARE_DATA_DIR=${SHARE_MOUNT}")
ENV_ARGS+=("DASHBOARD_LOCAL_DATA_DIR=${LOCAL_DIR}")

echo "    env vars      : ${#ENV_ARGS[@]}"
echo "    secrets split : ${#SECRET_ARGS[@]} (values redacted)"
for e in "${ENV_ARGS[@]}"; do
  # Print only secretref pointers and non-secret keys — never the secret value.
  [[ "$e" == *=secretref:* ]] && echo "      $e"
done

ACR_PASSWORD="<acr-password>"
if [[ "$CONFIRM" == true ]]; then
  ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" --output tsv)
fi

# The container-app create is handled by an explicit if/else (NOT run()) so secret
# VALUES are never echoed: dry-run prints names as <redacted>; --confirm passes the
# real values straight to az without echoing the array.
SECRET_NAMES_REDACTED=()
for s in "${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"}"; do
  SECRET_NAMES_REDACTED+=("${s%%=*}=<redacted>")
done

# ── 7. Create the Container App ────────────────────────────────────────────
# min-replicas=1 / max-replicas=1: the in-process APScheduler in run.py dies if the
# container scales to zero, so we pin exactly one always-warm replica (no scale-to-zero).
echo ">>> [7/7] Create the Container App (min=max=1 replica; scheduler must stay warm)"
if [[ "$CONFIRM" == true ]]; then
  echo "+ az containerapp create ... (secret values passed to az, not echoed)"
  az containerapp create \
    --name "$APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --environment "$ENV_NAME" \
    --image "$IMAGE" \
    --target-port 8000 \
    --ingress external \
    --min-replicas 1 \
    --max-replicas 1 \
    --cpu 1 \
    --memory 2Gi \
    --registry-server "${ACR_NAME}.azurecr.io" \
    --registry-username "$ACR_NAME" \
    --registry-password "$ACR_PASSWORD" \
    ${SECRET_ARGS[@]+--secrets "${SECRET_ARGS[@]}"} \
    --env-vars "${ENV_ARGS[@]}" \
    --volume-name "$SHARE_NAME" \
    --volume-storage-type AzureFile \
    --volume-storage-name "$SHARE_NAME" \
    --mount-path "$SHARE_MOUNT" \
    --output none
else
  echo "    WOULD RUN: az containerapp create" \
    "--name $APP_NAME --resource-group $AZURE_RESOURCE_GROUP --environment $ENV_NAME" \
    "--image $IMAGE --target-port 8000 --ingress external --min-replicas 1 --max-replicas 1" \
    "--cpu 1 --memory 2Gi --registry-server ${ACR_NAME}.azurecr.io --registry-username $ACR_NAME" \
    "--registry-password <acr-password>" \
    "--secrets ${SECRET_NAMES_REDACTED[*]+${SECRET_NAMES_REDACTED[*]}}" \
    "--env-vars ${ENV_ARGS[*]}" \
    "--volume-name $SHARE_NAME --volume-storage-type AzureFile --volume-storage-name $SHARE_NAME" \
    "--mount-path $SHARE_MOUNT --output none"
fi

echo ""
if [[ "$CONFIRM" == true ]]; then
  APP_URL=$(az containerapp show \
    --name "$APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query "properties.configuration.ingress.fqdn" \
    --output tsv)
  echo "================================================================"
  echo "  Deploy complete!"
  echo "  Dashboard URL: https://${APP_URL}"
  echo ""
  echo "  Point LogiqGPT at it (staged, separate repo — see DEPLOY.md):"
  echo "    DASHBOARD_API_URL=https://${APP_URL}"
  echo "    DASHBOARD_API_KEY=<the dashboard-api-key secret value>"
  echo "================================================================"
else
  echo "================================================================"
  echo "  DRY RUN complete — NOTHING was deployed and no az call was made."
  echo "  Review the plan above, then (after human approval) re-run with:"
  echo "    ./azure-deploy-dashboard.sh --confirm"
  echo "================================================================"
fi
