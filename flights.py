#!/usr/bin/env python3
"""
LED-style flight display with three modes:
    radar (default)   the nearest commercial airline flight with a known route
    map               full-screen radar of every plane and helicopter nearby, with flight paths
    UAL123 / UA123    one flight anywhere in the world, with a map of its whole route

Data: adsb.fi / adsb.lol (live positions and flight paths),
adsbdb.com and adsb.lol (routes), OurAirports (airport locations), Aviasales (logos),
Esri World Imagery (satellite map).

Options (any mode, any order):  r=RADIUS (miles)   airports=true|false   airfields=true|false
                                num_flights=N|all   heli_only=true|false   roads=true|false
                                set_coords=LAT,LON   (radar and map center, until the program closes)
    python flights.py map r=30 airports=true airfields=false num_flights=5 heli_only=false roads=false

While it runs, switch modes without restarting:
    Enter        type a command on the display (same format, e.g. "map r=30"), Enter to switch, Esc to cancel;
                 options alone (e.g. "r=30") change only those, in the current mode
    R / M        switch to radar / map
    T            in radar and map mode: track the blue flight (does nothing if there isn't one)
    Mouse        in map and radar mode: hover over an aircraft to see its flight number, click it to track it
    or type a command in the terminal and press Enter.
F11 toggles full screen, Esc quits.
"""
import csv
import gzip
import io
import json
import math
import os
import queue
import re
import sys
import threading
import time
import unicodedata

import pygame
import requests



def load_env(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Read KEY=VALUE lines from .env (next to this file) into os.environ, without overriding real variables."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


load_env()

# ================= SETTINGS =================

def parse_coords(text):
    """(lat, lon) from "LAT,LON" in decimal degrees, or None if it isn't two numbers in range."""
    parts = text.split(",")
    try:
        lat, lon = (float(x) for x in parts)
    except ValueError:  # not two numbers
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


# personal settings live in .env (see .env.example)
HOME_COORDS = parse_coords(os.environ.get("CENTER_COORDS", ""))
if not HOME_COORDS:
    sys.exit("Set CENTER_COORDS=LAT,LON (the center of the map) in a .env file next to flights.py; see .env.example")
CENTER_COORDS = HOME_COORDS  # radar and map center; set_coords= moves it until the program closes
RADIUS_MILES = 100        # search radius in miles (max 287)
NUM_FLIGHTS = 1           # radar mode: how many of the closest airline flights to show (0 shows none)
MAP_NUM_FLIGHTS = None  # map mode: how many of the closest aircraft to show (None = all)
MAP_RADIUS_MILES = 10   # map mode zoom: this distance fits from the center to the top and bottom edges
FLAGGED_AIRCRAFT = [n for n in os.environ.get("FLAGGED_AIRCRAFT", "").split(",") if n.strip()]  # from .env: colored orange in radar and map mode (callsign or tail number)
MAX_TRACES = 4            # flight paths to download per refresh, map and radar (more risks rate limits)
TRACK_NO_ROUTE_RADIUS_MILES = 1000  # tracking mode: map radius around the plane when its route is unknown
REFRESH_SECONDS = 15      # how often to download new flight data
ETA_BUFFER_MINUTES = 15   # added to every ETA for the approach and landing
MAX_LOOKUPS = 8           # new flights to look up per refresh
TAKEOFF_AIRPORT_MILES = 10  # the departure airport must be this close to where the flight's path shows it took off
SATELLITE_MAP = True
SHOW_AIRPORTS = True      # radar and tracking: show the route's departure and arrival airports on the map
MAP_SHOW_AIRPORTS = True   # map mode: show every airline airport on the map
MAP_SHOW_AIRFIELDS = False  # map mode: show every other airport too
MAX_AIRPORT_LABELS = 40   # past this many codes on one map, extra airports are just dots
CROWDED_AIRPORTS = 60     # with more airports than this on a map, they shrink to single LEDs (route airports don't)
HELI_ONLY = False         # radar: the selected flight plus helicopters (num_flights - 1 of them) instead of extras
MAP_HELI_ONLY = False     # map mode: show only helicopters
SHOW_ROADS = False        # draw roads on top of the satellite map
START_FULLSCREEN = False

GRID_W, GRID_H = 256, 144  # size of the LED grid

COLOR_OFF = (20, 20, 20)
COLOR_CALLSIGN = (255, 190, 0)
COLOR_AIRPORT = (0, 210, 255)
COLOR_TEXT = (230, 230, 230)
COLOR_LABEL = (120, 120, 120)
COLOR_LINE = (60, 60, 60)
COLOR_GRID = (34, 34, 34)
COLOR_HOME = (110, 255, 110)
COLOR_MILITARY = (255, 60, 60)
COLOR_ROAD = (240, 200, 110)
COLOR_FLAGGED_AIRCRAFT = (255, 150, 0)  # flagged aircraft in map mode
COLOR_EXTRA = (200, 200, 200)  # the extra flights on the radar-mode map
COLOR_SMALL_AIRPORT = (170, 170, 170)  # map mode: airports without airline service
COLOR_UP = (110, 255, 110)
COLOR_DOWN = (255, 140, 60)
# ============================================

FEED_URLS = [  # tried in this order; adsb.fi first since adsb.lol is often overloaded
    "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}",
    "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}",
]
CALLSIGN_URLS = [
    "https://opendata.adsb.fi/api/v2/callsign/{cs}",
    "https://api.adsb.lol/v2/callsign/{cs}",
]
TRACE_URLS = [  # each aircraft's flight path so far today
    "https://adsb.lol/data/traces/{folder}/trace_full_{hex}.json",
    "https://globe.adsb.fi/data/traces/{folder}/trace_full_{hex}.json",
]
ADSBDB_URL = "https://api.adsbdb.com/v0/"
ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"  # adsb.lol's route database (a second opinion)
AIRPORTS_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"  # OurAirports
AIRPORTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "airports.csv")  # kept next to this file
LOGO_URL = "http://img.wway.io/pics/root/{iata}@png?exar=1&rs=fit:256:256"  # large source, shrunk to LEDs here
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ROADS_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"
LOGO_H = 34      # logo height in LEDs: the full header
LOGO_MAX_W = 46  # wide logos may use this much width
LOGO_STRETCH = 1.15  # logos are drawn this much taller than their real shape (1 = no stretch)

# Airports whose code changed. Databases that haven't caught up still use the old code; both count as the
# same airport, and the new code is shown.
RENAMED_AIRPORTS = {"PBI": "DJT", "KPBI": "DJT", "KDJT": "DJT"}  # Palm Beach International

# Private jet / charter operators: not airlines, and they reuse callsigns so their routes are wrong
NON_AIRLINE_OPERATORS = {"EJA", "EJM", "LXJ", "VJT", "XSR", "JTL", "GAJ", "TWY", "FLX", "JRE", "KFB"}

_FONT = {
    "A": ".###. #...# #...# ##### #...# #...# #...#",
    "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.",
    "D": "###.. #..#. #...# #...# #...# #..#. ###..",
    "E": "##### #.... #.... ####. #.... #.... #####",
    "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .####",
    "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.",
    "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#",
    "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.",
    "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.",
    "V": "#...# #...# #...# #...# #...# .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# #.#.# .#.#.",
    "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",
    "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.",
    "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####",
    "3": "##### ...#. ..#.. ...#. ....# #...# .###.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.",
    "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.",
    "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.",
    "9": ".###. #...# #...# .#### ....# ...#. .##..",
    " ": "..... ..... ..... ..... ..... ..... .....",
    "-": "..... ..... ..... ##### ..... ..... .....",
    ".": "..... ..... ..... ..... ..... .##.. .##..",
    ",": "..... ..... ..... ..... .##.. ..#.. .#...",
    ":": "..... .##.. .##.. ..... .##.. .##.. .....",
    "/": "..... ....# ...#. ..#.. .#... #.... .....",
    "=": "..... ..... ##### ..... ##### ..... .....",
    "_": "..... ..... ..... ..... ..... ..... #####",
    ">": ".#... ..#.. ...#. ....# ...#. ..#.. .#...",
    "+": "..... ..#.. ..#.. ##### ..#.. ..#.. .....",
    "%": "##... ##..# ...#. ..#.. .#... #..## ...##",
    "'": "..#.. ..#.. .#... ..... ..... ..... .....",
    "(": "...#. ..#.. .#... .#... .#... ..#.. ...#.",
    ")": ".#... ..#.. ...#. ...#. ...#. ..#.. .#...",
    "&": ".##.. #..#. #.#.. .#... #.#.# #..#. .##.#",
}

GLYPHS = {ch: rows.split() for ch, rows in _FONT.items()}

# Military aircraft: flagged by the feed, or using these callsign prefixes
MILITARY_PREFIXES = ("RCH", "REACH", "SAM", "SPAR", "CNV", "PAT", "NAVY", "ARMY", "VV", "TOPCAT", "EVAC", "NATO")
HELICOPTER_WORDS = ("HELICOPTER", "COPTER", "ROBINSON")  # backup check on the feed's aircraft description
# Every helicopter ICAO type code (the feed's "t" field), from the ICAO aircraft type list used by readsb/tar1090
HELICOPTER_TYPES = set("""
A109 A119 A129 A139 A149 A169 A189 A2RT A600 AC31 AC33 ALH ALO2 ALO3 ANST AS32 AS3B AS50 AS55
AS65 B06 B06T B105 B150 B212 B214 B222 B230 B305 B407 B412 B427 B429 B430 B47G B47J B47T B505
B525 BABY BK17 BRB2 BSTP CH12 CH14 CH7 CHIF COMU DJIN DRAG DYH2 DYH3 EC20 EC25 EC30 EC35 EC45
EC55 EC75 EGL3 EH10 ELTO EN28 EN48 ES11 EXEC EXEJ EXPL FH11 FREL G2CA GAZL H12T H160 H2 H21 H269
H43A H43B H46 H47 H500 H53 H53S H60 H64 HSMT HUCO HX2 IS2 JAG2 K126 K209 K226 KA25 KA26 KA27
KA50 KA52 KA62 KH4 KMAX LAMA LCH LR2T LYNX M74 MD52 MD60 MH20 MI10 MI14 MI2 MI24 MI26 MI28 MI34
MI38 MI4 MI6 MI8 NA40 NH90 OH1 PHIL PSW4 PUMA R22 R4 R44 R66 RMOU RP1 RVAL S274 S278 S285 S330
S360 S434 S51 S52 S55P S55T S58P S58T S61 S61R S62 S64 S65C S76 S92 S97 SB1 SCOR SCOU SH09 SH4
SUCO SURN SYCA TIGR UH1 UH12 UH1Y UHEL ULTS V500 W3 WASP WESX WG30 WZ10 X2 X3 X49 YNHL ZA6 ZEFR
""".split())

SMALL_PLANE_ICON = [  # map mode: small planes, pointing east
    "..#....",
    "...#...",
    "#######",
    "...#...",
    "..#....",
]

HELICOPTER_DOT = [  # map mode: helicopters (solid, so it doesn't look like the green home marker)
    "###",
    "###",
    "###",
]

PLANE_ICON = [  # pointing east; rotated to the plane's heading on the map
    "....##.....",
    ".....##....",
    "#.....##...",
    "##########.",
    "#.....##...",
    ".....##....",
    "....##.....",
]

# ======================= drawing helpers =======================
def clean(text):
    """Uppercase and strip accents so text fits the LED font."""
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().upper()


def fit(text, max_chars):
    text = clean(text)
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "."


def text_width(text, scale=1):
    return max(0, len(str(text)) * 6 * scale - scale)


def draw_icon(surf, rows, x, y, color, scale=1, heights=None):
    """heights: optional height in LEDs of each row, for taller text (default: scale for every row)."""
    for row, h in zip(rows, heights or [scale] * len(rows)):
        for rx, bit in enumerate(row):
            if bit == "#":
                surf.fill(color, (x + rx * scale, y, scale, h))
        y += h


def draw_text(surf, text, x, y, color, scale=1, heights=None):
    for ch in clean(text):
        draw_icon(surf, GLYPHS.get(ch, GLYPHS[" "]), x, y, color, scale, heights)
        x += 6 * scale


def draw_text_right(surf, text, right, y, color, scale=1):
    """Right-align so the last lit LED column sits exactly at right - 1."""
    rows = GLYPHS.get(clean(text)[-1:], GLYPHS[" "])
    blank = next((i for i in range(5) if any(r[4 - i] == "#" for r in rows)), 0)
    draw_text(surf, text, right - text_width(text, scale) + blank * scale, y, color, scale)


def draw_text_center(surf, text, center, y, color, scale=1):
    draw_text(surf, text, center - text_width(text, scale) // 2, y, color, scale)


def draw_rotated_icon(surf, rows, cx, cy, angle_deg, color):
    """Draw an icon rotated clockwise by angle_deg, centered on (cx, cy)."""
    h, w = len(rows), len(rows[0])
    ox, oy = (w - 1) / 2, (h - 1) / 2
    ca, sa = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    r = max(w, h) // 2 + 1
    for ty in range(-r, r + 1):
        for tx in range(-r, r + 1):
            ix = round(tx * ca + ty * sa + ox)
            iy = round(-tx * sa + ty * ca + oy)
            if 0 <= ix < w and 0 <= iy < h and rows[iy][ix] == "#":
                surf.fill(color, (cx + tx, cy + ty, 1, 1))


def draw_outlined_icon(surf, rows, cx, cy, angle_deg, color):
    """Rotated icon with a black outline so it stands out on the satellite map."""
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw_rotated_icon(surf, rows, cx + dx, cy + dy, angle_deg, (0, 0, 0))
    draw_rotated_icon(surf, rows, cx, cy, angle_deg, color)


def draw_dotted_path(surf, view, points, color, spacing):
    """One LED every `spacing` pixels along a path of (lat, lon) points."""
    prev, since_dot = None, 0.0
    for pt in points:
        cur = view.project(*pt)
        if prev is None:
            surf.fill(color, (round(cur[0]), round(cur[1]), 1, 1))
        else:
            seg = math.hypot(cur[0] - prev[0], cur[1] - prev[1])
            d = spacing - since_dot
            while d <= seg:
                f = d / seg
                surf.fill(color, (round(prev[0] + (cur[0] - prev[0]) * f), round(prev[1] + (cur[1] - prev[1]) * f), 1, 1))
                d += spacing
            since_dot = seg - (d - spacing)
        prev = cur


# ======================= geography =======================
EARTH_RADIUS_MILES = 3958.8
MILES_PER_DEGREE = EARTH_RADIUS_MILES * math.pi / 180  # one degree of latitude
KNOTS_TO_MPH = 1.15078  # the feed reports speed in knots
NM_TO_MILES = 1.15078   # the feed's search radius is in nautical miles


def distance_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def great_circle(a, b, step_miles=3):
    """Points along the shortest path over the globe between two (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    v1 = (math.cos(la1) * math.cos(lo1), math.cos(la1) * math.sin(lo1), math.sin(la1))
    v2 = (math.cos(la2) * math.cos(lo2), math.cos(la2) * math.sin(lo2), math.sin(la2))
    d = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(v1, v2)))))
    if d < 1e-9:
        return [a, b]
    n = max(2, min(3000, int(d * EARTH_RADIUS_MILES / step_miles)))
    pts = []
    for i in range(n + 1):
        f = i / n
        wa, wb = math.sin((1 - f) * d) / math.sin(d), math.sin(f * d) / math.sin(d)
        x, y, z = (wa * p + wb * q for p, q in zip(v1, v2))
        pts.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return pts


def compass(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(deg / 45) % 8]


def current_position(p, now):
    """Extrapolate from the last report using speed and heading, so the plane moves smoothly."""
    dt = min(now - p["fetched"], 60)
    if not p["mph"] or p["track"] is None or dt <= 0:
        return p["lat"], p["lon"]
    deg = p["mph"] * dt / 3600 / MILES_PER_DEGREE
    lat = p["lat"] + deg * math.cos(math.radians(p["track"]))
    lon = p["lon"] + deg * math.sin(math.radians(p["track"])) / max(0.1, math.cos(math.radians(p["lat"])))
    return lat, lon


# ======================= live positions (adsb.lol / adsb.fi) =======================
def number(value):
    return value if isinstance(value, (int, float)) else None


def parse_plane(ac, now):
    """One aircraft from the feed -> plane dict. None if it has no position."""
    lat, lon, alt = ac.get("lat"), ac.get("lon"), ac.get("alt_baro")
    last = ac.get("lastPosition") or {}
    if (lat is None or lon is None) and (last.get("seen_pos") or 999) < 120:
        lat, lon = last.get("lat"), last.get("lon")  # position a little stale (low aircraft drop in and out)
    if lat is None or lon is None:
        return None
    return {
        "icao24": (ac.get("hex") or "").lower(),
        "callsign": (ac.get("flight") or "").strip().upper(),
        "lat": lat, "lon": lon, "fetched": now,
        "ground": alt == "ground",
        "alt": number(alt),
        "mph": number(ac.get("gs")) and number(ac.get("gs")) * KNOTS_TO_MPH,
        "track": number(ac.get("track")),
        "vrate": number(ac.get("baro_rate", ac.get("geom_rate"))),
        "squawk": ac.get("squawk") or "",
        "reg": ac.get("r") or "",
        "type": ac.get("t") or "",
        "desc": ac.get("desc") or "",
        "category": ac.get("category") or "",
        "military": bool((ac.get("dbFlags") or 0) & 1) or (ac.get("flight") or "").strip().upper().startswith(MILITARY_PREFIXES),
        "dist": distance_miles(*CENTER_COORDS, lat, lon),
        "alt_trend": 0, "mph_trend": 0,  # +1 / -1 / 0 since the previous refresh (see note_trends)
    }


last_reading = {}  # icao24 -> (altitude, speed in mph) at the previous refresh


def trend(before, now, step):
    """+1 if the value went up by at least step, -1 if down by at least step, else 0."""
    if before is None or now is None:
        return 0
    return 1 if now - before >= step else -1 if before - now >= step else 0


def note_trends(planes):
    """Mark whether each plane's altitude and speed went up or down since the previous refresh.
    Changes under 50 ft or 2 mph count as no change, so normal jitter in the readings doesn't flicker."""
    for p in planes:
        before = last_reading.get(p["icao24"], (None, None))
        mph = p["mph"]
        p["alt_trend"] = trend(before[0], p["alt"], 50)
        p["mph_trend"] = trend(before[1], mph, 2)
        last_reading[p["icao24"]] = (p["alt"], mph)


feed_problems = {}  # feed host -> last error, so each problem is printed once


def feed_failed(host, problem):
    if feed_problems.get(host) != problem:
        print(f"Feed {host} failed: {problem}")
    feed_problems[host] = problem


paused_until = {}  # host -> when it may be used again after telling us to slow down


def get_politely(session, url, timeout=5, method="GET", **kwargs):
    """GET (or another method), skipping servers that rate-limited us. Returns None while paused or after a 429."""
    host = url.split("/")[2]
    if time.time() < paused_until.get(host, 0):
        return None
    resp = session.request(method, url, timeout=timeout, **kwargs)
    if resp.status_code == 429:
        wait = resp.headers.get("Retry-After", "")
        wait = int(wait) if wait.isdigit() else 120
        paused_until[host] = time.time() + wait
        feed_failed(host, f"rate limited (HTTP 429), pausing requests to it for {wait}s")
        return None
    return resp


def feed_get(session, urls, **fmt):
    """Aircraft list from the first feed that answers. Feeds that have failed are tried last."""
    for url in sorted(urls, key=lambda u: u.split("/")[2] in feed_problems):
        host = url.split("/")[2]
        try:
            resp = get_politely(session, url.format(**fmt))
        except requests.RequestException as e:
            feed_failed(host, str(e))
            continue
        if resp is None:
            continue
        try:
            body = resp.json()
        except ValueError:  # the server answered with something other than flight data
            feed_failed(host, f"HTTP {resp.status_code}, not JSON: {resp.text[:100]!r}")
            continue
        found = body.get("ac", body.get("aircraft")) if isinstance(body, dict) else None
        if not isinstance(found, list):
            feed_failed(host, f"HTTP {resp.status_code}, no aircraft list in the response")
            continue
        if feed_problems.pop(host, None):
            print(f"Feed {host} is working again")
        return found
    raise ConnectionError("no flight feed responded")


def fetch_aircraft(session, radius_miles):
    """All aircraft within radius_miles, nearest first."""
    now = time.time()
    found = feed_get(session, FEED_URLS, lat=CENTER_COORDS[0], lon=CENTER_COORDS[1], dist=min(radius_miles / NM_TO_MILES, 250))
    return sorted((p for p in (parse_plane(ac, now) for ac in found) if p), key=lambda p: p["dist"])


def fetch_tracked(session, callsigns):
    """The first of these callsigns that's broadcasting anywhere in the world, or None."""
    now = time.time()
    for cs in callsigns:
        for ac in feed_get(session, CALLSIGN_URLS, cs=cs):
            p = parse_plane(ac, now)
            if p and p["callsign"]:
                return p
    return None


# ======================= routes and logos (adsbdb, adsb.lol, Aviasales) =======================
info_cache = {}      # callsign -> {"airline", "iata", "adsbdb": (orig, dest) or None, "adsblol": [(orig, dest), ...]}
airline_cache = {}   # ICAO airline code -> {"name", "iata"} ({} if unknown)
logo_cache = {}      # IATA code -> logo pixels (None if unavailable)
takeoff_cache = {}   # icao24 -> (time checked, airport its current flight took off from, or None if not seen)
departure_cache = {}  # icao24 -> when its current flight took off (wheels up), if its path showed the takeoff
AIRPORTS = []        # airports with airline service, from OurAirports (loaded at startup)
ALL_AIRPORTS = []    # every airport (large, medium, small), shown in map mode


def is_candidate(callsign):
    """Airline-style callsign (3 letters + number), not a private jet operator."""
    return bool(re.fullmatch(r"[A-Z]{3}\d{1,4}[A-Z]{0,2}", callsign)) and callsign[:3] not in NON_AIRLINE_OPERATORS


def renamed(airport):
    """Show an airport under its current code (see RENAMED_AIRPORTS), so old and new codes match each other."""
    new = RENAMED_AIRPORTS.get(airport["code"]) or RENAMED_AIRPORTS.get(airport["icao"])
    if new:
        airport["code"] = airport["icao"] = new
    return airport


def airport(a):
    """An airport from adsbdb."""
    try:
        pos = (float(a["latitude"]), float(a["longitude"]))
    except (KeyError, TypeError, ValueError):
        pos = None
    return renamed({"code": a.get("iata_code") or a.get("icao_code") or "", "icao": a.get("icao_code") or "",
                    "city": a.get("municipality") or a.get("name") or "", "pos": pos})


def lol_airport(a):
    """An airport from adsb.lol."""
    try:
        pos = (float(a["lat"]), float(a["lon"]))
    except (KeyError, TypeError, ValueError):
        pos = None
    return renamed({"code": a.get("iata") or a.get("icao") or "", "icao": a.get("icao") or "",
                    "city": a.get("location") or a.get("name") or "", "pos": pos})


def adsbdb(session, path):
    """adsbdb 'response' object, {} if unknown, or None on a network error (retried later)."""
    try:
        resp = session.get(ADSBDB_URL + path, timeout=10)
        if resp.status_code == 404:
            return {}
        response = resp.json().get("response")
    except Exception:
        return None
    return response if isinstance(response, dict) else {}


def callsign_options(session, flight):
    """Callsigns to search for: as typed, plus the ICAO callsign for an airline flight number like UA123."""
    flight = clean(flight).replace(" ", "")
    options = [flight]
    route = (adsbdb(session, "callsign/" + flight) or {}).get("flightroute")
    if isinstance(route, dict) and route.get("callsign_icao"):
        options.append(route["callsign_icao"])
    m = re.fullmatch(r"([A-Z0-9]{2})(\d{1,4}[A-Z]?)", flight)
    if m:
        try:
            airlines = session.get(ADSBDB_URL + "airline/" + m.group(1), timeout=10).json().get("response")
        except Exception:
            airlines = None
        if isinstance(airlines, list):
            options += [a["icao"] + m.group(2) for a in airlines if a.get("icao") and a.get("iata") == m.group(1)]
    return list(dict.fromkeys(options))


def lookup_route(session, cs):
    """adsbdb's (origin, destination) for a callsign, () if unknown, or None on a network error."""
    response = adsbdb(session, "callsign/" + cs)
    if response is None:
        return None
    r = response.get("flightroute")
    if not isinstance(r, dict):
        return ()
    orig, dest = airport(r.get("origin") or {}), airport(r.get("destination") or {})
    return (orig, dest) if orig["code"] and dest["code"] and orig["pos"] and dest["pos"] else ()


def lookup_airline(session, code):
    """Airline name and IATA code (for the logo) from its 3-letter ICAO code."""
    if code not in airline_cache:
        try:
            resp = session.get(ADSBDB_URL + "airline/" + code, timeout=10)
            airlines = [] if resp.status_code == 404 else resp.json().get("response")
        except Exception:
            return {}  # network error: try again with the next flight
        match = next((a for a in airlines if isinstance(a, dict) and a.get("icao") == code), None) \
            if isinstance(airlines, list) else None
        airline_cache[code] = {"name": match.get("name") or "", "iata": match.get("iata")} if match else {}
    return airline_cache[code]


def look_up_flight(session, cs):
    """adsbdb route, airline and logo for a callsign, into info_cache. False on a network error (retried later)."""
    route = lookup_route(session, cs)
    if route is None:
        return False
    airline = lookup_airline(session, cs[:3])
    info_cache[cs] = {"adsbdb": route or None, "airline": airline.get("name") or "", "iata": airline.get("iata")}
    iata = info_cache[cs]["iata"]
    if iata and iata not in logo_cache:
        logo_cache[iata] = load_logo(session, iata)
    return True


def lookup_routeset(session, planes):
    """adsb.lol's routes for many flights in one request: a second, independent route database.
    Each leg of a multi-stop flight becomes its own possible route."""
    todo = [p for p in planes if p["callsign"] in info_cache and "adsblol" not in info_cache[p["callsign"]]][:100]
    if not todo:
        return
    body = {"planes": [{"callsign": p["callsign"], "lat": p["lat"], "lng": p["lon"]} for p in todo]}
    try:
        resp = get_politely(session, ROUTESET_URL, timeout=10, method="POST", json=body)
        routes = resp.json() if resp is not None and resp.status_code == 200 else None
    except Exception as e:
        feed_failed("api.adsb.lol routes", str(e))
        return  # try again next refresh
    if not isinstance(routes, list):
        return
    for p in todo:
        info_cache[p["callsign"]]["adsblol"] = []
    for r in routes:
        info = info_cache.get(str(r.get("callsign") or "").upper()) if isinstance(r, dict) else None
        if info is not None:
            airports = [a for a in (lol_airport(x) for x in r.get("_airports") or [] if isinstance(x, dict)) if a["pos"]]
            info["adsblol"] = list(zip(airports, airports[1:]))


def load_logo(session, iata):
    """Download an airline logo and shrink it to LED pixels."""
    try:
        resp = session.get(LOGO_URL.format(iata=iata), timeout=10)
        img = pygame.image.load(io.BytesIO(resp.content)).convert_alpha()
    except Exception:
        return None
    bounds = img.get_bounding_rect()  # trim transparent margins so the logo itself fills the space
    if not bounds.w or not bounds.h:
        return None
    img = img.subsurface(bounds)
    scale = min(LOGO_MAX_W / bounds.w, LOGO_H / (bounds.h * LOGO_STRETCH))
    nw, nh = max(1, round(bounds.w * scale)), max(1, min(LOGO_H, round(bounds.h * scale * LOGO_STRETCH)))
    while img.get_width() > nw * 2:  # shrink in halves for sharper detail than one big jump
        img = pygame.transform.smoothscale(img, (max(nw, img.get_width() // 2), max(nh, img.get_height() // 2)))
    small = pygame.transform.smoothscale(img, (nw, nh))
    pixels = []
    for y in range(nh):
        for x in range(nw):
            r, g, b, a = small.get_at((x, y))
            if a < 40:
                continue
            if max(r, g, b) < 45:
                r, g, b = 70, 70, 85  # lift near-black so it shows on the dark display
            f = a / 255  # soft edges: blend partly transparent pixels into the background
            pixels.append((x, y, tuple(round(c * f + o * (1 - f)) for c, o in zip((r, g, b), COLOR_OFF))))
    return {"w": nw, "h": nh, "pixels": pixels} if pixels else None


def update_lookups(session, planes):
    """Routes for new airline flights: adsbdb a few at a time (nearest first), then adsb.lol for all in one request."""
    done = 0
    for p in planes:
        if not is_candidate(p["callsign"]) or p["callsign"] in info_cache:
            continue
        if done >= MAX_LOOKUPS:
            break
        done += 1
        look_up_flight(session, p["callsign"])
    lookup_routeset(session, planes)


def track_flight(session, p, trails):
    """Routes, logo and full flight path for the tracked flight."""
    if p["callsign"] not in info_cache:
        look_up_flight(session, p["callsign"])
    lookup_routeset(session, [p])
    trail = trails.get(p["icao24"])
    if trail is None:
        pts = fetch_trace(session, p["icao24"], trim_far=False)
        trail = trails[p["icao24"]] = {"pts": pts, "downloaded": bool(pts)}
    trail["pts"].append((p["lat"], p["lon"], time.time()))


def route_fits(p, orig, dest):
    """The 3 checks: do the plane's position, heading and climb/descent fit a flight from orig to dest?"""
    route = distance_miles(*orig, *dest)
    flown = distance_miles(*orig, p["lat"], p["lon"])
    left = distance_miles(p["lat"], p["lon"], *dest)
    if flown + left - route > max(40, 0.15 * route):
        return False  # too far off the direct path
    if p["track"] is not None and flown > 30 and left > 30:
        if abs((bearing(p["lat"], p["lon"], *dest) - p["track"] + 180) % 360 - 180) > 60:
            return False  # not heading toward the destination
    if p["alt"] is not None and p["alt"] < 12000 and route > 100:
        if flown < 50 and (p["vrate"] or 0) <= -500:
            return False  # descending near the departure airport: it's actually landing there
        if left < 50 and (p["vrate"] or 0) >= 500:
            return False  # climbing near the arrival airport: it actually just left there
    return True


def load_airports(session):
    """Airports from OurAirports' airports.csv: fills ALL_AIRPORTS (every large, medium and small airport) and
    returns the ones with scheduled airline service. The file is kept next to this program and downloaded
    again when it's over 30 days old."""
    data = None
    exists = os.path.exists(AIRPORTS_FILE)
    if not exists or time.time() - os.path.getmtime(AIRPORTS_FILE) > 30 * 86400:
        try:
            print("Downloading airports.csv (first run, or monthly update)...")
            resp = session.get(AIRPORTS_URL, timeout=60)
            resp.raise_for_status()
            data = resp.content
            try:
                with open(AIRPORTS_FILE, "wb") as fh:
                    fh.write(data)
            except OSError as e:
                print(f"Couldn't save {AIRPORTS_FILE} ({e}); it will be downloaded again next start")
        except Exception as e:
            print(f"Couldn't download airports.csv ({e})" + ("; using the older copy" if exists else ""))
    try:
        if data is None:
            with open(AIRPORTS_FILE, "rb") as fh:
                data = fh.read()
    except OSError:
        print("No airport list: no takeoff airports can be found, so no routes will be shown")
        return []
    ALL_AIRPORTS[:] = [renamed({"code": r["iata_code"] or r["icao_code"] or r["ident"], "icao": r["icao_code"] or r["ident"],
                                "city": r["municipality"] or r["name"], "type": r["type"], "airline": r["scheduled_service"] == "yes",
                                "pos": (float(r["latitude_deg"]), float(r["longitude_deg"]))})
                       for r in csv.DictReader(io.StringIO(data.decode("utf-8")))
                       if r["type"] in ("large_airport", "medium_airport", "small_airport")]
    return [a for a in ALL_AIRPORTS if a["airline"]]


def nearest_airport(point, max_miles):
    """The airline airport closest to point, if one is within max_miles."""
    best = min(AIRPORTS, key=lambda a: distance_miles(*point, *a["pos"]), default=None)
    return best if best and distance_miles(*point, *best["pos"]) <= max_miles else None


def takeoff_airport(icao24):
    """The airport this aircraft's current flight took off from, if its path showed the takeoff (checked in the last 3h)."""
    checked = takeoff_cache.get(icao24)
    return checked[1] if checked and time.time() - checked[0] < 3 * 3600 else None


def takeoff_checked(icao24):
    checked = takeoff_cache.get(icao24)
    return bool(checked) and time.time() - checked[0] < 3 * 3600


def same_airport(a, b):
    return bool(a["icao"] and a["icao"] == b["icao"]) or distance_miles(*a["pos"], *b["pos"]) < 5


def best_route(p):
    """{"orig", "dest"} for this flight, or None.
    The departure must be confirmed twice: the airport within TAKEOFF_AIRPORT_MILES of where the flight's path
    shows it took off, AND at least one route database listing a flight from that airport. The arrival comes
    from that matching database (adsb.lol's leg leaving that airport first, then adsbdb). Then the 3 checks."""
    info = info_cache.get(p["callsign"])
    orig = takeoff_airport(p["icao24"])
    if not info or not orig:
        return None  # no route data, or no takeoff seen: not confirmed
    db, legs = info.get("adsbdb"), info.get("adsblol") or []
    dest = next((d for o, d in legs if same_airport(o, orig)), None)  # adsb.lol's leg leaving that airport
    if dest is None and db and same_airport(db[0], orig):
        dest = db[1]  # adsbdb's route from that airport
    if dest is None or same_airport(orig, dest) or not route_fits(p, orig["pos"], dest["pos"]):
        return None  # neither database matches the takeoff, or the route fails the checks
    return {"orig": orig, "dest": dest}


def qualifies(p):
    """The criteria: a commercial airline flight with a full, believable flight plan."""
    info = info_cache.get(p["callsign"])
    return bool(is_candidate(p["callsign"]) and info and info.get("iata") and best_route(p))


# ======================= map =======================
class MapView:
    """Maps lat/lon to pixels in rect: equal-distance scale, centered on (lat, lon), works across the date line."""

    def __init__(self, rect, center, scale):
        self.rect, self.inner = rect, rect.inflate(-2, -2)
        self.center, self.scale = center, scale  # scale: pixels per degree of latitude
        self.k = math.cos(math.radians(center[0]))

    @classmethod
    def around(cls, rect, center, radius_miles):
        """radius_miles fits from the center to the top and bottom edges."""
        return cls(rect, center, (rect.height - 4) / 2 / (radius_miles / MILES_PER_DEGREE))

    @classmethod
    def fitting(cls, rect, points, pad=8, min_radius_miles=20):
        """Everything in points fits inside, with pad pixels to spare."""
        ref_lon = points[0][1]
        lats = [p[0] for p in points]
        lons = [ref_lon + (p[1] - ref_lon + 180) % 360 - 180 for p in points]  # unwrap across the date line
        center = ((min(lats) + max(lats)) / 2, ((min(lons) + max(lons)) / 2 + 180) % 360 - 180)
        k = math.cos(math.radians(center[0]))
        span_y = max(max(lats) - min(lats), 2 * min_radius_miles / MILES_PER_DEGREE)
        span_x = max((max(lons) - min(lons)) * k, 2 * min_radius_miles / MILES_PER_DEGREE)
        inner = rect.inflate(-2, -2)
        return cls(rect, center, min((inner.width - 2 * pad) / span_x, (inner.height - 2 * pad) / span_y))

    def reach_miles(self):
        """Distance from the center to the map's corners."""
        return math.hypot(self.inner.width / 2, self.inner.height / 2) / self.scale * MILES_PER_DEGREE

    def contains(self, lat, lon, margin=0):
        x, y = self.project(lat, lon)
        return self.inner.inflate(-2 * margin, -2 * margin).collidepoint(round(x), round(y))

    def project(self, lat, lon):
        dlon = (lon - self.center[1] + 180) % 360 - 180
        return (self.inner.centerx + dlon * self.k * self.scale,
                self.inner.centery - (lat - self.center[0]) * self.scale)

    def unproject(self, px, py):
        return (self.center[0] - (py - self.inner.centery) / self.scale,
                self.center[1] + (px - self.inner.centerx) / self.scale / self.k)


def merc_x(lon, z):
    return (lon + 180) / 360 * 256 * 2 ** z


def merc_y(lat, z):
    s = math.sin(math.radians(max(-85, min(85, lat))))
    return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * 256 * 2 ** z


def get_tile(session, url, z, x, y, alpha=False):
    x %= 2 ** z  # wrap around the date line
    if not 0 <= y < 2 ** z:
        return None
    try:
        resp = session.get(url.format(z=z, x=x, y=y), timeout=8)
        if resp.status_code != 200:
            return None
        img = pygame.image.load(io.BytesIO(resp.content))
        return img.convert_alpha() if alpha else img.convert()
    except Exception:
        return None


def stitch(session, url, z, tx0, tx1, ty0, ty1, alpha=False):
    """Tiles joined into one image, or None if none downloaded."""
    canvas = pygame.Surface(((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), pygame.SRCALPHA if alpha else 0)
    got_any = False
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            tile = get_tile(session, url, z, tx, ty, alpha)
            if tile:
                canvas.blit(tile, ((tx - tx0) * 256, (ty - ty0) * 256))
                got_any = True
    return canvas if got_any else None


background_cache = {}


def get_background(session, view):
    """Satellite map for this view, downloaded once and kept for switching back."""
    if not SATELLITE_MAP or view is None:
        return None
    key = (tuple(view.rect), view.center, round(view.scale, 6), SHOW_ROADS)
    if key not in background_cache:
        if len(background_cache) > 20:
            background_cache.clear()
        background_cache[key] = build_background(session, view)
    return background_cache[key]


def build_background(session, view):
    """Satellite tiles for the map area, reduced to one color per LED, with roads drawn on top."""
    rect = view.inner
    lat_top, lon_left = view.unproject(rect.left, rect.top)
    lat_bot, lon_right = view.unproject(rect.right, rect.bottom)
    want = rect.width * 2 / max(lon_right - lon_left, 1e-6)
    z = max(2, min(13, math.ceil(math.log2(want * 360 / 256))))
    while True:
        x0, x1 = merc_x(lon_left, z), merc_x(lon_right, z)
        y0, y1 = merc_y(lat_top, z), merc_y(lat_bot, z)
        tx0, tx1, ty0, ty1 = int(x0 // 256), int(x1 // 256), int(y0 // 256), int(y1 // 256)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= 16 or z <= 2:
            break
        z -= 1

    canvas = stitch(session, TILE_URL, z, tx0, tx1, ty0, ty1)
    if not canvas:
        return None
    roads = stitch(session, ROADS_URL, z, tx0, tx1, ty0, ty1, alpha=True) if SHOW_ROADS else None

    crop = pygame.Rect(int(x0 - tx0 * 256), int(y0 - ty0 * 256), max(1, int(x1 - x0)), max(1, int(y1 - y0)))
    tall_h = rect.height * 4

    def shrink(img):  # crop to the map area, one pixel per LED across, 4 per LED down
        return pygame.transform.smoothscale(img.subsurface(crop.clip(img.get_rect())), (rect.width, tall_h))

    tall = shrink(canvas)
    tall_roads = shrink(roads) if roads else None
    rows = []  # map rows are even in latitude; Mercator tiles aren't
    for j in range(rect.height):
        lat, _ = view.unproject(rect.left, rect.top + j + 0.5)
        rows.append(max(0, min(tall_h - 1, int((merc_y(lat, z) - y0) / max(y1 - y0, 1e-6) * tall_h))))

    bg = pygame.Surface(rect.size)
    for y, row in enumerate(rows):
        for x in range(rect.width):
            r, g, b, _ = tall.get_at((x, row))
            avg = (r + g + b) / 3  # boost color, then dim so overlays stand out
            color = [max(0, min(255, (avg + (c - avg) * 1.6) * 0.8)) for c in (r, g, b)]
            if tall_roads:  # blend in the road color by how much road falls in this LED
                mix = min(1.0, tall_roads.get_at((x, row))[3] / 140)
                color = [c * (1 - mix) + rc * mix for c, rc in zip(color, COLOR_ROAD)]
            bg.set_at((x, y), [int(c) for c in color])
    return bg


# ======================= screen sections =======================
def draw_header(low, p, count, tracking=False):
    info = info_cache.get(p["callsign"], {})
    logo = logo_cache.get(info.get("iata"))
    if logo:
        ox, oy = 1, 1 + (LOGO_H - logo["h"]) // 2
        for px, py, color in logo["pixels"]:
            low.fill(color, (ox + px, oy + py, 1, 1))
        text_x = max(38, 1 + logo["w"] + 4)
    else:  # no logo: gray box with the operator code
        low.fill(COLOR_LINE, (1, 1, LOGO_H, LOGO_H))
        draw_text_center(low, p["callsign"][:3], 1 + LOGO_H // 2, 14, COLOR_TEXT)
        text_x = 38

    # Right side: flight count and radius on top, distance from you below
    labels = ["TRACKING"] if tracking else [f"{count} FLIGHT{'S' if count != 1 else ''}", f"({RADIUS_MILES:g}MI RADIUS)"]
    dist = f"{p['dist']:.1f}MI" if p["dist"] < 100 else f"{p['dist']:,.0f}MI"
    block_left = GRID_W - 2 - max([text_width(dist, 2)] + [text_width(t) for t in labels])
    for i, t in enumerate(labels):
        draw_text_right(low, t, GRID_W - 2, 3 + i * 8, COLOR_LABEL)
    draw_text_right(low, dist, GRID_W - 2, 20, COLOR_TEXT, 2)

    # Left side: flight number lined up with the top of the flight count (rows 3-23), as wide as fits;
    # airline name below it, 9 LEDs tall, its bottom lined up with the distance (rows 25-33)
    scale = next((s for s in (4, 3) if text_x + text_width(p["callsign"], s) <= block_left - 4), 2)
    draw_text(low, p["callsign"], text_x, 3, COLOR_CALLSIGN, scale, [3] * 7)
    sub = info.get("airline") or p["callsign"][:3]
    draw_text(low, fit(sub, (block_left - 6 - text_x) // 6), text_x, 25, COLOR_TEXT, 1, [1, 1, 2, 1, 2, 1, 1])


def clock(t):
    """2:14PM"""
    return time.strftime("%I:%M%p", time.localtime(t)).lstrip("0")


def duration(secs):
    """1H 05M, or 12 MIN under an hour."""
    h, m = divmod(max(0, round(secs / 60)), 60)
    return f"{h}H {m:02d}M" if h else f"{m} MIN"


TIGHT_SPACE = 2  # route section's middle lines: extra blank LEDs for a space (letters are 1 LED apart there)


def glyph_cols(ch):
    """(first, last) lit column of a character, for the narrow lettering of draw_text_fitting."""
    cols = [c for c in range(5) if any(row[c] == "#" for row in GLYPHS.get(ch, GLYPHS[" "]))]
    return (cols[0], cols[-1]) if cols else (0, -1)


def tight_width(text):
    """Width of text in narrow lettering (see draw_text_fitting)."""
    w = 0
    for ch in clean(text):
        first, last = glyph_cols(ch)
        w += TIGHT_SPACE if ch == " " else last - first + 2
    return max(0, w - 1)


def draw_text_fitting(low, options, center, y, width, color):
    """The first of these texts that fits in width (the last one if none do), centered. Narrow lettering:
    each character is only as wide as its lit LEDs, 1 LED apart, so more fits between the airport codes."""
    text = next((t for t in options if tight_width(t) <= width), options[-1])
    x = center - tight_width(text) // 2
    for ch in clean(text):
        if ch == " ":
            x += TIGHT_SPACE
            continue
        first, last = glyph_cols(ch)
        draw_icon(low, GLYPHS.get(ch, GLYPHS[" "]), x - first, y, color)
        x += last - first + 2


def path_miles(trail):
    """Length of a recorded flight path in miles, not counting taxiing. Cached in the trail and extended
    as new positions are added, so it isn't re-summed every frame."""
    pts = trail["pts"]
    done, miles = trail.get("miles_n", 0), trail.get("miles", 0.0)
    if done > len(pts):  # the path was replaced: start over
        done, miles = 0, 0.0
    for a, b in zip(pts[max(0, done - 1):], pts[max(1, done):]):
        if "ground" not in (a[3:4] + b[3:4]):
            miles += distance_miles(a[0], a[1], b[0], b[1])
    trail["miles_n"], trail["miles"] = len(pts), miles
    return miles


def miles_flown(p, trail, now, orig=None):
    """How far the flight has gone since takeoff: its recorded path plus the bit since the last position.
    If the path doesn't start at the takeoff (or there's none), the straight line from the departure airport
    covers the missing part. None if neither is known."""
    here = current_position(p, now)
    pts = trail["pts"] if trail else []
    if not pts:
        return distance_miles(*orig["pos"], *here) if orig else None
    miles = path_miles(trail) + distance_miles(pts[-1][0], pts[-1][1], *here)
    if orig and p["icao24"] not in departure_cache:
        miles += distance_miles(*orig["pos"], pts[0][0], pts[0][1])
    return miles


def eta(p, now):
    """(seconds to go, arrival time) for a flight with a route, or None if it's too slow to estimate."""
    route = best_route(p)
    mph = p["mph"] or 0
    if not route or mph <= 80:
        return None
    left = distance_miles(*current_position(p, now), *route["dest"]["pos"])
    secs = left / mph * 3600 + ETA_BUFFER_MINUTES * 60  # straight-line time at this speed, plus landing
    return secs, now + secs


def departed(p, now):
    """When its current flight took off, or None if unknown."""
    dep = departure_cache.get(p["icao24"])
    return dep if dep is not None and dep <= now else None


def short_duration(secs):
    """1H32M, or 45M under an hour."""
    h, m = divmod(max(0, round(secs / 60)), 60)
    return f"{h}H{m:02d}M" if h else f"{m}M"


def progress_options(flown, pct, left):
    """Top line, longest first: 351MI (16%), 1,786MI LEFT. left (and pct) are None without a destination."""
    f, nf = f"{flown:,.0f}", f"{flown:.0f}"
    if left is None:
        return [f"{f}MI FLOWN", f"{f}MI"]
    l, nl = f"{left:,.0f}", f"{left:.0f}"
    return [f"{f}MI ({pct}%), {l}MI LEFT", f"{nf}MI ({pct}%), {nl}MI LEFT", f"{nf}MI ({pct}%), {nl} LEFT",
            f"{nf}MI {pct}%, {nl} LEFT", f"{nf}MI ({pct}%)"]


def times_options(dep, arrive, now):
    """Bottom line, longest first: IN AIR 1H32M, 6:18PM > 10:08PM. Either time may be None."""
    up = f"IN AIR {short_duration(now - dep)}" if dep is not None else None
    d = clock(dep) if dep is not None else None
    a = clock(arrive) if arrive is not None else None
    if up and a:
        return [f"{up}, {d} > {a}", f"{up}, {d}>{a}", f"{d} > {a}"]
    if up:
        return [f"{up}, DEP {d}", f"{up}, {d}", up]
    if a:
        return [f"ETA {a}"]
    return ["DEPARTURE TIME UNKNOWN", "DEP UNKNOWN"]


def draw_route(low, p, now, trail=None):
    top = 40
    route = best_route(p)
    if not route:
        took_off = takeoff_airport(p["icao24"])
        if took_off:  # departure known from the takeoff, arrival not confirmed
            draw_text(low, took_off["code"], 2, top, COLOR_AIRPORT, 3)
            draw_text(low, fit(took_off["city"], 20), 2, top + 24, COLOR_TEXT)
            left = 2 + text_width(took_off["code"], 3) + 8
            mid_x, width = (left + GRID_W - 2) // 2, GRID_W - 2 - left
            flown = miles_flown(p, trail, now, took_off)
            dep = departed(p, now)
            draw_text_center(low, "ARRIVAL UNKNOWN", mid_x, top, COLOR_LABEL)
            if flown is not None:
                draw_text_fitting(low, progress_options(flown, None, None), mid_x, top + 8, width, COLOR_TEXT)
            draw_text_fitting(low, times_options(dep, None, now), mid_x, top + 16, width, COLOR_TEXT)
        else:
            draw_text_center(low, "ROUTE NOT AVAILABLE", GRID_W // 2, 52, COLOR_LABEL)
        return
    orig, dest = route["orig"], route["dest"]
    draw_text(low, orig["code"], 2, top, COLOR_AIRPORT, 3)
    draw_text_right(low, dest["code"], GRID_W - 2, top, COLOR_AIRPORT, 3)
    draw_text(low, fit(orig["city"], 20), 2, top + 24, COLOR_TEXT)
    draw_text_right(low, fit(dest["city"], 20), GRID_W - 2, top + 24, COLOR_TEXT)

    x0 = 2 + text_width(orig["code"], 3) + 3
    x1 = GRID_W - 2 - text_width(dest["code"], 3) - 3
    mid_x = (x0 + x1) // 2
    lat, lon = current_position(p, now)
    left = distance_miles(lat, lon, *dest["pos"])
    flown = miles_flown(p, trail, now, orig)  # the miles actually flown, turns and all
    frac = min(1.0, max(0.0, flown / (flown + left))) if flown + left > 0 else 0.0  # matches the miles shown

    bar_y = top + 10
    icon_w = len(PLANE_ICON[0])
    px = x0 + round(frac * (x1 - x0 - icon_w))
    low.fill(COLOR_AIRPORT, (x0, bar_y, max(0, px - x0), 1))
    for x in range(px + icon_w + 1, x1, 2):
        low.fill(COLOR_LINE, (x, bar_y, 1, 1))
    draw_icon(low, PLANE_ICON, px, bar_y - 3, COLOR_TEXT)
    dep, arrival = departed(p, now), eta(p, now)
    draw_text_fitting(low, progress_options(flown, round(frac * 100), left), mid_x, top, x1 - x0, COLOR_LABEL)
    draw_text_fitting(low, times_options(dep, arrival[1] if arrival else None, now), mid_x, top + 16, x1 - x0,
                      COLOR_TEXT)


def short_aircraft(p):
    """BOEING 737-800 -> 737-800 so it fits the 10-character column."""
    desc = clean(p["desc"])
    if len(desc) > 10:
        desc = desc.split(" ", 1)[-1]
    if not desc or len(desc) > 10:
        desc = clean(p["type"])
    return desc or "---"


def trend_color(t):
    return COLOR_UP if t > 0 else COLOR_DOWN if t < 0 else COLOR_TEXT


def draw_data(low, p, now):
    rows = [  # altitude and speed: green if up since the last refresh, orange if down, white if unchanged
        ("ALTITUDE", "ON GROUND" if p["ground"] else f"{p['alt']:,} FT" if p["alt"] is not None else "---",
         COLOR_TEXT if p["ground"] else trend_color(p["alt_trend"])),
        ("SPEED", f"{p['mph']:.0f} MPH" if p["mph"] else "---", trend_color(p["mph_trend"])),
    ]
    vr = p["vrate"]
    if vr is not None and vr >= 300:
        rows.append(("CLIMBING", f"{vr:+,.0f} FPM", COLOR_UP))
    elif vr is not None and vr <= -300:
        rows.append(("DESCENDING", f"{vr:+,.0f} FPM", COLOR_DOWN))
    else:
        rows.append(("VERT RATE", "CRUISING" if (p["alt"] or 0) >= 18000 else "LEVEL", COLOR_TEXT))
    rows += [
        ("HEADING", f"{p['track']:.0f} {compass(p['track'])}" if p["track"] is not None else "---", COLOR_TEXT),
        ("AIRCRAFT", short_aircraft(p), COLOR_TEXT),
        ("TAIL NO.", fit(p["reg"], 10) or "---", COLOR_TEXT),
        ("SQUAWK", p["squawk"] or "---", COLOR_TEXT),
    ]
    for i, (label, value, color) in enumerate(rows):
        draw_text(low, label, 2, 77 + i * 9, COLOR_LABEL)
        draw_text(low, value, 66, 77 + i * 9, color)


def draw_map_base(low, view, background):
    if background:
        low.blit(background, view.inner.topleft)
    else:
        for y in range(view.rect.top + 4, view.rect.bottom - 2, 6):
            for x in range(view.rect.left + 4, view.rect.right - 2, 6):
                low.fill(COLOR_GRID, (x, y, 1, 1))
    pygame.draw.rect(low, COLOR_LINE, view.rect, 1)


def draw_home(low, view, point=None):
    """The green marker at point (default: the radar and map center)."""
    hx, hy = (round(v) for v in view.project(*(point or CENTER_COORDS)))
    low.fill((0, 0, 0), (hx - 3, hy - 1, 7, 3))
    low.fill((0, 0, 0), (hx - 1, hy - 3, 3, 7))
    low.fill(COLOR_HOME, (hx - 2, hy, 5, 1))
    low.fill(COLOR_HOME, (hx, hy - 2, 1, 5))


def draw_heading(low, view, p, here, color):
    """Dots from the plane to its destination, if it has a believable route."""
    route = best_route(p)
    if route:
        draw_dotted_path(low, view, great_circle(here, route["dest"]["pos"]), color, 4)


NO_TRAIL = {"pts": [], "downloaded": False}


def draw_map(low, view, background, p, now, extras=(), trails=None, airports=(), helis=()):
    """Radar map: the closest flight (p) and any extras, each with where it has flown and where it's heading,
    plus helicopters when heli_only is on."""
    trails = trails or {}
    draw_map_base(low, view, background)
    low.set_clip(view.inner)
    for e in extras:  # next-closest flights: sparser dots and a white plane, under the main one
        if best_route(e):
            behind, ahead = flight_path(e, trails.get(e["icao24"], NO_TRAIL), now)
            draw_dotted_path(low, view, behind, COLOR_EXTRA, 4)
            draw_dotted_path(low, view, ahead, COLOR_EXTRA, 4)
        ex, ey = (round(v) for v in view.project(*current_position(e, now)))
        draw_outlined_icon(low, PLANE_ICON, ex, ey, (e["track"] or 0) - 90, aircraft_color(e))
    route = best_route(p) if p else None
    if route:  # dot trails: behind the plane (where it has flown) and ahead of it (to the destination)
        trail_color = COLOR_TEXT if background else COLOR_LABEL
        behind, ahead = flight_path(p, trails.get(p["icao24"], NO_TRAIL), now)
        draw_dotted_path(low, view, behind, trail_color, 2)
        draw_dotted_path(low, view, ahead, trail_color, 4)
    draw_home(low, view)
    route_airports = [a for a in ((route["dest"], route["orig"]) if route and SHOW_AIRPORTS else ())
                      if view.contains(*a["pos"], margin=3)]  # fully inside, not cut off at the edge
    planes = [q for q in (p, *extras) if q]
    draw_airports(low, view, airports, [plane_box(view, q, now) for q in planes] +
                  [plane_box(view, h, now, HELICOPTER_DOT) for h in helis], route_airports)
    for h in helis:
        hx, hy = (round(v) for v in view.project(*current_position(h, now)))
        draw_outlined_icon(low, HELICOPTER_DOT, hx, hy, 0, aircraft_color(h))
    if p:
        px, py = (round(v) for v in view.project(*current_position(p, now)))
        if view.inner.collidepoint(px, py):
            draw_outlined_icon(low, PLANE_ICON, px, py, (p["track"] or 0) - 90, COLOR_AIRPORT)
    low.set_clip(None)


def render(low, view, background, selected, count, now, extras=(), trails=None, airports=(), helis=()):
    """selected: the plane to show in detail (or None, which shows just the map). count: planes in the radius.
    extras: more flights to show on the map. trails: their flight paths."""
    low.fill(COLOR_OFF)
    if not selected:
        draw_map(low, view, background, None, now, airports=airports, helis=helis)
        return
    draw_header(low, selected, count)
    low.fill(COLOR_LINE, (0, 36, GRID_W, 1))
    draw_route(low, selected, now, (trails or {}).get(selected["icao24"]))
    low.fill(COLOR_LINE, (0, 73, GRID_W, 1))
    draw_data(low, selected, now)
    draw_map(low, view, background, selected, now, extras, trails, airports, helis)


# ======================= tracking mode =======================
def flight_path(p, trail, now):
    """(behind, ahead): where the plane has been (its recorded path, or the shortest path from the departure
    airport where there's no recording), and the shortest path on to its destination, if the route is known."""
    here = current_position(p, now)
    info = best_route(p)
    behind = [(lat, lon) for lat, lon, *_ in trail["pts"]] + [here]
    if info and not trail["downloaded"]:  # no downloaded path: assume the shortest path from the origin
        behind = great_circle(info["orig"]["pos"], behind[0]) + behind
    ahead = great_circle(here, info["dest"]["pos"]) if info else []
    return behind, ahead


def tracking_view(view, rect, p, trail, now):
    """Keep the current map unless the flight no longer fits; then make one that shows the whole route
    (without a route: the takeoff airport and the path since, or else the plane with TRACK_NO_ROUTE_RADIUS_MILES)."""
    here = current_position(p, now)
    info = best_route(p)
    if info:
        behind, ahead = flight_path(p, trail, now)
        points = [info["orig"]["pos"], info["dest"]["pos"]] + behind + ahead
        if view and all(view.contains(*pt, margin=2) for pt in points[::10] + points[:2]):
            return view
        return MapView.fitting(rect, points)
    took_off = takeoff_airport(p["icao24"])
    if took_off:  # no route, but we know where it took off: show that airport and the path flown since
        behind, _ = flight_path(p, trail, now)
        points = [took_off["pos"], here] + behind
        if view and all(view.contains(*pt, margin=2) for pt in points[::10] + points[:2]):
            return view
        return MapView.fitting(rect, points, min_radius_miles=50)
    if view and distance_miles(*view.center, *here) < TRACK_NO_ROUTE_RADIUS_MILES / 4:
        return view
    return MapView.around(rect, here, TRACK_NO_ROUTE_RADIUS_MILES)


def draw_text_outlined(surf, text, x, y, color):
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw_text(surf, text, x + dx, y + dy, (0, 0, 0))
    draw_text(surf, text, x, y, color)


def plane_box(view, p, now, icon=PLANE_ICON):
    """The LEDs an aircraft icon (any rotation, with its black outline) can cover on the map."""
    x, y = (round(v) for v in view.project(*current_position(p, now)))
    r = max(len(icon), len(icon[0])) // 2 + 2
    return pygame.Rect(x - r, y - r, 2 * r + 1, 2 * r + 1)


def map_airports(view, map_mode):
    """Map mode: the airports to show on its map, per airports= / airfields=, biggest first.
    Radar and tracking only ever show their route's airports, so this is empty for them."""
    show_airports, show_airfields = MAP_SHOW_AIRPORTS, MAP_SHOW_AIRFIELDS
    if not map_mode or view is None or not (show_airports or show_airfields):
        return []
    order = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
    return sorted((a for a in ALL_AIRPORTS if (show_airports if a["airline"] else show_airfields)
                   and view.contains(*a["pos"], margin=3)), key=lambda a: (order[a["type"]], not a["airline"]))


def draw_airports(low, view, airports, avoid, route=()):
    """Dots and codes: the route's airports (always full size and labeled first), then the map's other airports
    (airline airports in cyan, airfields in gray, biggest first). All dots go down before any code, so codes are
    never covered; a code is left out if it would touch an aircraft icon (avoid) or a code already drawn.
    On crowded maps (more than CROWDED_AIRPORTS), the other airports shrink to single LEDs and only large
    airports keep their codes, so the map stays readable."""
    route_icao = {a["icao"] for a in route}
    others = [a for a in airports if a["icao"] not in route_icao]
    crowded = len(others) > CROWDED_AIRPORTS

    def dot(a, color, small):
        x, y = (round(v) for v in view.project(*a["pos"]))
        if small:
            low.fill(color, (x, y, 1, 1))
        else:
            low.fill((0, 0, 0), (x - 2, y - 2, 5, 5))
            low.fill(color, (x - 1, y - 1, 3, 3))

    for a in reversed(others):  # least important first, so important dots end up on top
        dot(a, COLOR_AIRPORT if a["airline"] else COLOR_SMALL_AIRPORT, crowded)
    for a in route:
        dot(a, COLOR_AIRPORT, False)

    avoid, labels = list(avoid), 0
    for a in [*route, *(o for o in others if not crowded or o["type"] == "large_airport")]:
        if labels >= MAX_AIRPORT_LABELS:
            break
        x, y = (round(v) for v in view.project(*a["pos"]))
        code = clean(a["code"])
        tx = max(view.inner.left + 1, min(view.inner.right - 1 - text_width(code), x - text_width(code) // 2))
        ty = y - 11 if y - 11 >= view.inner.top + 1 else y + 4  # above the dot, or below it at the top edge
        box = pygame.Rect(tx - 1, ty - 1, text_width(code) + 2, 9)
        if box.collidelist(avoid) == -1:
            draw_text_outlined(low, code, tx, ty, COLOR_AIRPORT if a.get("airline", True) else COLOR_SMALL_AIRPORT)
            avoid.append(box)
            labels += 1


def draw_tracking_map(low, view, background, p, trail, now, airports=()):
    draw_map_base(low, view, background)
    low.set_clip(view.inner)
    trail_color = COLOR_TEXT if background else COLOR_LABEL
    behind, ahead = flight_path(p, trail, now)
    draw_dotted_path(low, view, behind, trail_color, 2)
    draw_dotted_path(low, view, ahead, trail_color, 4)
    draw_home(low, view, HOME_COORDS)  # tracking ignores set_coords=: always the .env location
    info = best_route(p)
    took_off = takeoff_airport(p["icao24"])
    route_airports = [] if not SHOW_AIRPORTS else [info["orig"], info["dest"]] if info else [took_off] if took_off else []
    draw_airports(low, view, airports, [plane_box(view, p, now)], route_airports)
    px, py = (round(v) for v in view.project(*current_position(p, now)))
    draw_outlined_icon(low, PLANE_ICON, px, py, (p["track"] or 0) - 90, COLOR_AIRPORT)
    low.set_clip(None)


def render_tracking(low, view, background, p, trail, flight, now, airports=()):
    low.fill(COLOR_OFF)
    if not p:
        draw_text_center(low, flight, GRID_W // 2, 40, COLOR_CALLSIGN, 3)
        draw_text_center(low, "NOT BROADCASTING RIGHT NOW", GRID_W // 2, 72, COLOR_LABEL)
        draw_text_center(low, "(LANDED, NOT DEPARTED, OR OUT OF RANGE)", GRID_W // 2, 84, COLOR_LABEL)
        return
    draw_header(low, p, 1, tracking=True)
    low.fill(COLOR_LINE, (0, 36, GRID_W, 1))
    draw_route(low, p, now, trail)
    low.fill(COLOR_LINE, (0, 73, GRID_W, 1))
    draw_data(low, p, now)
    draw_tracking_map(low, view, background, p, trail, now, airports)


# ======================= map mode =======================
def aircraft_color(p, radar_flight=False):
    """Orange for flagged aircraft, red for military, blue for the flight radar mode shows (or would show), else white."""
    return (COLOR_FLAGGED_AIRCRAFT if is_flagged_aircraft(p) else COLOR_MILITARY if p["military"]
            else COLOR_AIRPORT if radar_flight else COLOR_TEXT)


def is_flagged_aircraft(p):
    names = {n.strip().upper() for n in FLAGGED_AIRCRAFT}
    return p["callsign"] in names or p["reg"].upper() in names


def is_helicopter(p):
    return (p["type"].upper() in HELICOPTER_TYPES or p["category"] == "A7"
            or any(w in clean(p["desc"]) for w in HELICOPTER_WORDS))


def is_large(p):
    """Large/heavy by the broadcast size category; without one, assume airline flights are large."""
    return p["category"] in ("A3", "A4", "A5") if p["category"] else is_candidate(p["callsign"])


def fetch_trace(session, icao24, trim_far=True):
    """Where the aircraft has been on its current flight, as [(lat, lon, time, altitude)]. [] if unavailable.
    Also notes which airport the flight took off from, if the path starts on (or near) the ground there."""
    for url in TRACE_URLS:
        host = url.split("/")[2]
        try:
            resp = get_politely(session, url.format(folder=icao24[-2:], hex=icao24))
            if resp is None:
                continue
            data = resp.content
            body = json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
            start, rows = body["timestamp"], body["trace"]
        except Exception as e:
            feed_failed(host, f"flight path unavailable ({e})")
            continue
        pts = []
        for row in rows:
            t = start + row[0]
            if pts and ((row[6] or 0) & 2 or t - pts[-1][2] > 1200):
                pts = []  # a new flight started (or a 20-minute gap): drop the earlier one
            if row[1] is not None and row[2] is not None:
                pts.append((row[1], row[2], t, row[3]))
        first_alt = pts[0][3] if pts else None
        took_off = first_alt == "ground" or (isinstance(first_alt, (int, float)) and first_alt < 2500)
        takeoff_cache[icao24] = (time.time(), nearest_airport(pts[0][:2], TAKEOFF_AIRPORT_MILES) if took_off else None)
        if took_off:  # wheels up: the first point off the ground
            departure_cache[icao24] = next((t for _, _, t, alt in pts if alt != "ground"), None)
        else:
            departure_cache.pop(icao24, None)  # the path starts in the air: the takeoff time isn't known
        if trim_far:  # skip the part far away from here; it's off the map anyway (map mode)
            pts = pts[next((i for i, (lat, lon, *_) in enumerate(pts) if distance_miles(*CENTER_COORDS, lat, lon) < 150), len(pts)):]
        return pts
    takeoff_cache[icao24] = (time.time() - 3 * 3600 + 600, None)  # unavailable: check again in 10 minutes
    return []


def update_trails(session, trails, planes, view):
    """Flight paths for every aircraft on the map: downloaded once, then extended with each new position.
    Erased when the aircraft leaves the map (or isn't seen for a minute)."""
    now, downloads = time.time(), 0
    for p in planes:  # nearest first
        if not view.contains(p["lat"], p["lon"]):
            trails.pop(p["icao24"], None)
            continue
        trail = trails.get(p["icao24"])
        if trail is None:
            if downloads >= MAX_TRACES:
                continue  # the rest get their paths on the next refresh
            downloads += 1
            trail = trails[p["icao24"]] = {"pts": fetch_trace(session, p["icao24"])}
        trail["seen"] = now
        trail["pts"].append((p["lat"], p["lon"], now))
    for icao in [i for i, t in trails.items() if now - t["seen"] > 60]:
        del trails[icao]


def map_icon(p):
    return HELICOPTER_DOT if is_helicopter(p) else PLANE_ICON if is_large(p) else SMALL_PLANE_ICON


def render_map_mode(low, view, background, planes, trails, now, airports=(), radar_pick=None):
    """Full-screen radar: every aircraft, each with where it has been and where it's heading, plus the
    airports and airfields on the map (per the airports= / airfields= settings)."""
    low.fill(COLOR_OFF)
    draw_map_base(low, view, background)
    low.set_clip(view.inner)
    trail_color = COLOR_TEXT if background else COLOR_LABEL
    for p in planes:
        here = current_position(p, now)
        if not view.contains(*here):
            continue  # not on the map (yet, or anymore): no paths
        trail = trails.get(p["icao24"])
        if trail:
            draw_dotted_path(low, view, [(lat, lon) for lat, lon, *_ in trail["pts"]] + [here], trail_color, 2)
        draw_heading(low, view, p, here, trail_color)
    draw_airports(low, view, airports, [plane_box(view, p, now, map_icon(p)) for p in planes])
    draw_home(low, view)
    for p in reversed(planes):  # nearest drawn last, on top
        x, y = (round(v) for v in view.project(*current_position(p, now)))
        color = aircraft_color(p, p["icao24"] == radar_pick)
        icon = map_icon(p)
        draw_outlined_icon(low, icon, x, y, 0 if icon is HELICOPTER_DOT else (p["track"] or 0) - 90, color)
    low.set_clip(None)


# ======================= window =======================
def build_dot_mask(dot):
    """Black overlay with round holes so each LED looks like a separate dot."""
    mask = pygame.Surface((GRID_W * dot, GRID_H * dot), pygame.SRCALPHA)
    mask.fill((0, 0, 0, 255))
    radius = max(1, int(dot * 0.45))
    for gy in range(GRID_H):
        for gx in range(GRID_W):
            pygame.draw.circle(mask, (0, 0, 0, 0), (gx * dot + dot // 2, gy * dot + dot // 2), radius)
    return mask


def open_window(fullscreen):
    if fullscreen:
        pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        return
    desk_w, desk_h = pygame.display.get_desktop_sizes()[0]
    w = int(min(desk_w * 0.85, desk_h * 0.85 * GRID_W / GRID_H))
    pygame.display.set_mode((w, w * GRID_H // GRID_W), pygame.RESIZABLE)


class Mode:
    """Everything for one mode: "radar", "map", or a flight number to track."""

    def __init__(self, session, mode):
        self.name = mode  # "radar", "map" or the flight number, for commands that only change options
        self.map_mode = mode == "map"
        self.flight = None if mode in ("radar", "map") else clean(mode)
        self.panel = pygame.Rect(130, 76, GRID_W - 132, GRID_H - 78)  # map area on the detail screens
        if self.map_mode:
            self.view = MapView.around(pygame.Rect(0, 0, GRID_W, GRID_H), CENTER_COORDS, MAP_RADIUS_MILES)
            self.radius = math.ceil(self.view.reach_miles())  # fetch everything visible, corners included
        elif self.flight:
            self.view, self.radius = None, None  # made to fit the flight once it's found
        else:
            self.view, self.radius = MapView.around(self.panel, CENTER_COORDS, RADIUS_MILES), RADIUS_MILES
        self.background = get_background(session, self.view)
        self.airports = map_airports(self.view, self.map_mode)  # found once per map, not every redraw
        self.options = callsign_options(session, self.flight) if self.flight else None
        self.trails = {}  # flight paths
        self.planes, self.selected, self.extras, self.last_fetch = [], None, [], 0.0
        self.radar_pick = None  # map mode: the flight radar mode would show, drawn in blue
        self.helis = []  # radar with heli_only: the helicopters on its map
        pygame.display.set_caption(f"Tracking {self.flight}" if self.flight else "Map" if self.map_mode else "Radar")

    def update(self, session):
        """Download new data every REFRESH_SECONDS."""
        now = time.time()
        if now - self.last_fetch < REFRESH_SECONDS:
            return
        self.last_fetch = now
        try:
            if self.flight:
                self.selected = fetch_tracked(session, self.options)
                if self.selected:
                    note_trends([self.selected])
                if self.selected:
                    self.options = [self.selected["callsign"]]  # found it: stop trying the other spellings
                    track_flight(session, self.selected, self.trails)
                    view = tracking_view(self.view, self.panel, self.selected, self.trails[self.selected["icao24"]], now)
                    if view is not self.view:
                        self.view, self.background = view, get_background(session, view)
            else:
                planes = fetch_aircraft(session, self.radius)
                if self.map_mode:
                    fetched = planes
                    planes = [q for q in planes if self.view.contains(q["lat"], q["lon"])
                              and (is_helicopter(q) or not MAP_HELI_ONLY)][:MAP_NUM_FLIGHTS]
                    update_trails(session, self.trails, planes, self.view)
                    # the flight radar mode would show right now: the nearest airborne one with a route
                    self.radar_pick = next((q["icao24"] for q in fetched if not q["ground"] and
                                            q["dist"] <= RADIUS_MILES and qualifies(q)), None)
                else:
                    planes = [q for q in planes if not q["ground"]]
                    note_trends(planes)
                update_lookups(session, planes)
                self.planes = planes
        except ConnectionError as e:
            print(e)  # keep showing the last good data
        if not self.map_mode and not self.flight:
            # num_flights counts the selected flight plus extras; with heli_only, the selected flight plus helicopters
            wanted = NUM_FLIGHTS if not HELI_ONLY else (1 if NUM_FLIGHTS is None else min(1, NUM_FLIGHTS))
            shown, checks = [], 0
            for q in self.planes:  # nearest first
                if wanted is not None and len(shown) >= wanted:
                    break
                if not info_cache.get(q["callsign"]):
                    continue
                pts = None
                if not takeoff_checked(q["icao24"]) and checks < MAX_TRACES:
                    checks += 1  # its flight path shows where it took off, confirming the route's origin
                    pts = fetch_trace(session, q["icao24"], trim_far=False)
                if qualifies(q):
                    shown.append(q)
                    if q["icao24"] not in self.trails:  # its path, for the trail on the map
                        if pts is None:
                            pts = fetch_trace(session, q["icao24"], trim_far=False)
                        self.trails[q["icao24"]] = {"pts": pts, "downloaded": bool(pts)}
                    self.trails[q["icao24"]]["pts"].append((q["lat"], q["lon"], now))
            nearby = {q["icao24"] for q in self.planes}
            for icao in [i for i in self.trails if i not in nearby]:
                del self.trails[icao]  # left the radius: forget its path
            self.selected, self.extras = (shown[0] if shown else None), shown[1:]
            self.helis = []
            if HELI_ONLY:  # the rest of num_flights: the closest helicopters (all of it if no flight is selected)
                helis = [q for q in self.planes if is_helicopter(q) and q is not self.selected]
                self.helis = helis if NUM_FLIGHTS is None else helis[:max(0, NUM_FLIGHTS - len(shown))]

    def blue_flight(self):
        """The flight drawn in blue: radar's closest flight, or in map mode the flight radar would show
        (if it's on the map). None in tracking mode, or if there isn't one."""
        if self.flight:
            return None
        if self.map_mode:
            return next((q for q in self.planes if q["icao24"] == self.radar_pick), None)
        return self.selected

    def plane_at(self, point):
        """(aircraft, x, y) for the aircraft icon at this LED on the map, or None.
        Map mode: any aircraft; radar: the flights on its map; tracking: none."""
        if self.flight or not self.view:
            return None
        planes = self.planes if self.map_mode else [q for q in (self.selected, *self.extras, *self.helis) if q]
        now, best = time.time(), None
        for p in planes:
            x, y = self.view.project(*current_position(p, now))
            d = math.hypot(x - point[0], y - point[1])
            if d <= 6 and self.view.inner.collidepoint(round(x), round(y)) and (best is None or d < best[0]):
                best = (d, p, round(x), round(y))
        return best[1:] if best else None

    def draw_hover(self, low, point):
        """Map and radar mode: the flight number (or tail number) of the aircraft under the mouse,
        kept inside the map."""
        found = self.plane_at(point)
        if found:
            p, x, y = found
            text, box = clean(p["callsign"] or p["reg"] or p["icao24"]), self.view.inner
            tx = max(box.left + 1, min(box.right - 1 - text_width(text), x - text_width(text) // 2))
            ty = y - 14 if y - 14 >= box.top + 1 else y + 8  # above the icon, or below it at the top edge
            draw_text_outlined(low, text, tx, ty, COLOR_CALLSIGN)

    def render(self, low):
        now = time.time()
        if self.flight:
            trail = self.trails.get(self.selected["icao24"]) if self.selected else None
            render_tracking(low, self.view, self.background, self.selected, trail, self.flight, now, self.airports)
        elif self.map_mode:
            render_map_mode(low, self.view, self.background, self.planes, self.trails, now, self.airports,
                         self.radar_pick)
        else:
            render(low, self.view, self.background, self.selected, len(self.planes), now, self.extras, self.trails,
                   self.airports, self.helis)


def draw_command_bar(low, text, color, typing=False):
    """A line of text along the bottom of the screen: the command being typed (its end stays visible), or a message."""
    max_chars = (GRID_W - 4) // 6
    text = clean(text)[-max_chars:] if typing else fit(text, max_chars)
    low.fill((0, 0, 0), (0, GRID_H - 11, GRID_W, 11))
    low.fill(COLOR_LINE, (0, GRID_H - 12, GRID_W, 1))
    draw_text(low, text, 2, GRID_H - 9, color)


def read_terminal(commands):
    """Commands typed in the terminal while the display runs (runs in the background)."""
    try:
        for line in sys.stdin:
            commands.put(line)
    except Exception:
        pass  # no terminal attached


def run_command(session, text, current):
    """New Mode for a typed command, or a message (str) explaining what's wrong.
    A command naming a mode starts that mode from the defaults. A command with only options
    (like "r=30" or "roads=true") changes just those options in the current mode."""
    words = text.split()
    for i, w in enumerate(words):  # allow the full "py flights.py map ..." too
        if w.lower().endswith(".py"):
            words = words[i + 1:]
            break
    try:
        mode, settings = parse_command(words)
        if mode is None:  # options only: keep the current mode and its other settings
            mode = current.name
            settings = parse_command(words, {name: globals()[name] for name in OPTION_SETTINGS})[1]
    except ValueError as e:
        return str(e)
    if mode == "help":
        return "RADAR/MAP/FLIGHT R= ROADS= NUM_FLIGHTS="
    globals().update(settings)
    return Mode(session, mode)


def current_radius(mode):
    r = MAP_RADIUS_MILES if mode.map_mode else TRACK_NO_ROUTE_RADIUS_MILES if mode.flight else RADIUS_MILES
    return f"{r:g}"


def current_count(mode):
    if mode.flight:
        return None  # tracking always shows one flight
    n = MAP_NUM_FLIGHTS if mode.map_mode else NUM_FLIGHTS
    return "all" if n is None else n


def main(mode="radar"):
    pygame.display.init()
    fullscreen = START_FULLSCREEN
    open_window(fullscreen)
    scene = pygame.Surface((GRID_W, GRID_H))  # the mode's picture, redrawn 4 times a second
    low = pygame.Surface((GRID_W, GRID_H))    # what's shown: the picture plus the command bar
    mask, mask_dot = None, None

    session = requests.Session()
    session.headers["User-Agent"] = "led-flight-display"
    AIRPORTS[:] = load_airports(session)
    current = Mode(session, mode)
    pygame.key.start_text_input()  # typed characters arrive as TEXTINPUT events

    commands = queue.Queue()  # from the display, the terminal and the R / L keys
    threading.Thread(target=read_terminal, args=(commands,), daemon=True).start()
    print("Switch modes any time: type a command here (like: map r=30) and press Enter,")
    print("or in the display press Enter to type one, R for radar, M for map, T to track the blue flight.")
    typing = None  # the command being typed on the display, or None
    message, message_until = "", 0.0
    last_scene = 0.0
    last_mouse = 0.0  # when the mouse last moved: hovering redraws quickly for a moment
    ox, oy, dot = 0, 0, 1  # where the LED grid sits on screen (updated every frame)

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEMOTION:
                last_mouse = time.time()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and typing is None:
                found = current.plane_at(((event.pos[0] - ox) // dot, (event.pos[1] - oy) // dot))
                if found and found[0]["callsign"]:  # clicked an aircraft: track it
                    commands.put(found[0]["callsign"])
                elif found:
                    print("That aircraft isn't broadcasting a flight number, so it can't be tracked")
            elif event.type == pygame.KEYDOWN and typing is not None:
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    commands.put(typing)
                    typing = None
                elif event.key == pygame.K_ESCAPE:
                    typing = None
                elif event.key == pygame.K_BACKSPACE:
                    typing = typing[:-1]
            elif event.type == pygame.TEXTINPUT and typing is not None:
                typing = (typing + event.text)[:80]
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_F11:
                    fullscreen = not fullscreen
                    open_window(fullscreen)
                elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    typing = ""
                elif event.key == pygame.K_r:
                    commands.put("radar")
                elif event.key == pygame.K_m:
                    commands.put("map")
                elif event.key == pygame.K_t:  # radar and map mode: track the blue flight, if there is one
                    target = current.blue_flight()
                    if target:
                        commands.put(target["callsign"])

        while not commands.empty():
            text = commands.get().strip()
            if not text:
                continue
            result = run_command(session, text, current)
            if isinstance(result, Mode):
                current, message, last_scene = result, "", 0.0
                airports = MAP_SHOW_AIRPORTS if current.map_mode else SHOW_AIRPORTS
                airfields = str(MAP_SHOW_AIRFIELDS).lower() if current.map_mode else None  # map mode only
                heli_only = None if current.flight else str(MAP_HELI_ONLY if current.map_mode else HELI_ONLY).lower()
                print(f"Now showing: {current.name} " + " ".join(
                    f"{k}={v}" for k, v in (("r", current_radius(current)), ("airports", str(airports).lower()),
                                             ("airfields", airfields), ("num_flights", current_count(current)),
                                             ("heli_only", heli_only), ("roads", str(SHOW_ROADS).lower()))
                    if v is not None))
            else:
                print(result)  # a mistake: keep the current mode
                message, message_until = result, time.time() + 6

        # Downloads can take seconds, so they wait while you type; otherwise keys would freeze
        if typing is None:
            current.update(session)

        now = time.time()
        if now - last_scene >= 0.25:  # the map and planes only need redrawing 4 times a second
            current.render(scene)
            last_scene = now
        screen = pygame.display.get_surface()
        sw, sh = screen.get_size()
        dot = max(1, min(sw // GRID_W, sh // GRID_H))
        if dot != mask_dot:
            mask, mask_dot = build_dot_mask(dot), dot
        ox, oy = (sw - GRID_W * dot) // 2, (sh - GRID_H * dot) // 2

        low.blit(scene, (0, 0))
        if pygame.mouse.get_focused():  # which LED the mouse is over
            mx, my = pygame.mouse.get_pos()
            current.draw_hover(low, ((mx - ox) // dot, (my - oy) // dot))
        if typing is not None:
            cursor = "_" if int(now * 2) % 2 else " "
            draw_command_bar(low, "> " + typing + cursor, COLOR_CALLSIGN, typing=True)
        elif message and now < message_until:
            draw_command_bar(low, message, COLOR_DOWN)

        screen.fill((0, 0, 0))
        screen.blit(pygame.transform.scale(low, (GRID_W * dot, GRID_H * dot)), (ox, oy))
        screen.blit(mask, (ox, oy))
        pygame.display.flip()
        quick = typing is not None or (not current.flight and now - last_mouse < 1)  # typing, or moving the mouse
        time.sleep(0.03 if quick else 0.25)
    pygame.quit()


USAGE = f"""usage: py flights.py [radar | map | FLIGHT] [r=RADIUS] [airports=true|false] [airfields=true|false]
                    [num_flights=N] [heli_only=true|false] [roads=true|false]
                    [set_coords=LAT,LON]
  radar      (the default) nearest airline flight with a known route
  map        every plane and helicopter nearby, full screen
  FLIGHT     track one flight anywhere, e.g. UAL123 or UA123
  r=         map radius in miles (defaults: {RADIUS_MILES:g} radar, {MAP_RADIUS_MILES:g} map, {TRACK_NO_ROUTE_RADIUS_MILES:g} tracking without a route)
  airports=  radar/tracking: show the route's departure and arrival airports; map: show every airline airport
             (default: true)
  airfields= map mode only: show every other airport too (default: false)
  num_flights=  how many of the closest flights to show, or all (defaults: 1 radar, all map; 0 shows none)
  heli_only= map: only helicopters; radar: the selected flight plus the closest helicopters, num_flights in all
             (default: false)
  roads=     draw roads on the satellite map (default: false)
  set_coords= radar and map: move the center to LAT,LON until the program closes
             (default: CENTER_COORDS from .env; tracking mode always uses .env)
While running: press Enter in the display (or type in this terminal) to give a new command; R = radar, M = map,
T = track the blue flight (radar and map mode).
A command naming a mode starts it fresh; options alone (like r=30) change only those in the current mode."""

OPTION_SETTINGS = ("RADIUS_MILES", "MAP_RADIUS_MILES", "TRACK_NO_ROUTE_RADIUS_MILES", "SHOW_AIRPORTS",
                   "MAP_SHOW_AIRPORTS", "MAP_SHOW_AIRFIELDS", "NUM_FLIGHTS", "MAP_NUM_FLIGHTS",
                   "HELI_ONLY", "MAP_HELI_ONLY", "SHOW_ROADS")
DEFAULTS = {name: globals()[name] for name in OPTION_SETTINGS}  # every command starts from these


def parse_command(args, base=None):
    """(mode, settings) for a command like ["map", "r=30"]. mode is "radar", "map", "help", a flight
    number, or None if the command doesn't name one; settings are base (default: the defaults) with the
    options applied. Raises ValueError for mistakes."""
    mode, settings = None, dict(base or DEFAULTS)
    args = " ".join(args).replace(", ", ",").replace(" ,", ",").split()  # "LAT, LON" is one value
    for arg in args:
        key, has_value, value = arg.partition("=")
        key, value = key.lower(), value.lower()
        if not has_value:
            if key in ("help", "-h", "--help", "/?"):
                return "help", settings
            if key.isdigit():
                raise ValueError(f"Write it as num_flights={key}")
            if mode:
                raise ValueError("Only one mode or flight at a time")
            mode = key if key in ("radar", "map") else arg
        elif key in ("r", "radius"):
            try:
                r = float(value)
            except ValueError:
                r = 0
            if not 0 < r <= 5000:
                raise ValueError("r= needs a number, like r=15")
            r = int(r) if r == int(r) else r
            for name in ("RADIUS_MILES", "MAP_RADIUS_MILES", "TRACK_NO_ROUTE_RADIUS_MILES"):
                settings[name] = r  # only the active mode's radius is used
        elif key == "set_coords":
            coords = parse_coords(value)
            if not coords:
                raise ValueError("set_coords= needs LAT,LON, like set_coords=40.71,-74.01")
            # not in DEFAULTS, so the new center lasts until the program closes, even across new modes
            settings["CENTER_COORDS"] = coords
        elif key == "num_flights":
            if value == "all":
                n = None
            elif value.isdigit():
                n = int(value)
            else:
                raise ValueError("num_flights= needs a number or all")
            settings["NUM_FLIGHTS"] = settings["MAP_NUM_FLIGHTS"] = n  # only the active mode's count is used
        elif key in ("airports", "airfields", "heli_only", "roads"):
            if value in ("true", "yes", "on", "1"):
                on = True
            elif value in ("false", "no", "off", "0"):
                on = False
            else:
                raise ValueError(f"{key}= should be true or false")
            names = {"airports": ("SHOW_AIRPORTS", "MAP_SHOW_AIRPORTS"),
                     "airfields": ("MAP_SHOW_AIRFIELDS",), "heli_only": ("HELI_ONLY", "MAP_HELI_ONLY"),
                     "roads": ("SHOW_ROADS",)}[key]
            for name in names:
                settings[name] = on  # only the active mode's setting is used
        else:
            raise ValueError(f"Unknown option {arg}")
    return mode, settings


if __name__ == "__main__":
    try:
        start_mode, start_settings = parse_command(sys.argv[1:])
        start_mode = start_mode or "radar"
    except ValueError as e:
        sys.exit(f"{e}.\n\n{USAGE}")
    if start_mode == "help":
        sys.exit(USAGE)
    globals().update(start_settings)
    main(start_mode)