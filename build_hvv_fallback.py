#!/usr/bin/env python3
"""
Build a tiny offline timetable for the Hammerbrook board from the official
HVV GTFS feed, so the web board has a fallback when the realtime API is down.

Source feed (≈40 MB ZIP, free under Datenlizenz Deutschland – Namensnennung 2.0,
attribution "Hamburger Verkehrsverbund GmbH"):
  https://suche.transparenz.hamburg.de/dataset/hvv-fahrplandaten-gtfs-april-2026-bis-dezember-2026
Download the ZIP from that page (the "Upload__hvv_Rohdaten_GTFS_Fpl_*.ZIP" resource),
then run:

  python build_hvv_fallback.py hvv_gtfs.zip

Output: hammerbrook_schedule.json  (put it next to hammerbrook_board.html)

The output is *scheduled* times only (no realtime delays). It covers DAYS_AHEAD
days from today; re-run it every few weeks (and whenever HVV publishes a new feed).
"""

import csv, io, json, sys, zipfile, datetime, re

# --------------------------------------------------------------------------- #
# What to extract.  A stop is kept only if it is served by the board's routes
# AND its name matches the regex — so line + stop together pin the right platform.
# --------------------------------------------------------------------------- #
BOARDS = {
    "s":   {"routes": {"S3", "S5"}, "name_re": r"hammerbrook"},          # S-Bahn (City Süd)
    "bus": {"routes": {"12"},       "name_re": r"hammerbrook.*nord"},    # S Hammerbrook (Nord)
}
DAYS_AHEAD = 28          # how many days of timetable to bake in
OUT = "hammerbrook_schedule.json"

norm = lambda s: re.sub(r"\s+", "", (s or "").strip().upper())   # "S 3" -> "S3"


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
        # 1) routes we care about -> route_id -> board key
        want_routes = {k: v["routes"] for k, v in BOARDS.items()}
        route_board = {}                       # route_id -> board key
        for row in reader(zf, "routes.txt"):
            short = norm(row.get("route_short_name"))
            longn = norm(row.get("route_long_name"))
            for key, names in want_routes.items():
                wn = {norm(x) for x in names}
                if short in wn or longn in wn:
                    route_board[row["route_id"]] = key
        if not route_board:
            sys.exit("No matching routes found — check the route short names in BOARDS.")

        # 2) candidate stop_ids per board, by name
        name_re = {k: re.compile(v["name_re"], re.I) for k, v in BOARDS.items()}
        stop_name = {}                         # stop_id -> name
        cand_stops = {k: set() for k in BOARDS}
        for row in reader(zf, "stops.txt"):
            sid, nm = row["stop_id"], row.get("stop_name", "")
            stop_name[sid] = nm
            low = nm.lower()
            for k in BOARDS:
                if name_re[k].search(low):
                    cand_stops[k].add(sid)

        # 3) trips on our routes -> trip_id -> (board, headsign, service_id)
        trips = {}
        for row in reader(zf, "trips.txt"):
            key = route_board.get(row["route_id"])
            if key:
                trips[row["trip_id"]] = (
                    key,
                    (row.get("trip_headsign") or "").strip(),
                    row["service_id"],
                )
        if not trips:
            sys.exit("No trips found for the wanted routes.")

        # 4) stream stop_times.txt, keep rows at our stops on our trips
        deps = {k: [] for k in BOARDS}         # board -> list of (min, line, head, service)
        seen = {k: set() for k in BOARDS}
        line_of = {rid: None for rid in route_board}
        # remember each route's printed line name for the badge
        for row in reader(zf, "routes.txt"):
            if row["route_id"] in route_board:
                line_of[row["route_id"]] = (row.get("route_short_name") or "").strip()
        # need trip_id -> line name; map via route through trips file again is costly,
        # so capture line at trip read instead:
        trip_line = {}
        for row in reader(zf, "trips.txt"):
            if row["trip_id"] in trips:
                trip_line[row["trip_id"]] = line_of.get(row["route_id"], "")

        used_services = {k: set() for k in BOARDS}
        used_stops = {k: set() for k in BOARDS}
        for row in reader(zf, "stop_times.txt"):
            tid = row.get("trip_id")
            t = trips.get(tid)
            if not t:
                continue
            key, head, svc = t
            sid = row.get("stop_id")
            if sid not in cand_stops[key]:
                continue
            mins = parse_minutes(row.get("departure_time") or row.get("arrival_time") or "")
            if mins is None:
                continue
            line = trip_line.get(tid) or ""
            hkey = (mins, line, head, svc)
            if hkey in seen[key]:
                continue
            seen[key].add(hkey)
            deps[key].append([mins, line, head or "—", svc])
            used_services[key].add(svc)
            used_stops[key].add(sid)

        all_used = set().union(*used_services.values())

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
                "note": "scheduled times only — no realtime delays",
            },
            "services": services_out,
            "boards": {
                k: {
                    "label": label(k),
                    "lines": sorted(BOARDS[k]["routes"]),
                    "departures": deps[k],
                }
                for k in BOARDS
            },
        }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    import os
    size = os.path.getsize(OUT) / 1024
    print(f"wrote {OUT}  ({size:.0f} KB)")
    for k in BOARDS:
        cands = ", ".join(sorted({stop_name.get(s, s) for s in used_stops[k]})) or "(none!)"
        print(f"  [{k}] stops: {cands}")
        print(f"       {len(deps[k])} departures, {len(used_services[k])} service patterns")
    if any(not deps[k] for k in BOARDS):
        print("  WARNING: a board has no departures — check route names / stop name regex at top.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python build_hvv_fallback.py <hvv_gtfs.zip>")
    main(sys.argv[1])
