#!/usr/bin/env bash
# Restore Flow Deployments / Upload state onto the in-cluster PVC from a
# flow-deploy-results-*.csv export (and optionally a flow-schedules-*.csv).
#
# Use after a PVC attach, empty volume, or wiped /app/data — normal rollouts
# already persist via last_*.json once the PVC is mounted.
#
# Examples:
#   ./scripts/restore-flow-state.sh ~/Downloads/flow-deploy-results-….csv
#   ./scripts/restore-flow-state.sh results.csv --schedules schedules.csv
#   ./scripts/restore-flow-state.sh results.csv --dry-run
#   FLOW_NS=rhdp-flow ./scripts/restore-flow-state.sh results.csv --lookup-assets
#
# Env:
#   FLOW_NS       Flow namespace (default: rhdp-flow)
#   FLOW_CONTEXT  oc/kubectl context (optional)
#   OC            oc binary (default: oc)

set -euo pipefail

FLOW_NS="${FLOW_NS:-rhdp-flow}"
OC="${OC:-oc}"
CONTAINER="${FLOW_CONTAINER:-rhdp-scheduler}"
DATA_DIR_IN_POD="${FLOW_DATA_DIR:-/app/data}"

RESULTS_CSV=""
SCHEDULES_CSV=""
DRY_RUN=0
NO_RESTART=0
LOOKUP_ASSETS=0
OUT_DIR=""
FLOW_CONTEXT="${FLOW_CONTEXT:-}"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --schedules) SCHEDULES_CSV="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --lookup-assets) LOOKUP_ASSETS=1; shift ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --namespace|-n) FLOW_NS="$2"; shift 2 ;;
    --context) FLOW_CONTEXT="$2"; shift 2 ;;
    -*) echo "Unknown flag: $1" >&2; usage 1 ;;
    *)
      if [[ -z "$RESULTS_CSV" ]]; then
        RESULTS_CSV="$1"
      else
        echo "Unexpected arg: $1" >&2
        usage 1
      fi
      shift
      ;;
  esac
done

[[ -n "$RESULTS_CSV" ]] || { echo "Need a flow-deploy-results CSV path." >&2; usage 1; }
[[ -f "$RESULTS_CSV" ]] || { echo "Not found: $RESULTS_CSV" >&2; exit 1; }
if [[ -n "$SCHEDULES_CSV" && ! -f "$SCHEDULES_CSV" ]]; then
  echo "Not found: $SCHEDULES_CSV" >&2
  exit 1
fi

OC_ARGS=()
[[ -n "$FLOW_CONTEXT" ]] && OC_ARGS+=(--context "$FLOW_CONTEXT")

OUT_DIR="${OUT_DIR:-$(mktemp -d /tmp/flow-restore.XXXXXX)}"
mkdir -p "$OUT_DIR"

# Optional: fill asset_cis for multi-workshop GUIDs from live MultiWorkshops
ASSETS_JSON="{}"
if [[ "$LOOKUP_ASSETS" -eq 1 ]]; then
  echo "==> Looking up MultiWorkshop asset lists on cluster…"
  ASSETS_JSON="$("$OC" "${OC_ARGS[@]}" get multiworkshop -A -o json 2>/dev/null | python3 -c '
import json, sys
doc = json.load(sys.stdin)
out = {}
for item in doc.get("items") or []:
    name = (item.get("metadata") or {}).get("name") or ""
    assets = (item.get("spec") or {}).get("assets") or []
    cis = []
    for a in assets:
        ci = a.get("catalogItem") or a.get("name") or ""
        ns = a.get("catalogItemNamespace") or a.get("namespace") or ""
        if ci and ns and "." not in ci:
            cis.append(f"{ns}.{ci}")
        elif ci:
            cis.append(ci)
    if name and cis:
        out[name] = ",".join(cis)
print(json.dumps(out))
' || echo '{}')"
fi

python3 - "$RESULTS_CSV" "${SCHEDULES_CSV:-}" "$OUT_DIR" "$ASSETS_JSON" <<'PY'
import csv, json, sys
from pathlib import Path

results_csv, schedules_csv, out_dir, assets_json = sys.argv[1:5]
out = Path(out_dir)
assets = json.loads(assets_json or "{}")

def parse_int(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None

def parse_bool(v, default=True):
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

results = []
with open(results_csv, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        results.append({
            "ci_name": row.get("ci_name") or "",
            "ci": row.get("ci") or "",
            "namespace": row.get("namespace") or "",
            "guid": row.get("guid") or "",
            "url": row.get("url") or "",
            "status": row.get("status") or "deployed_unverified",
            "provisioning_date": row.get("provisioning_date") or "",
            "auto_stop": row.get("auto_stop") or "",
            "auto_destroy": row.get("auto_destroy") or "",
            "timestamp": row.get("timestamp") or "",
            "error_message": row.get("error_message") or "",
            "showroom_url": row.get("showroom_url") or "",
            "showroom_status": row.get("showroom_status") or "",
            "password": row.get("password") or "",
            "cluster_name": row.get("cluster_name") or "",
            "cluster_capacity": row.get("cluster_capacity") or "",
            "users": parse_int(row.get("users")),
            "instances": parse_int(row.get("instances")),
        })

schedules = []
if schedules_csv:
    with open(schedules_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            schedules.append({
                "ci_name": row.get("ci_name") or "",
                "ci": row.get("ci") or "",
                "namespace": row.get("namespace") or "",
                "users": parse_int(row.get("users")),
                "enable_workshop_interface": parse_bool(row.get("enable_workshop_interface"), True),
                "password": row.get("password") or "",
                "activity": row.get("activity") or "Workshops",
                "purpose": row.get("purpose") or "QA",
                "workshop_name": row.get("workshop_name") or row.get("ci_name") or "",
                "provisioning_date": row.get("provisioning_date") or "",
                "auto_stop": row.get("auto_stop") or "",
                "auto_destroy": row.get("auto_destroy") or "",
                "is_multi_asset": parse_bool(row.get("is_multi_asset"), False),
                "asset_cis": row.get("asset_cis") or "",
                "multi_workshop_name": row.get("multi_workshop_name") or "",
                "concurrency": parse_int(row.get("concurrency")) or 10,
                "instances": parse_int(row.get("instances")),
                "salesforce_ids": row.get("salesforce_ids") or "",
                "salesforce_type": row.get("salesforce_type") or "opportunity",
                "aws_regions": row.get("aws_regions") or "",
                "count": parse_int(row.get("count")),
                "white_glove": parse_bool(row.get("white_glove"), True),
                "redirect": parse_bool(row.get("redirect"), True),
                "catalog_namespace": row.get("catalog_namespace") or "",
                "showroom_repo": row.get("showroom_repo") or "",
                "showroom_ref": row.get("showroom_ref") or "",
                "showroom_novnc": parse_bool(row.get("showroom_novnc"), False),
                "showroom_zerotouch": parse_bool(row.get("showroom_zerotouch"), False),
                "item_type": row.get("item_type") or None,
                "cluster_ci_override": row.get("cluster_ci_override") or None,
            })
else:
    # Synthesize schedules from results (Deployments + Retry usability)
    seen = set()
    for r in results:
        key = (r["ci"], r["namespace"], r["provisioning_date"], r["guid"])
        if key in seen:
            continue
        seen.add(key)
        guid = r["guid"]
        is_multi = guid in assets or "/multi-workshop/" in (r.get("url") or "")
        schedules.append({
            "ci_name": r["ci_name"],
            "ci": r["ci"],
            "namespace": r["namespace"],
            "users": r["users"],
            "enable_workshop_interface": True,
            "password": r.get("password") or "",
            "activity": "Workshops",
            "purpose": "QA",
            "workshop_name": r["ci_name"],
            "provisioning_date": r["provisioning_date"],
            "auto_stop": r["auto_stop"],
            "auto_destroy": r["auto_destroy"],
            "is_multi_asset": is_multi,
            "asset_cis": assets.get(guid, ""),
            "multi_workshop_name": guid if is_multi else "",
            "concurrency": 10,
            "instances": r["instances"],
            "salesforce_ids": "",
            "salesforce_type": "opportunity",
            "aws_regions": "",
            "count": None,
            "white_glove": True,
            "redirect": True,
            "catalog_namespace": "",
            "showroom_repo": "",
            "showroom_ref": "",
            "showroom_novnc": False,
            "showroom_zerotouch": False,
            "item_type": None,
            "cluster_ci_override": None,
        })

meta = {"filename": f"restored-from-{Path(results_csv).name}"}
(out / "last_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
(out / "last_schedules.json").write_text(json.dumps(schedules, indent=2), encoding="utf-8")
(out / "last_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
print(f"Wrote {len(results)} results, {len(schedules)} schedules → {out}")
multi = sum(1 for s in schedules if s.get("is_multi_asset"))
if multi:
    print(f"  multi-asset schedules: {multi}")
PY

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> Dry-run only. JSON left in $OUT_DIR"
  exit 0
fi

echo "==> Finding Flow pod in $FLOW_NS…"
POD="$("$OC" "${OC_ARGS[@]}" get pods -n "$FLOW_NS" \
  -l app.kubernetes.io/name=rhdp-scheduler \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$POD" ]] || { echo "No running rhdp-scheduler pod in $FLOW_NS" >&2; exit 1; }
echo "    pod=$POD"

echo "==> Copying state onto PVC ($DATA_DIR_IN_POD)…"
for f in last_results.json last_schedules.json last_meta.json; do
  "$OC" "${OC_ARGS[@]}" cp "$OUT_DIR/$f" -n "$FLOW_NS" "$POD:$DATA_DIR_IN_POD/$f" -c "$CONTAINER"
done

if [[ "$NO_RESTART" -eq 1 ]]; then
  echo "==> Files copied; skipped restart (--no-restart). Delete the pod to reload."
  exit 0
fi

echo "==> Restarting pod so Flow reloads persisted state…"
"$OC" "${OC_ARGS[@]}" delete pod -n "$FLOW_NS" "$POD" --wait=false
"$OC" "${OC_ARGS[@]}" rollout status deploy/rhdp-scheduler -n "$FLOW_NS" --timeout=180s

POD2="$("$OC" "${OC_ARGS[@]}" get pods -n "$FLOW_NS" \
  -l app.kubernetes.io/name=rhdp-scheduler \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}')"

echo "==> Verifying API…"
"$OC" "${OC_ARGS[@]}" exec -n "$FLOW_NS" "$POD2" -c "$CONTAINER" -- python3 -c '
import os, urllib.request, json
key = os.environ.get("RHDP_API_KEY", "")
for path in ("/api/deploy/results", "/api/schedules"):
    req = urllib.request.Request(
        "http://127.0.0.1:8000" + path,
        headers={"X-API-Key": key},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    print(path, "count", len(data) if isinstance(data, list) else data)
'

echo "Done. Refresh the Flow UI."
