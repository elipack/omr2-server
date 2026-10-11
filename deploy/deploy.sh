#!/usr/bin/env bash
# Deploy omr2-server to Google Cloud Run.
#
# One-time setup (before first run):
#   1. gcloud auth login
#   2. gcloud projects create omr2-server-prod   (or pick an existing project)
#   3. Link a billing account in the console (required even for free-tier use)
#   4. gcloud config set project omr2-server-prod
#   5. gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
#        artifactregistry.googleapis.com
#   6. Set a budget alert (console → Billing → Budgets & alerts → $5/month).
#   7. Generate a token:  openssl rand -hex 32
#      and store it:      gcloud secrets create omr2-token --data-file=- <<< "$TOKEN"
#      (or pass it inline via OMR2_TOKEN below for v1 simplicity)
#
# AGPL §13 REMINDER: this service must not run publicly unless the source
# repo (github.com/elipack/omr2-server) is PUBLIC. Flip it before deploying.
#
# Usage:
#   OMR2_TOKEN=<hex> ./deploy/deploy.sh

set -euo pipefail

REGION="${REGION:-us-central1}"          # tier-1 pricing; free tier applies
SERVICE="${SERVICE:-omr2-server}"

if [[ -z "${OMR2_TOKEN:-}" ]]; then
  echo "OMR2_TOKEN is not set. Generate one with: openssl rand -hex 32" >&2
  exit 1
fi

# Config per plan: 2 vCPU / 2 GiB, one request per instance (pipeline is
# CPU-bound), scale to zero, max 3 instances as the cost ceiling, 300 s
# timeout, CPU only during requests (the free-tier billing mode), startup
# boost to cut cold-start latency.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --cpu 2 \
  --memory 2Gi \
  --concurrency 1 \
  --min-instances 0 \
  --max-instances 3 \
  --timeout 300 \
  --cpu-boost \
  --allow-unauthenticated \
  --set-env-vars "OMR2_TOKEN=${OMR2_TOKEN}"

echo
echo "Deployed. Verify with:"
echo "  curl \$(gcloud run services describe $SERVICE --region $REGION --format 'value(status.url)')/health"
