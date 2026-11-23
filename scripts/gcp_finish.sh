#!/usr/bin/env bash
# Upload a finished GCP batch run to GCS, then delete the VM.
#
# Called from the VM by scripts/gcp_batch.py. The VM's default compute service
# account has the project Editor role, so it can create the GCS object and
# delete itself. If the upload fails after retries, the VM stops itself instead
# of leaving compute billing running.
set -u

BUCKET=${1:?bucket}
PREFIX=${2:?prefix}
PROJECT=${3:?project}
ZONE=${4:?zone}
INSTANCE=${5:?instance}
RUN_DIR=${6:?run directory}
RC=${7:-1}

mkdir -p "$RUN_DIR"
LOG="$RUN_DIR/finish.log"
exec >>"$LOG" 2>&1
echo "=== finish $(date -u +%Y-%m-%dT%H:%M:%SZ) rc=$RC ==="

python3 - "$RUN_DIR" "$RC" "$BUCKET" "$PREFIX" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

run_dir, rc, bucket, prefix = sys.argv[1:5]
payload = {
    'exit_code': int(rc),
    'finished_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    'bucket': bucket,
    'prefix': prefix,
}
Path(run_dir, 'finish.json').write_text(json.dumps(payload, indent=2) + '\n')
PY

TARBALL=/tmp/adv-results.tar.gz
tar -czf "$TARBALL" -C "$RUN_DIR" .

TOKEN=$(curl -fsS -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
  | jq -r .access_token)
if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo 'metadata token unavailable; leaving VM running for manual recovery'
  exit 2
fi

uploaded=0
for attempt in 1 2 3 4 5; do
  echo "upload attempt $attempt"
  if curl -fsS --max-time 300 -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/gzip' \
      --data-binary "@$TARBALL" \
      "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&name=$PREFIX/results.tar.gz"; then
    uploaded=1
    break
  fi
  sleep $((attempt * 10))
done

if [ "$uploaded" -eq 1 ]; then
  echo "uploaded gs://$BUCKET/$PREFIX/results.tar.gz"
  echo "deleting VM $INSTANCE"
  curl -fsS --max-time 30 -X DELETE \
    -H "Authorization: Bearer $TOKEN" \
    "https://compute.googleapis.com/compute/v1/projects/$PROJECT/zones/$ZONE/instances/$INSTANCE" || true
else
  echo 'upload failed; stopping VM to stop compute billing'
  echo 'results remain on the boot disk; use `gcp_batch.py start` to recover'
  curl -fsS --max-time 30 -X POST \
    -H "Authorization: Bearer $TOKEN" \
    "https://compute.googleapis.com/compute/v1/projects/$PROJECT/zones/$ZONE/instances/$INSTANCE/stop" || true
fi
