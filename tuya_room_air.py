#!/usr/bin/env python3
"""
Pull a current reading from a Tuya-based air-quality sensor ("Air Housekeeper")
via the Tuya Cloud OpenAPI, and write a small JSON snapshot for the dashboard
to read. Meant to be run periodically by .github/workflows/room-air.yml --
each run does one auth + one status fetch, nothing persistent.

Required environment variables (set as GitHub Actions secrets -- NEVER commit
these or paste them anywhere public):
  TUYA_API_BASE      e.g. https://openapi.tuyaeu.com -- copied exactly from
                     your Tuya IoT Platform project's Overview page
                     ("API Endpoint" / "Base URL").
  TUYA_CLIENT_ID      from the project Overview page ("Access ID / Client ID").
  TUYA_CLIENT_SECRET  from the project Overview page ("Access Secret / Client Secret").
  TUYA_DEVICE_ID      the device's ID -- shown as "Virtual ID" in the SmartLife
                     app's Device Information screen, and also visible on the
                     Tuya IoT Platform under Devices -> your device -> Device Info.

Output: room_air.json (current directory; the workflow commits it to the repo).

FIRST RUN: set DEBUG_DUMP=1 to print every raw code/value the device reports.
Tuya's "codes" differ slightly by device/firmware, so use that output to
confirm (or correct) the CODE_MAP below before relying on the friendly fields.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ["TUYA_API_BASE"].rstrip("/")
CLIENT_ID = os.environ["TUYA_CLIENT_ID"]
CLIENT_SECRET = os.environ["TUYA_CLIENT_SECRET"]
DEVICE_ID = os.environ["TUYA_DEVICE_ID"]
DEBUG_DUMP = os.environ.get("DEBUG_DUMP") == "1"
OUT = "room_air.json"

# Raw Tuya "code" -> friendly field name. Covers the common variants seen on
# 6-in-1 air-quality boxes; confirm against YOUR device with DEBUG_DUMP=1 and
# add/adjust entries here if something doesn't show up in `values` below.
CODE_MAP = {
    "pm25": "pm25",
    "pm25_value": "pm25",
    "co2": "co2",
    "co2_value": "co2",
    "ch2o": "formaldehyde",
    "ch2o_value": "formaldehyde",
    "voc": "tvoc",
    "voc_value": "tvoc",
    "temp_current": "temperature",
    "va_temperature": "temperature",
    "humidity_value": "humidity",
    "va_humidity": "humidity",
}

# Some Tuya DPs report a scaled integer (e.g. 268 meaning 26.8). Confirmed from
# a real reading: temp_current/va_temperature and humidity_value/va_humidity
# both need /10 (268 -> 26.8 C, 521 -> 52.1% -- the unscaled values would be
# physically impossible, so this one's certain). ch2o_value's scale is a
# reasonable guess (Tuya commonly reports formaldehyde in units of 0.01 mg/m3)
# but NOT confirmed -- check the device's "scale" field under the Functions
# tab on the Tuya IoT Platform if this seems off, and adjust here.
SCALE_MAP = {
    "temperature": 10,
    "humidity": 10,
    "formaldehyde": 100,   # best guess, not yet confirmed -- see note above
}


def _sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sign(t, access_token, method, path, body=b""):
    # Tuya OpenAPI 2.0 signature: HMAC-SHA256 of
    #   client_id + access_token + t + (Method\nContentSHA256\nHeaders\nURL)
    string_to_sign = f"{method}\n{_sha256_hex(body)}\n\n{path}"
    pre = CLIENT_ID + (access_token or "") + t + string_to_sign
    return hmac.new(CLIENT_SECRET.encode(), pre.encode(), hashlib.sha256).hexdigest().upper()


def _request(method, path, access_token=None, body=b""):
    t = str(int(time.time() * 1000))
    sign = _sign(t, access_token, method, path, body)
    headers = {
        "client_id": CLIENT_ID,
        "sign": sign,
        "t": t,
        "sign_method": "HMAC-SHA256",
        "Content-Type": "application/json",
    }
    if access_token:
        headers["access_token"] = access_token
    req = urllib.request.Request(API_BASE + path, data=(body or None), method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from Tuya: {e.read().decode(errors='replace')}")


def get_token():
    res = _request("GET", "/v1.0/token?grant_type=1")
    if not res.get("success"):
        sys.exit(f"token request failed -- check TUYA_API_BASE/CLIENT_ID/CLIENT_SECRET: {res}")
    return res["result"]["access_token"]


def get_status(token):
    res = _request("GET", f"/v1.0/devices/{DEVICE_ID}/status", access_token=token)
    if not res.get("success"):
        sys.exit(f"status request failed -- check TUYA_DEVICE_ID: {res}")
    return res["result"]  # list of {"code": ..., "value": ...}


def main():
    token = get_token()
    raw = get_status(token)

    if DEBUG_DUMP:
        print("Raw codes reported by this device:")
        for item in raw:
            print(f"  {item['code']!r}: {item['value']!r}")

    values = {}
    for item in raw:
        friendly = CODE_MAP.get(item["code"])
        if friendly:
            v = item["value"]
            divisor = SCALE_MAP.get(friendly)
            if divisor and isinstance(v, (int, float)):
                v = round(v / divisor, 2)
            values[friendly] = v

    out = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "values": values,                                   # friendly fields, for the dashboard
        "raw": {item["code"]: item["value"] for item in raw},  # kept for reference/debugging
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {OUT}: {values}")


if __name__ == "__main__":
    main()
