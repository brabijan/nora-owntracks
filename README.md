# OwnTracks SQLite Tracker

Polls OwnTracks API, stores location points in SQLite, detects stays, and fires hooks on location transitions.

## Setup

Create env file at `~/.config/openclaw/owntracks.env`:

```
OWNTRACKS_URL=https://your-owntracks-server/api/0/last
OWNTRACKS_USER=user
OWNTRACKS_PASS=secret
```

DB is stored at `~/.openclaw/data/owntracks.db` by default. Both paths can be overridden with `--env` and `--db` flags.

## Commands

```
python3 owntracks-sqlite.py poll          # Fetch latest point from API
python3 owntracks-sqlite.py now           # Show current stay
python3 owntracks-sqlite.py stays [--days N]    # List detected stays (default 7 days)
python3 owntracks-sqlite.py unnamed [--days N]  # List unnamed locations (default 14 days)
python3 owntracks-sqlite.py places        # List named places
python3 owntracks-sqlite.py add-place NAME LAT LON [--radius M] [--purpose TEXT]
python3 owntracks-sqlite.py ignore-unknown INDEX [--days N]
python3 owntracks-sqlite.py dump [--days N]     # JSON dump
python3 owntracks-sqlite.py import FILE         # Import from JSONL
```

## Hooks

Hooks are optional scripts that run automatically when location transitions are detected during `poll`.

Add to your env file:

```
ARRIVE_HOOK=/path/to/arrive.sh
LEAVE_HOOK=/path/to/leave.sh
NEW_UNKNOWN_HOOK=/path/to/new_unknown.sh
```

### When hooks fire

| Hook | Trigger |
|------|---------|
| `ARRIVE_HOOK` | Arrival at a **known** place |
| `LEAVE_HOOK` | Departure from **any** place (known or unknown) |
| `NEW_UNKNOWN_HOOK` | Arrival at a **new unknown** location (ignored locations are skipped) |

### Environment variables passed to hooks

| Variable | Description | Events |
|----------|-------------|--------|
| `HOOK_EVENT` | `arrive`, `leave`, or `new_unknown` | all |
| `HOOK_PLACE` | Place name | `arrive`, `leave` (known places only) |
| `HOOK_LAT` | Latitude | all |
| `HOOK_LON` | Longitude | all |
| `HOOK_DURATION_S` | Duration of stay in seconds | `leave` |

### Example hook

```bash
#!/bin/bash
echo "$(date): $HOOK_EVENT at ${HOOK_PLACE:-unknown} ($HOOK_LAT, $HOOK_LON)" >> ~/owntracks-hooks.log
```

## Stay detection

Points within 200m of a cluster centroid are grouped into a stay. Stays shorter than 10 minutes are filtered out from listings (but not from hook detection).
