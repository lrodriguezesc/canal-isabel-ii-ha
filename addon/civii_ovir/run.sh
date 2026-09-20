#!/usr/bin/env bash
set -e

OPTIONS_FILE="/data/options.json"

export CIVII_USERNAME="$(jq -r '.username' "$OPTIONS_FILE")"
export CIVII_PASSWORD="$(jq -r '.password' "$OPTIONS_FILE")"
export CIVII_DOCUMENT_TYPE="$(jq -r '.document_type' "$OPTIONS_FILE")"
export CIVII_SCAN_INTERVAL_MINUTES="$(jq -r '.scan_interval_minutes' "$OPTIONS_FILE")"
export CIVII_HISTORY_WINDOW_DAYS="$(jq -r '.history_window_days' "$OPTIONS_FILE")"
export CIVII_ANOMALY_THRESHOLD_LITERS="$(jq -r '.anomaly_threshold_liters' "$OPTIONS_FILE")"
export CIVII_INGRESS_PORT="8099"

echo "Starting civii_ovir..."
cd /app
exec python3 -m app.main
