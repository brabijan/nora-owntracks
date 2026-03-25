#!/usr/bin/env python3
"""OwnTracks location tracker — polls API, stores points, detects stays."""

import argparse
import datetime as dt
import json
import math
import os
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from base64 import b64encode

DB_PATH = os.path.expanduser("~/.openclaw/data/owntracks.db")
ENV_PATH_DEFAULT = os.path.expanduser("~/.config/openclaw/owntracks.env")

# Stay detection parameters
STAY_RADIUS_M = 200       # points within this distance belong to same stay
STAY_MIN_DURATION_S = 600  # minimum 10 min to count as a stay
STAY_MAX_ACC_M = 300      # stays where median accuracy exceeds this are auto-ignored


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_env(path):
    vals = {}
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing env file: {path}")
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def ensure_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS points (
            id INTEGER PRIMARY KEY,
            ts INTEGER NOT NULL UNIQUE,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            acc REAL,
            vel REAL,
            batt REAL,
            conn TEXT,
            raw_json TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (unixepoch())
        );

        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            radius_m REAL NOT NULL DEFAULT 150,
            purpose TEXT,
            group_name TEXT,
            address TEXT
        );

        CREATE TABLE IF NOT EXISTS ignored_locations (
            id INTEGER PRIMARY KEY,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            radius_m REAL NOT NULL DEFAULT 300,
            permanent INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT (unixepoch())
        );

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()

    # Migrations for existing databases
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ignored_locations)").fetchall()}
    if "permanent" not in cols:
        conn.execute("ALTER TABLE ignored_locations ADD COLUMN permanent INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    place_cols = {r[1] for r in conn.execute("PRAGMA table_info(places)").fetchall()}
    if "group_name" not in place_cols:
        conn.execute("ALTER TABLE places ADD COLUMN group_name TEXT")
        conn.commit()
    if "address" not in place_cols:
        conn.execute("ALTER TABLE places ADD COLUMN address TEXT")
        conn.commit()


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_last(url, user, password):
    auth = b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8")
    obj = json.loads(raw)
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    return obj


# ---------------------------------------------------------------------------
# Reverse geocoding
# ---------------------------------------------------------------------------

def reverse_geocode(lat, lon):
    """Get address from Nominatim. Returns string or empty on failure."""
    q = urllib.parse.urlencode({
        "format": "jsonv2", "lat": f"{lat}", "lon": f"{lon}",
        "zoom": "18", "addressdetails": "1",
    })
    url = f"https://nominatim.openstreetmap.org/reverse?{q}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "nora-owntracks/1.0 (local assistant)"
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            obj = json.loads(r.read().decode("utf-8"))
        addr = obj.get("address", {})
        parts = []
        # street/village + house number (Czech style: "Sviny 117", "Pivovarská 226/14")
        num = addr.get("house_number")
        road = addr.get("road") or addr.get("hamlet") or ""
        village = addr.get("village") or ""
        if road and num:
            parts.append(f"{road} {num}")
        elif village and num:
            parts.append(f"{village} {num}")
        elif road:
            parts.append(road)
        elif village:
            parts.append(village)
        # city/town — prefer higher level; skip if same as village already used
        raw_city = addr.get("city") or addr.get("town") or addr.get("municipality") or ""
        # strip Czech admin prefixes like "SO POÚ ", "SO ORP "
        city = raw_city
        for prefix in ("SO POÚ ", "SO ORP "):
            if city.startswith(prefix):
                city = city[len(prefix):]
        if not city and not road and not village:
            city = addr.get("village") or ""
        if city and city not in parts[0] if parts else True:
            parts.append(city)
        # country
        country = addr.get("country") or ""
        if country:
            parts.append(country)
        return ", ".join(parts) if parts else obj.get("display_name", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Stay detection — computed from points, not a state machine
# ---------------------------------------------------------------------------

def detect_stays(conn, since_ts=0):
    """Scan points and return list of stays.

    A stay is a consecutive run of points whose centroid stays within
    STAY_RADIUS_M.  Returns list of dicts:
        {lat, lon, start_ts, end_ts, is_current, point_count}
    """
    rows = conn.execute(
        "SELECT ts, lat, lon FROM points WHERE ts >= ? ORDER BY ts",
        (since_ts,),
    ).fetchall()

    if not rows:
        return []

    stays = []
    cluster_pts = [rows[0]]
    cluster_lat = rows[0][1]
    cluster_lon = rows[0][2]

    for ts, lat, lon in rows[1:]:
        dist = haversine_m(cluster_lat, cluster_lon, lat, lon)
        if dist <= STAY_RADIUS_M:
            cluster_pts.append((ts, lat, lon))
            # update centroid as running average
            n = len(cluster_pts)
            cluster_lat = cluster_lat + (lat - cluster_lat) / n
            cluster_lon = cluster_lon + (lon - cluster_lon) / n
        else:
            # close current cluster — end_ts is when the NEXT stay starts
            if len(cluster_pts) >= 1:
                start_ts = cluster_pts[0][0]
                stays.append({
                    "lat": cluster_lat,
                    "lon": cluster_lon,
                    "start_ts": start_ts,
                    "end_ts": ts,  # next point = departure
                    "is_current": False,
                    "point_count": len(cluster_pts),
                })
            # start new cluster
            cluster_pts = [(ts, lat, lon)]
            cluster_lat = lat
            cluster_lon = lon

    # last cluster is the current stay
    if cluster_pts:
        start_ts = cluster_pts[0][0]
        end_ts = cluster_pts[-1][0]
        stays.append({
            "lat": cluster_lat,
            "lon": cluster_lon,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "is_current": True,
            "point_count": len(cluster_pts),
        })

    return stays


def is_ignored(conn, lat, lon, stay_start_ts=None):
    """Check if location is near any ignored location.

    Permanent entries always match.  Non-permanent entries only match if
    stay_start_ts <= created_at (so future visits resurface).
    """
    rows = conn.execute(
        "SELECT lat, lon, radius_m, permanent, created_at FROM ignored_locations"
    ).fetchall()
    for ilat, ilon, radius, permanent, created_at in rows:
        if haversine_m(lat, lon, ilat, ilon) <= radius:
            if permanent:
                return True
            if stay_start_ts is None or stay_start_ts <= created_at:
                return True
    return False


def resolve_place(conn, lat, lon):
    """Find the closest matching place within its radius.

    Returns (id, name, radius_m, group_name) or None.
    """
    rows = conn.execute("SELECT id, name, lat, lon, radius_m, group_name FROM places").fetchall()
    best = None
    for pid, name, plat, plon, radius, group_name in rows:
        d = haversine_m(lat, lon, plat, plon)
        if d <= radius and (best is None or d < best[0]):
            best = (d, pid, name, radius, group_name)
    return (best[1], best[2], best[3], best[4]) if best else None


def get_state(conn):
    """Get all state key-value pairs as a dict."""
    rows = conn.execute("SELECT key, value FROM state").fetchall()
    return {k: v for k, v in rows}


def set_state(conn, **kwargs):
    """Set state values (upsert)."""
    for k, v in kwargs.items():
        conn.execute(
            "INSERT INTO state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, str(v) if v is not None else None),
        )


def run_hook(hook_path, event, place_name=None, lat=None, lon=None, duration_s=None):
    """Run a hook script with event info passed as environment variables."""
    if not hook_path:
        return
    if not os.path.isfile(hook_path):
        print(f"Hook not found: {hook_path}", file=sys.stderr)
        return
    env = os.environ.copy()
    env["HOOK_EVENT"] = event
    if place_name is not None:
        env["HOOK_PLACE"] = str(place_name)
    if lat is not None:
        env["HOOK_LAT"] = f"{lat:.6f}"
    if lon is not None:
        env["HOOK_LON"] = f"{lon:.6f}"
    if duration_s is not None:
        env["HOOK_DURATION_S"] = str(int(duration_s))
    try:
        subprocess.run([hook_path], env=env, timeout=30, check=False)
    except Exception as e:
        print(f"Hook error ({event}): {e}", file=sys.stderr)


def detect_transition(conn, env):
    """Compare current stay with stored state, fire hooks on transitions.

    Returns the updated state dict.
    """
    arrive_hook = env.get("ARRIVE_HOOK", "")
    leave_hook = env.get("LEAVE_HOOK", "")
    new_unknown_hook = env.get("NEW_UNKNOWN_HOOK", "")

    if not any([arrive_hook, leave_hook, new_unknown_hook]):
        return

    since = int(dt.datetime.now().timestamp()) - 24 * 86400
    stays = detect_stays(conn, since)
    if not stays:
        return

    current = enrich_stay(conn, stays[-1])
    cur_place_id = current["place_id"]
    cur_group = current["group_name"]
    cur_lat = current["lat"]
    cur_lon = current["lon"]
    cur_place_name = current["place_name"]

    # Don't fire hooks for unnamed stays shorter than STAY_MIN_DURATION_S.
    # Named places trigger immediately (we know where we are).
    if cur_place_id is None and current["duration_s"] < STAY_MIN_DURATION_S:
        return

    state = get_state(conn)
    prev_place_id = state.get("place_id")  # str(int) or None
    prev_group = state.get("group_name") or None
    prev_lat = float(state.get("lat") or 0)
    prev_lon = float(state.get("lon") or 0)
    prev_place_name = state.get("place_name")
    prev_duration = state.get("duration_s")

    first_run = "place_id" not in state and "lat" not in state

    if first_run:
        # First run — just store state, don't fire hooks
        set_state(conn, place_id=cur_place_id or "", place_name=cur_place_name or "",
                  group_name=cur_group or "",
                  lat=cur_lat, lon=cur_lon, duration_s=current.get("duration_s", 0))
        conn.commit()
        return

    # Determine if location changed
    same_group = (cur_group and prev_group and cur_group == prev_group)
    same_known_place = (cur_place_id is not None and prev_place_id
                        and str(cur_place_id) == str(prev_place_id))
    same_unknown = (cur_place_id is None and not prev_place_id
                    and prev_lat and prev_lon
                    and haversine_m(cur_lat, cur_lon, prev_lat, prev_lon) <= STAY_RADIUS_M)

    if same_group or same_known_place or same_unknown:
        # Still at the same location — just update duration
        set_state(conn, duration_s=current.get("duration_s", 0))
        conn.commit()
        return

    # --- Location changed ---
    prev_label = prev_place_name or f"{prev_lat:.4f},{prev_lon:.4f}"
    cur_label = cur_place_name or f"{cur_lat:.4f},{cur_lon:.4f}"
    print(f"TRANSITION {prev_label} -> {cur_label}")

    # Leave previous location
    if prev_lat and prev_lon:
        leave_name = prev_place_name if prev_place_name else None
        run_hook(leave_hook, "leave", place_name=leave_name,
                 lat=prev_lat, lon=prev_lon,
                 duration_s=int(prev_duration) if prev_duration else None)

    # Arrive at new location
    if cur_place_id is not None:
        print(f"HOOK arrive {cur_place_name}")
        run_hook(arrive_hook, "arrive", place_name=cur_place_name,
                 lat=cur_lat, lon=cur_lon)
    else:
        if not is_ignored(conn, cur_lat, cur_lon, current["start_ts"]):
            print(f"HOOK new_unknown {cur_lat:.4f},{cur_lon:.4f}")
            run_hook(new_unknown_hook, "new_unknown",
                     lat=cur_lat, lon=cur_lon)

    # Update state
    set_state(conn, place_id=cur_place_id or "", place_name=cur_place_name or "",
              group_name=cur_group or "",
              lat=cur_lat, lon=cur_lon, duration_s=current.get("duration_s", 0))
    conn.commit()


def enrich_stay(conn, stay):
    """Add place info and duration to a stay dict."""
    place = resolve_place(conn, stay["lat"], stay["lon"])
    now_ts = int(dt.datetime.now().timestamp())
    if stay["is_current"]:
        duration_s = now_ts - stay["start_ts"]
    else:
        duration_s = stay["end_ts"] - stay["start_ts"]
    stay["duration_s"] = duration_s
    stay["place_id"] = place[0] if place else None
    stay["place_name"] = place[1] if place else None
    stay["group_name"] = place[3] if place else None
    return stay


def merge_grouped_stays(stays):
    """Merge consecutive stays that belong to the same group into one.

    The merged stay keeps the first stay's start_ts, the last stay's end_ts
    and is_current, sums point_count, and uses the group_name as place_name.
    """
    if not stays:
        return stays
    merged = [stays[0].copy()]
    for s in stays[1:]:
        prev = merged[-1]
        prev_key = prev["group_name"] or prev["place_id"]
        cur_key = s["group_name"] or s["place_id"]
        if prev_key and cur_key and prev_key == cur_key:
            prev["end_ts"] = s["end_ts"]
            prev["is_current"] = s["is_current"]
            prev["point_count"] += s["point_count"]
            now_ts = int(dt.datetime.now().timestamp())
            if prev["is_current"]:
                prev["duration_s"] = now_ts - prev["start_ts"]
            else:
                prev["duration_s"] = prev["end_ts"] - prev["start_ts"]
        else:
            merged.append(s.copy())
    # Use group_name as display name where applicable
    for s in merged:
        if s["group_name"]:
            s["place_name"] = s["group_name"]
    return merged


def fmt_ts(ts):
    return dt.datetime.fromtimestamp(int(ts)).astimezone().strftime("%H:%M")


def fmt_date_ts(ts):
    return dt.datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M")


def fmt_duration(seconds):
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m:02d}m" if m else f"{h}h"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_poll(env_path, db_path):
    """Fetch latest point from OwnTracks API and store it."""
    env = parse_env(env_path)
    url, user, password = env["OWNTRACKS_URL"], env["OWNTRACKS_USER"], env["OWNTRACKS_PASS"]

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    ensure_db(conn)

    obj = fetch_last(url, user, password)
    ts = int(obj.get("tst") or dt.datetime.now().timestamp())
    lat, lon = float(obj["lat"]), float(obj["lon"])

    conn.execute(
        "INSERT OR IGNORE INTO points(ts,lat,lon,acc,vel,batt,conn,raw_json) VALUES(?,?,?,?,?,?,?,?)",
        (ts, lat, lon, obj.get("acc"), obj.get("vel"), obj.get("batt"),
         obj.get("conn"), json.dumps(obj, ensure_ascii=False)),
    )
    conn.commit()

    place = resolve_place(conn, lat, lon)
    place_name = place[1] if place else "unknown"
    print(f"OK ts={ts} place={place_name} lat={lat} lon={lon}")

    detect_transition(conn, env)
    conn.close()


def cmd_now(db_path):
    """Show current stay — where am I and since when."""
    conn = sqlite3.connect(db_path)
    ensure_db(conn)

    # look back 7 days for stay detection
    since = int(dt.datetime.now().timestamp()) - 7 * 86400
    stays = detect_stays(conn, since)
    if not stays:
        print("No data.")
        return

    stays = [enrich_stay(conn, s) for s in stays]
    stays = merge_grouped_stays(stays)
    current = stays[-1]
    name = current["place_name"] or "unnamed"
    start = fmt_ts(current["start_ts"])
    dur = fmt_duration(current["duration_s"])

    print(f"{name}, od {start}, {dur} (current)")
    print(f"  {current['lat']:.6f}, {current['lon']:.6f}")
    print(f"  https://maps.google.com/?q={current['lat']},{current['lon']}")

    # also show last point info
    row = conn.execute(
        "SELECT ts, batt, acc, vel, conn FROM points ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        ts, batt, acc, vel, cn = row
        print(f"  last update: {fmt_date_ts(ts)}, batt={batt}, acc={acc}, vel={vel}, conn={cn}")
    conn.close()


def cmd_stays(db_path, days=7):
    """List detected stays."""
    conn = sqlite3.connect(db_path)
    ensure_db(conn)

    since = int(dt.datetime.now().timestamp()) - days * 86400
    stays = detect_stays(conn, since)
    stays = [enrich_stay(conn, s) for s in stays]
    stays = merge_grouped_stays(stays)
    stays = [s for s in stays if s["duration_s"] >= STAY_MIN_DURATION_S or s["is_current"]]

    if not stays:
        print("No stays detected.")
        return

    prev_date = None
    for s in stays:
        date_str = dt.datetime.fromtimestamp(s["start_ts"]).astimezone().strftime("%Y-%m-%d")
        if date_str != prev_date:
            print(f"\n{date_str}")
            prev_date = date_str

        name = s["place_name"] or "???"
        start = fmt_ts(s["start_ts"])
        end = "now" if s["is_current"] else fmt_ts(s["end_ts"])
        dur = fmt_duration(s["duration_s"])
        current_tag = " (current)" if s["is_current"] else ""
        # Only reverse-geocode unnamed stays to avoid slow HTTP calls
        if not s["place_name"]:
            addr = reverse_geocode(s["lat"], s["lon"])
            addr_part = f"  ({addr})" if addr else ""
        else:
            addr_part = ""
        print(f"  {start}-{end}{current_tag}  {dur:>7}  {name}{addr_part}")

    conn.close()


def stay_median_acc(conn, stay):
    """Return median GPS accuracy (meters) for points within a stay's time window."""
    rows = conn.execute(
        "SELECT acc FROM points WHERE ts >= ? AND ts <= ? AND acc IS NOT NULL",
        (stay["start_ts"], stay["end_ts"]),
    ).fetchall()
    if not rows:
        return None
    vals = sorted(r[0] for r in rows)
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def get_unnamed_clusters(conn, days=14):
    """Return sorted list of unnamed location clusters."""
    since = int(dt.datetime.now().timestamp()) - days * 86400
    stays = detect_stays(conn, since)
    stays = [enrich_stay(conn, s) for s in stays]

    unnamed = [s for s in stays if s["place_id"] is None and s["duration_s"] >= STAY_MIN_DURATION_S
               and not is_ignored(conn, s["lat"], s["lon"], s["start_ts"])
               and (stay_median_acc(conn, s) or 0) <= STAY_MAX_ACC_M]

    clusters = []
    for s in unnamed:
        found = False
        for c in clusters:
            if haversine_m(c["lat"], c["lon"], s["lat"], s["lon"]) <= STAY_RADIUS_M:
                c["visits"].append(s)
                c["total_s"] += s["duration_s"]
                found = True
                break
        if not found:
            clusters.append({
                "lat": s["lat"],
                "lon": s["lon"],
                "visits": [s],
                "total_s": s["duration_s"],
            })

    clusters.sort(key=lambda c: c["total_s"], reverse=True)
    return clusters


def cmd_unnamed(db_path, days=14):
    """List stays at unnamed locations — candidates for naming."""
    conn = sqlite3.connect(db_path)
    ensure_db(conn)

    clusters = get_unnamed_clusters(conn, days)
    if not clusters:
        print("No unnamed stays.")
        conn.close()
        return

    print(f"Unnamed locations ({days}d):\n")
    for i, c in enumerate(clusters, 1):
        total = fmt_duration(c["total_s"])
        count = len(c["visits"])
        addr = reverse_geocode(c["lat"], c["lon"])
        print(f"  {i}. {c['lat']:.6f}, {c['lon']:.6f}  —  {count}x, total {total}")
        if addr:
            print(f"     {addr}")
        print(f"     https://maps.google.com/?q={c['lat']},{c['lon']}")
        last = max(c["visits"], key=lambda v: v["start_ts"])
        print(f"     last: {fmt_date_ts(last['start_ts'])}")

    conn.close()


def cmd_ignore_unknown(db_path, index, days=14, permanent=False):
    """Ignore an unnamed location by its number from the unnamed list."""
    conn = sqlite3.connect(db_path)
    ensure_db(conn)

    clusters = get_unnamed_clusters(conn, days)
    if index < 1 or index > len(clusters):
        print(f"Invalid index {index}. Run 'unnamed' to see the list (1-{len(clusters)}).")
        conn.close()
        return

    c = clusters[index - 1]
    conn.execute(
        "INSERT INTO ignored_locations(lat, lon, radius_m, permanent) VALUES(?,?,?,?)",
        (c["lat"], c["lon"], STAY_RADIUS_M, 1 if permanent else 0),
    )
    conn.commit()
    addr = reverse_geocode(c["lat"], c["lon"])
    loc = addr if addr else f"{c['lat']:.6f}, {c['lon']:.6f}"
    label = "Permanently ignored" if permanent else "Ignored (current visits)"
    print(f"{label}: {loc}")
    conn.close()


def cmd_add_place(db_path, name, lat, lon, radius_m, purpose, group_name=None):
    conn = sqlite3.connect(db_path)
    ensure_db(conn)
    address = reverse_geocode(lat, lon)
    conn.execute(
        "INSERT INTO places(name,lat,lon,radius_m,purpose,group_name,address) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, "
        "radius_m=excluded.radius_m, purpose=excluded.purpose, "
        "group_name=excluded.group_name, address=excluded.address",
        (name, lat, lon, radius_m, purpose, group_name, address),
    )
    conn.commit()
    group_part = f" group={group_name}" if group_name else ""
    addr_part = f" ({address})" if address else ""
    print(f"Saved place: {name} @ {lat},{lon} r={radius_m}m{group_part}{addr_part}")
    conn.close()


def cmd_places(db_path):
    conn = sqlite3.connect(db_path)
    ensure_db(conn)
    rows = conn.execute(
        "SELECT id, name, lat, lon, radius_m, purpose, group_name, address FROM places ORDER BY group_name, name"
    ).fetchall()
    if not rows:
        print("No places.")
        return
    for pid, name, lat, lon, radius_m, purpose, group_name, address in rows:
        group_part = f"  [{group_name}]" if group_name else ""
        addr_part = f"  {address}" if address else ""
        print(f"  #{pid} {name} @ {lat:.6f},{lon:.6f} r={radius_m}m{group_part}  {purpose or ''}{addr_part}")
    conn.close()


def cmd_dump(db_path, days=14):
    """JSON dump for external consumption."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_db(conn)

    since = int(dt.datetime.now().timestamp()) - days * 86400

    out = {"places": [], "stays": [], "last_point": None}

    lp = conn.execute("SELECT * FROM points ORDER BY ts DESC LIMIT 1").fetchone()
    if lp:
        out["last_point"] = dict(lp)

    out["places"] = [dict(r) for r in conn.execute("SELECT * FROM places ORDER BY name")]

    conn.row_factory = None
    stays = detect_stays(conn, since)
    stays = [enrich_stay(conn, s) for s in stays]
    out["stays"] = stays

    print(json.dumps(out, ensure_ascii=False, indent=2))
    conn.close()


def cmd_import_jsonl(db_path, jsonl_path):
    """Import points from a JSONL file (one OwnTracks JSON per line)."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    ensure_db(conn)

    count = 0
    for line in open(jsonl_path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        ts = int(obj.get("tst", 0))
        if not ts:
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO points(ts,lat,lon,acc,vel,batt,conn,raw_json) VALUES(?,?,?,?,?,?,?,?)",
                (ts, float(obj["lat"]), float(obj["lon"]),
                 obj.get("acc"), obj.get("vel"), obj.get("batt"),
                 obj.get("conn"), line),
            )
            count += 1
        except (KeyError, ValueError):
            continue

    conn.commit()
    print(f"Imported {count} points from {jsonl_path}")
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="OwnTracks location tracker")
    ap.add_argument("--db", default=DB_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_poll = sub.add_parser("poll", help="Fetch latest point from API")
    p_poll.add_argument("--env", default=ENV_PATH_DEFAULT)

    sub.add_parser("now", help="Current stay")

    p_stays = sub.add_parser("stays", help="List stays")
    p_stays.add_argument("--days", type=int, default=7)

    p_unnamed = sub.add_parser("unnamed", help="Unnamed locations")
    p_unnamed.add_argument("--days", type=int, default=14)

    sub.add_parser("places", help="List named places")

    p_add = sub.add_parser("add-place", help="Add/update a named place")
    p_add.add_argument("name")
    p_add.add_argument("lat", type=float)
    p_add.add_argument("lon", type=float)
    p_add.add_argument("--radius", type=float, default=150)
    p_add.add_argument("--purpose", default="")
    p_add.add_argument("--group", default=None,
                       help="Group name — places in the same group are treated as one location")

    p_ignore = sub.add_parser("ignore-unknown", help="Ignore an unnamed location by index")
    p_ignore.add_argument("index", type=int)
    p_ignore.add_argument("--days", type=int, default=14)
    p_ignore.add_argument("--permanent", action="store_true",
                          help="Block this location permanently (never show in unnamed)")

    p_dump = sub.add_parser("dump", help="JSON dump")
    p_dump.add_argument("--days", type=int, default=14)

    p_import = sub.add_parser("import", help="Import from JSONL file")
    p_import.add_argument("file")

    args = ap.parse_args()

    if args.cmd == "poll":
        cmd_poll(args.env, args.db)
    elif args.cmd == "now":
        cmd_now(args.db)
    elif args.cmd == "stays":
        cmd_stays(args.db, args.days)
    elif args.cmd == "unnamed":
        cmd_unnamed(args.db, args.days)
    elif args.cmd == "places":
        cmd_places(args.db)
    elif args.cmd == "add-place":
        cmd_add_place(args.db, args.name, args.lat, args.lon, args.radius, args.purpose, args.group)
    elif args.cmd == "ignore-unknown":
        cmd_ignore_unknown(args.db, args.index, args.days, args.permanent)
    elif args.cmd == "dump":
        cmd_dump(args.db, args.days)
    elif args.cmd == "import":
        cmd_import_jsonl(args.db, args.file)


if __name__ == "__main__":
    main()
