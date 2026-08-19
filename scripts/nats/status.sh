#!/usr/bin/env bash
# Print the live state of the NATS core cluster + leafnodes by querying each
# core node's HTTP monitoring endpoint (port 8222, no auth required).
#
# Why HTTP and not `nats server list`: the core configs ship without an
# `accounts {}` block, so the default $G account is not the system account and
# `$SYS.REQ.*` requests get no responder. The monitoring endpoints work
# regardless. See scripts/nats/README.md.
#
# Usage: bash scripts/nats/status.sh
# Optional: CORE_IPS="10.13.11.7 10.13.11.1 10.13.11.177" PORT=8222 bash scripts/nats/status.sh
set -euo pipefail

CORE_IPS="${CORE_IPS:-10.13.11.7 10.13.11.1 10.13.11.177}"
PORT="${PORT:-8222}"

CORE_IPS="$CORE_IPS" PORT="$PORT" python3 <<'PY'
import json, os, urllib.request

IPS = os.environ["CORE_IPS"].split()
PORT = os.environ["PORT"]


def fetch(ip, ep):
    url = f"http://{ip}:{PORT}/{ep}"
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return None


for ip in IPS:
    print("=" * 60)
    print(f" {ip}  (core, :{PORT})")
    print("=" * 60)

    v = fetch(ip, "varz")
    if v is None:
        print("  DOWN (no response on :%s)" % PORT)
        print()
        continue

    js = (v.get("jetstream") or {}).get("config") or {}
    print("--- /varz ---")
    print(f"  server_name : {v.get('server_name')}")
    print(f"  version     : {v.get('version')}")
    print(f"  uptime      : {v.get('uptime')}")
    print(f"  cluster     : {(v.get('cluster') or {}).get('name')}")
    print(f"  jetstream   : store={js.get('store_dir')} max={js.get('max_file_store')}")

    r = fetch(ip, "routez") or {}
    routes = r.get("routes", [])
    print("--- /routez (cluster peers, active = msgs>0) ---")
    print(f"  num_routes  : {r.get('num_routes', len(routes))}")
    # NATS opens multiple redundant TCP links per peer; keep the one with the
    # highest message count per ip (the active link).
    best = {}
    for rt in routes:
        peer = rt.get("ip")
        if not peer:
            continue
        cur = best.get(peer)
        if cur is None or (rt.get("in_msgs", 0) + rt.get("out_msgs", 0)) > (cur.get("in_msgs", 0) + cur.get("out_msgs", 0)):
            best[peer] = rt
    for peer, rt in sorted(best.items()):
        active = "ACTIVE" if (rt.get("in_msgs", 0) + rt.get("out_msgs", 0)) > 0 else "idle"
        print(f"  {peer:<16} {active:<7} in={rt.get('in_msgs', 0)} out={rt.get('out_msgs', 0)} up={rt.get('uptime')}")

    l = fetch(ip, "leafz") or {}
    leafs = l.get("leafs", [])
    print("--- /leafz (leafnodes connected here) ---")
    print(f"  leaf_count  : {len(leafs)}")
    for lf in leafs:
        print(f"  {lf.get('ip','?'):<16} acct={lf.get('account','?')} in={lf.get('in_msgs',0)} out={lf.get('out_msgs',0)}")
    print()
PY
