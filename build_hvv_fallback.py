#!/usr/bin/env python3
"""
Build a tiny offline timetable from the official HVV GTFS feed, so both the
web board and the SwiftBar menu-bar plugin have a fallback when the realtime
API is down. Covers every area: Home, University, Berliner Tor.

Source feed (~40 MB ZIP, free under Datenlizenz Deutschland - Namensnennung 2.0,
attribution "Hamburger Verkehrsverbund GmbH"):
  https://suche.transparenz.hamburg.de/dataset/hvv-fahrplandaten-gtfs-april-2026-bis-dezember-2026
Download the ZIP from that page (the "Upload__hvv_Rohdaten_GTFS_Fpl_*.ZIP" resource),
then run:

  python build_hvv_fallback.py hvv_gtfs.zip

Output: hammerbrook_schedule.json (put it next to hammerbrook_board.html /
hammerbrook.30s.py -- both already know how to read this exact file/format).

The output is *scheduled* times only (no realtime delays). It covers DAYS_AHEAD
days from today; re-run it every few weeks (and whenever HVV publishes a new feed).
"""

import csv, io, json, os, sys, zipfile, datetime, re

# --------------------------------------------------------------------------- #
# BOARDS -- mirrors the live SwiftBar plugin's AREAS/boards exactly:
#   "modes": {mode: None}            -> every line of that mode, no filter
#   "modes": {mode: {"4","5"}}       -> only those line names, for that mode
#   "exclude": {...}                 -> these line names are dropped even if
#                                        otherwise unfiltered
# "name_re" identifies the physical stop by name (case-insensitive search).
# mode is inferred from the line's own name: S+digit -> suburban, U+digit ->
# subway, anything else -> bus (same heuristic as the live plugin).
# --------------------------------------------------------------------------- #
EXCLUDED_UNI_BUSES = {"M4", "M5", "M15", "603", "604", "105"}

BOARDS = {
    "s":   {"name_re": r"hammerbrook(?!.*nord)", "modes": {"suburban": {"S3", "S5"}}},
    "bus": {"name_re": r"hammerbrook.*nord",      "modes": {"bus": {"12"}}},

    "uni_eimsbuettel":   {"name_re": r"bezirksamt\s*eimsb",            "modes": {"bus": None}, "exclude": EXCLUDED_UNI_BUSES},
    "uni_grindelhof":    {"name_re": r"^grindelhof",                   "modes": {"bus": None}, "exclude": EXCLUDED_UNI_BUSES},
    "uni_unistabi":      {"name_re": r"universit.*staatsbibliothek",   "modes": {"bus": None}, "exclude": EXCLUDED_UNI_BUSES},
    "uni_bundesstr":     {"name_re": r"^bundesstra",                   "modes": {"bus": None}, "exclude": EXCLUDED_UNI_BUSES},
    "uni_dammtor":       {"name_re": r"dammtor",                       "modes": {"suburban": None, "bus": {"4", "5"}}},
    "uni_stephansplatz": {"name_re": r"stephansplatz",                 "modes": {"subway": None,   "bus": {"4", "5"}}},

    "berliner_tor": {"name_re": r"berliner\s*tor", "modes": {"suburban": None, "subway": None}},
}
DAYS_AHEAD = 90          # how many days of timetable to bake in (~3 months; re-run every ~2-3 months)
OUT = "hammerbrook_schedule.json"

norm = lambda s: re.sub(r"\s+", "", (s or "").strip().upper())   # "S 3" -> "S3"


def infer_mode(line_name):
    """Same heuristic as the live plugin: S+digit -> suburban, U+digit -> subway, else bus."""
    n = (line_name or "").strip().upper()
    if n[:1] == "S" and n[1:2].isdigit():
        return "suburban"
    if n[:1] == "U" and n[1:2].isdigit():
        return "subway"
    return "bus"


def accepts(board, line_name):
    mode = infer_mode(line_name)
    modes = board.get("modes", {})
    if mode not in modes:
        return False
    rn = norm(line_name)
    if rn in {norm(x) for x in board.get("exclude", ())}:
        return False
    allow = modes[mode]
    if allow is not None and rn not in {norm(x) for x in allow}:
        return False
    return True


def open_member(zf, suffix):
    """Open a GTFS .txt member (case-insensitive, tolerates a subfolder)."""
    for n in zf.namelist():
        if n.lower().rstrip("/").endswith(suffix):
            return io.TextIOWrapper(zf.open(n), encoding="utf-8-sig", newline="")
    return None


def reader(zf, suffix):
    f = open_member(zf, suffix)
    return csv.DictReader(f) if f else iter(())


def parse_minutes(t):
    # GTFS times are HH:MM:SS and HH may be >= 24 (after-midnight trips)
    try:
        h, m, *_ = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def main(zip_path):
    today = datetime.date.today()
    window = [today + datetime.timedelta(days=i) for i in range(DAYS_AHEAD + 1)]

    with zipfile.ZipFile(zip_path) as zf:
        # 1) every route's display name, by route_id (no upfront filtering --
        #    unfiltered boards need to see ANY route, so we decide acceptance
        #    later, per board, once we know which stop it's at).
        route_name = {}
        for row in reader(zf, "routes.txt"):
            route_name[row["route_id"]] = (row.get("route_short_name") or row.get("route_long_name") or "").strip()
        if not route_name:
            sys.exit("No routes found in this feed -- is the ZIP structure as expected?")

        # 2) candidate stop_ids per board, by name; and the reverse lookup
        #    (stop_id -> which boards care about it) for the streaming pass.
        name_re = {k: re.compile(v["name_re"], re.I) for k, v in BOARDS.items()}
        stop_name = {}
        cand_stops = {k: set() for k in BOARDS}
        for row in reader(zf, "stops.txt"):
            sid, nm = row["stop_id"], row.get("stop_name", "")
            stop_name[sid] = nm
            low = nm.lower()
            for k in BOARDS:
                if name_re[k].search(low):
                    cand_stops[k].add(sid)
        stop_to_boards = {}
        for k, ids in cand_stops.items():
            for sid in ids:
                stop_to_boards.setdefault(sid, set()).add(k)
        if not stop_to_boards:
            sys.exit("No stops matched ANY board's name_re -- check the patterns in BOARDS.")
        all_candidate_stops = set(stop_to_boards.keys())

        # 3) every trip -> (route_id, headsign, service_id). Loaded in full since
        #    we don't know which trips matter until we see their stop_times rows.
        trips = {}
        for row in reader(zf, "trips.txt"):
            trips[row["trip_id"]] = (
                row["route_id"],
                (row.get("trip_headsign") or "").strip(),
                row["service_id"],
            )
        if not trips:
            sys.exit("No trips found in this feed.")

        # 4) stream stop_times.txt once; for each row at one of our candidate
        #    stops, check every board that cares about that stop and keep the
        #    row if that board accepts this line (mode + allow-list + exclude).
        deps = {k: [] for k in BOARDS}
        seen = {k: set() for k in BOARDS}
        used_services = {k: set() for k in BOARDS}
        used_stops = {k: set() for k in BOARDS}
        used_lines = {k: set() for k in BOARDS}

        for row in reader(zf, "stop_times.txt"):
            sid = row.get("stop_id")
            if sid not in all_candidate_stops:
                continue
            trip = trips.get(row.get("trip_id"))
            if not trip:
                continue
            route_id, head, svc = trip
            line = route_name.get(route_id, "")
            mins = parse_minutes(row.get("departure_time") or row.get("arrival_time") or "")
            if mins is None:
                continue
            for key in stop_to_boards.get(sid, ()):
                board = BOARDS[key]
                if not accepts(board, line):
                    continue
                hkey = (mins, line, head, svc)
                if hkey in seen[key]:
                    continue
                seen[key].add(hkey)
                deps[key].append([mins, line, head or "—", svc])
                used_services[key].add(svc)
                used_stops[key].add(sid)
                used_lines[key].add(line)

        all_used = set().union(*used_services.values()) if used_services else set()

        # 5) resolve active dates per service within the window
        cal = {}
        for row in reader(zf, "calendar.txt"):
            sid = row["service_id"]
            if sid in all_used:
                cal[sid] = {
                    "days": [row.get(d, "0") for d in
                             ("monday", "tuesday", "wednesday", "thursday",
                              "friday", "saturday", "sunday")],
                    "start": row.get("start_date", "00000000"),
                    "end": row.get("end_date", "99999999"),
                }
        caldates = {}
        for row in reader(zf, "calendar_dates.txt"):
            sid = row["service_id"]
            if sid in all_used:
                caldates.setdefault(sid, {})[row["date"]] = row.get("exception_type", "1")

        def active(sid, d):
            ds = d.strftime("%Y%m%d")
            exc = caldates.get(sid, {}).get(ds)
            if exc == "2":
                return False
            if exc == "1":
                return True
            c = cal.get(sid)
            if not c:
                return False
            return c["start"] <= ds <= c["end"] and c["days"][d.weekday()] == "1"

        services_out = {}
        for sid in all_used:
            dates = [d.strftime("%Y%m%d") for d in window if active(sid, d)]
            if dates:
                services_out[sid] = dates

        # drop departures whose service never runs in the window
        for k in BOARDS:
            deps[k] = sorted((d for d in deps[k] if d[3] in services_out),
                              key=lambda x: x[0])

        def label(k):
            names = [stop_name.get(s, "") for s in used_stops[k]]
            names = [n for n in names if n]
            return sorted(set(names), key=len)[0] if names else k

        out = {
            "meta": {
                "generated": today.isoformat(),
                "days_ahead": DAYS_AHEAD,
                "source": "HVV GTFS (Transparenzportal Hamburg)",
                "attribution": "Hamburger Verkehrsverbund GmbH (dl-de/by-2-0)",
                "note": "scheduled times only -- no realtime delays",
            },
            "services": services_out,
            "boards": {
                k: {
                    "label": label(k),
                    "lines": sorted(used_lines[k]),
                    "departures": deps[k],
                }
                for k in BOARDS
            },
        }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(OUT) / 1024
    print(f"wrote {OUT}  ({size:.0f} KB)")
    for k in BOARDS:
        cands = ", ".join(sorted({stop_name.get(s, s) for s in used_stops[k]})) or "(none!)"
        lines = ", ".join(sorted(used_lines[k])) or "(none!)"
        print(f"  [{k}] stop(s): {cands}")
        print(f"       lines included: {lines}")
        print(f"       {len(deps[k])} departures, {len(used_services[k])} service patterns")
    if any(not deps[k] for k in BOARDS):
        print("  WARNING: a board has no departures -- check its name_re / modes / exclude in BOARDS.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python build_hvv_fallback.py <hvv_gtfs.zip>")
    main(sys.argv[1])
