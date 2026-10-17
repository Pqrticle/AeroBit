import csv
import gzip
import io
import json
import math
import os
import re
import time

import pygame
import requests

import aerobit as app
from aerobit import (COLOR_AIRPORT, COLOR_FLAGGED_AIRCRAFT, COLOR_GRID, COLOR_HOME, COLOR_LABEL, COLOR_LINE,
                     COLOR_MILITARY, COLOR_OFF, COLOR_ROAD, COLOR_SMALL_AIRPORT, COLOR_TEXT, CROWDED_AIRPORTS,
                     FLAGGED_AIRCRAFT, GRID_H, GRID_W, MAX_AIRPORT_LABELS, MAX_TRACES, ROOT, SATELLITE_MAP,
                     TAKEOFF_AIRPORT_MILES, clean, draw_outlined_icon, draw_text_outlined, text_width)

FEED_URLS = [  # adsb.fi first: adsb.lol is often overloaded
    "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}",
    "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}",
]
TRACE_URLS = [
    "https://adsb.lol/data/traces/{folder}/trace_full_{hex}.json",
    "https://globe.adsb.fi/data/traces/{folder}/trace_full_{hex}.json",
]
AIRPORTS_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
AIRPORTS_FILE = os.path.join(ROOT, "airports.csv")
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ROADS_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"

EARTH_RADIUS_MILES = 3958.8
MILES_PER_DEGREE = EARTH_RADIUS_MILES * math.pi / 180
KNOTS_TO_MPH = 1.15078
NM_TO_MILES = 1.15078

RENAMED_AIRPORTS = {"PBI": "DJT", "KPBI": "DJT", "KDJT": "DJT"}  # airport codes that changed: old -> new
NON_AIRLINE_OPERATORS = {"EJA", "EJM", "LXJ", "VJT", "XSR", "JTL", "GAJ", "TWY", "FLX", "JRE", "KFB"}
MILITARY_PREFIXES = ("RCH", "REACH", "SAM", "SPAR", "CNV", "PAT", "NAVY", "ARMY", "VV", "TOPCAT", "EVAC", "NATO")
HELICOPTER_WORDS = ("HELICOPTER", "COPTER", "ROBINSON")
# Helicopter ICAO type codes, from readsb/tar1090
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

# Icons point east and are rotated to the heading
PLANE_ICON = [
    "....##.....",
    ".....##....",
    "#.....##...",
    "##########.",
    "#.....##...",
    ".....##....",
    "....##.....",
]
SMALL_PLANE_ICON = [
    "..#....",
    "...#...",
    "#######",
    "...#...",
    "..#....",
]
HELICOPTER_DOT = [
    "###",
    "###",
    "###",
]


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


def current_position(p, now):
    dt = min(now - p["fetched"], 60)
    if not p["mph"] or p["track"] is None or dt <= 0:
        return p["lat"], p["lon"]
    deg = p["mph"] * dt / 3600 / MILES_PER_DEGREE
    lat = p["lat"] + deg * math.cos(math.radians(p["track"]))
    lon = p["lon"] + deg * math.sin(math.radians(p["track"])) / max(0.1, math.cos(math.radians(p["lat"])))
    return lat, lon


def number(value):
    return value if isinstance(value, (int, float)) else None


def parse_plane(ac, now):
    lat, lon, alt = ac.get("lat"), ac.get("lon"), ac.get("alt_baro")
    last = ac.get("lastPosition") or {}
    if (lat is None or lon is None) and (last.get("seen_pos") or 999) < 120:
        lat, lon = last.get("lat"), last.get("lon")
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
        "dist": distance_miles(*app.CENTER_COORDS, lat, lon),
        "alt_trend": 0, "mph_trend": 0,
    }


feed_problems = {}
paused_until = {}


def feed_failed(host, problem):
    if feed_problems.get(host) != problem:
        print(f"Feed {host} failed: {problem}")
    feed_problems[host] = problem


def get_politely(session, url, timeout=5, method="GET", **kwargs):
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
        except ValueError:
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
    now = time.time()
    lat, lon = app.CENTER_COORDS
    found = feed_get(session, FEED_URLS, lat=lat, lon=lon, dist=min(radius_miles / NM_TO_MILES, 250))
    return sorted((p for p in (parse_plane(ac, now) for ac in found) if p), key=lambda p: p["dist"])


AIRPORTS = []
ALL_AIRPORTS = []
TOWNS = []  # (lat, lon, "TOWN, AREA") for every airport that lists the town it serves


def renamed(airport):
    new = RENAMED_AIRPORTS.get(airport["code"]) or RENAMED_AIRPORTS.get(airport["icao"])
    if new:
        airport["code"] = airport["icao"] = new
    return airport


def load_airports(session):
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
        return
    ALL_AIRPORTS[:] = [renamed({"code": r["iata_code"] or r["icao_code"] or r["ident"], "icao": r["icao_code"] or r["ident"],
                                "city": r["municipality"] or r["name"], "type": r["type"], "airline": r["scheduled_service"] == "yes",
                                "pos": (float(r["latitude_deg"]), float(r["longitude_deg"]))})
                       for r in csv.DictReader(io.StringIO(data.decode("utf-8")))
                       if r["type"] in ("large_airport", "medium_airport", "small_airport")]
    AIRPORTS[:] = [a for a in ALL_AIRPORTS if a["airline"]]
    TOWNS[:] = [(float(r["latitude_deg"]), float(r["longitude_deg"]), town_name(r))
                for r in csv.DictReader(io.StringIO(data.decode("utf-8")))
                if r["type"] in ("large_airport", "medium_airport", "small_airport") and r["municipality"]]


def town_name(r):
    """CITY, ST in the US; elsewhere CITY, REGION, COUNTRY, or CITY, COUNTRY where regions are numbered."""
    town = clean(r["municipality"].split(",")[0].strip())
    region = r["iso_region"].split("-", 1)[-1]
    if r["iso_country"] == "US":
        return f"{town}, {region}"
    return f"{town}, {region}, {r['iso_country']}" if region.isalpha() and len(region) <= 3 else f"{town}, {r['iso_country']}"


def nearest_town(point, max_miles=30):
    """The town of the airport closest to point, if it's within max_miles."""
    k = math.cos(math.radians(point[0])) ** 2
    best = min(TOWNS, key=lambda t: (t[0] - point[0]) ** 2 + ((t[1] - point[1] + 180) % 360 - 180) ** 2 * k, default=None)
    return best[2] if best and distance_miles(*point, best[0], best[1]) <= max_miles else None


def nearest_airport(point, max_miles):
    best = min(AIRPORTS, key=lambda a: distance_miles(*point, *a["pos"]), default=None)
    return best if best and distance_miles(*point, *best["pos"]) <= max_miles else None


takeoff_cache = {}  # icao24 -> (time checked, takeoff airport or None)
departure_cache = {}  # icao24 -> wheels-up time


def fetch_trace(session, icao24, trim_far=True):
    """Also records the takeoff airport and time, which is what confirms routes."""
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
                pts = []  # a new flight or a 20-minute gap
            if row[1] is not None and row[2] is not None:
                pts.append((row[1], row[2], t, row[3]))
        first_alt = pts[0][3] if pts else None
        took_off = first_alt == "ground" or (isinstance(first_alt, (int, float)) and first_alt < 2500)
        takeoff_cache[icao24] = (time.time(), nearest_airport(pts[0][:2], TAKEOFF_AIRPORT_MILES) if took_off else None)
        if took_off:
            departure_cache[icao24] = next((t for _, _, t, alt in pts if alt != "ground"), None)
        else:
            departure_cache.pop(icao24, None)
        if trim_far:
            pts = pts[next((i for i, (lat, lon, *_) in enumerate(pts) if distance_miles(*app.CENTER_COORDS, lat, lon) < 150), len(pts)):]
        return pts
    takeoff_cache[icao24] = (time.time() - 3 * 3600 + 600, None)
    return []


def takeoff_airport(icao24):
    checked = takeoff_cache.get(icao24)
    return checked[1] if checked and time.time() - checked[0] < 3 * 3600 else None


def takeoff_checked(icao24):
    checked = takeoff_cache.get(icao24)
    return bool(checked) and time.time() - checked[0] < 3 * 3600


def is_candidate(callsign):
    return bool(re.fullmatch(r"[A-Z]{3}\d{1,4}[A-Z]{0,2}", callsign)) and callsign[:3] not in NON_AIRLINE_OPERATORS


def is_flagged_aircraft(p):
    names = {n.strip().upper() for n in FLAGGED_AIRCRAFT}
    return p["callsign"] in names or p["reg"].upper() in names


def is_helicopter(p):
    return (p["type"].upper() in HELICOPTER_TYPES or p["category"] == "A7"
            or any(w in clean(p["desc"]) for w in HELICOPTER_WORDS))


def is_large(p):
    return p["category"] in ("A3", "A4", "A5") if p["category"] else is_candidate(p["callsign"])


def aircraft_color(p, radar_flight=False):
    return (COLOR_FLAGGED_AIRCRAFT if is_flagged_aircraft(p) else COLOR_MILITARY if p["military"]
            else COLOR_AIRPORT if radar_flight else COLOR_TEXT)


def map_icon(p):
    return HELICOPTER_DOT if is_helicopter(p) else PLANE_ICON if is_large(p) else SMALL_PLANE_ICON


class MapView:

    def __init__(self, rect, center, scale):
        self.rect, self.inner = rect, rect.inflate(-2, -2)
        self.center, self.scale = center, scale
        self.k = math.cos(math.radians(center[0]))

    @classmethod
    def around(cls, rect, center, radius_miles):
        return cls(rect, center, (rect.height - 4) / 2 / (radius_miles / MILES_PER_DEGREE))

    @classmethod
    def fitting(cls, rect, points, pad=8, min_radius_miles=20):
        ref_lon = points[0][1]
        lats = [p[0] for p in points]
        lons = [ref_lon + (p[1] - ref_lon + 180) % 360 - 180 for p in points]
        center = ((min(lats) + max(lats)) / 2, ((min(lons) + max(lons)) / 2 + 180) % 360 - 180)
        k = math.cos(math.radians(center[0]))
        span_y = max(max(lats) - min(lats), 2 * min_radius_miles / MILES_PER_DEGREE)
        span_x = max((max(lons) - min(lons)) * k, 2 * min_radius_miles / MILES_PER_DEGREE)
        inner = rect.inflate(-2, -2)
        return cls(rect, center, min((inner.width - 2 * pad) / span_x, (inner.height - 2 * pad) / span_y))

    def reach_miles(self):
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
    x %= 2 ** z
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
    if not SATELLITE_MAP or view is None:
        return None
    key = (tuple(view.rect), view.center, round(view.scale, 6), app.SHOW_ROADS)
    if key not in background_cache:
        if len(background_cache) > 20:
            background_cache.clear()
        background_cache[key] = build_background(session, view)
    return background_cache[key]


def build_background(session, view):
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
    roads = stitch(session, ROADS_URL, z, tx0, tx1, ty0, ty1, alpha=True) if app.SHOW_ROADS else None

    crop = pygame.Rect(int(x0 - tx0 * 256), int(y0 - ty0 * 256), max(1, int(x1 - x0)), max(1, int(y1 - y0)))
    tall_h = rect.height * 4

    def shrink(img):
        return pygame.transform.smoothscale(img.subsurface(crop.clip(img.get_rect())), (rect.width, tall_h))

    tall = shrink(canvas)
    tall_roads = shrink(roads) if roads else None
    rows = []
    for j in range(rect.height):
        lat, _ = view.unproject(rect.left, rect.top + j + 0.5)
        rows.append(max(0, min(tall_h - 1, int((merc_y(lat, z) - y0) / max(y1 - y0, 1e-6) * tall_h))))

    bg = pygame.Surface(rect.size)
    for y, row in enumerate(rows):
        for x in range(rect.width):
            r, g, b, _ = tall.get_at((x, row))
            avg = (r + g + b) / 3
            color = [max(0, min(255, (avg + (c - avg) * 1.6) * 0.8)) for c in (r, g, b)]
            if tall_roads:
                mix = min(1.0, tall_roads.get_at((x, row))[3] / 140)
                color = [c * (1 - mix) + rc * mix for c, rc in zip(color, COLOR_ROAD)]
            bg.set_at((x, y), [int(c) for c in color])
    return bg


def draw_map_base(low, view, background):
    if background:
        low.blit(background, view.inner.topleft)
    else:
        for y in range(view.rect.top + 4, view.rect.bottom - 2, 6):
            for x in range(view.rect.left + 4, view.rect.right - 2, 6):
                low.fill(COLOR_GRID, (x, y, 1, 1))
    pygame.draw.rect(low, COLOR_LINE, view.rect, 1)


def draw_dotted_path(surf, view, points, color, spacing):
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


def draw_home(low, view, point=None):
    hx, hy = (round(v) for v in view.project(*(point or app.CENTER_COORDS)))
    low.fill((0, 0, 0), (hx - 3, hy - 1, 7, 3))
    low.fill((0, 0, 0), (hx - 1, hy - 3, 3, 7))
    low.fill(COLOR_HOME, (hx - 2, hy, 5, 1))
    low.fill(COLOR_HOME, (hx, hy - 2, 1, 5))


def plane_box(view, p, now, icon=PLANE_ICON):
    x, y = (round(v) for v in view.project(*current_position(p, now)))
    r = max(len(icon), len(icon[0])) // 2 + 2
    return pygame.Rect(x - r, y - r, 2 * r + 1, 2 * r + 1)


def draw_airports(low, view, airports, avoid, route=()):
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

    for a in reversed(others):
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
        ty = y - 11 if y - 11 >= view.inner.top + 1 else y + 4
        box = pygame.Rect(tx - 1, ty - 1, text_width(code) + 2, 9)
        if box.collidelist(avoid) == -1:
            draw_text_outlined(low, code, tx, ty, COLOR_AIRPORT if a.get("airline", True) else COLOR_SMALL_AIRPORT)
            avoid.append(box)
            labels += 1


def map_airports(view):
    show_airports, show_airfields = app.SHOW_AIRPORTS, app.MAP_SHOW_AIRFIELDS
    if not (show_airports or show_airfields):
        return []
    order = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
    return sorted((a for a in ALL_AIRPORTS if (show_airports if a["airline"] else show_airfields)
                   and view.contains(*a["pos"], margin=3)), key=lambda a: (order[a["type"]], not a["airline"]))


def update_trails(session, trails, planes, view):
    now, downloads = time.time(), 0
    for p in planes:
        if not view.contains(p["lat"], p["lon"]):
            trails.pop(p["icao24"], None)
            continue
        trail = trails.get(p["icao24"])
        if trail is None:
            if downloads >= MAX_TRACES:
                continue
            downloads += 1
            trail = trails[p["icao24"]] = {"pts": fetch_trace(session, p["icao24"])}
        trail["seen"] = now
        trail["pts"].append((p["lat"], p["lon"], now))
    for icao in [i for i, t in trails.items() if now - t["seen"] > 60]:
        del trails[icao]


def render_map_mode(low, view, background, planes, trails, now, airports=(), radar_pick=None):
    import radar  # imported here because radar imports this module

    low.fill(COLOR_OFF)
    draw_map_base(low, view, background)
    low.set_clip(view.inner)
    trail_color = COLOR_TEXT if background else COLOR_LABEL
    for p in planes:
        here = current_position(p, now)
        if not view.contains(*here):
            continue
        trail = trails.get(p["icao24"])
        if trail:
            draw_dotted_path(low, view, [(lat, lon) for lat, lon, *_ in trail["pts"]] + [here], trail_color, 2)
        route = radar.best_route(p)
        if route:
            draw_dotted_path(low, view, great_circle(here, route["dest"]["pos"]), trail_color, 4)
    draw_airports(low, view, airports, [plane_box(view, p, now, map_icon(p)) for p in planes])
    draw_home(low, view)
    for p in reversed(planes):
        x, y = (round(v) for v in view.project(*current_position(p, now)))
        icon = map_icon(p)
        draw_outlined_icon(low, icon, x, y, 0 if icon is HELICOPTER_DOT else (p["track"] or 0) - 90,
                           aircraft_color(p, p["icao24"] == radar_pick))
    low.set_clip(None)


class MapMode:
    kind = "map"

    def __init__(self, session):
        self.name = "map"
        self.view = MapView.around(pygame.Rect(0, 0, GRID_W, GRID_H), app.CENTER_COORDS, app.MAP_RADIUS_MILES)
        self.radius = math.ceil(self.view.reach_miles())
        self.background = get_background(session, self.view)
        self.airports = map_airports(self.view)
        self.planes, self.trails = [], {}
        self.radar_pick = None  # drawn in blue

    def refresh(self, session):
        import radar

        try:
            fetched = fetch_aircraft(session, self.radius)
            planes = [q for q in fetched if self.view.contains(q["lat"], q["lon"])
                      and (is_helicopter(q) or not app.MAP_HELI_ONLY)][:app.MAP_NUM_FLIGHTS]
            update_trails(session, self.trails, planes, self.view)
            self.radar_pick = next((q["icao24"] for q in fetched if not q["ground"] and
                                    q["dist"] <= app.RADAR_RADIUS_MILES and radar.qualifies(q)), None)
            radar.update_lookups(session, planes)
            self.planes = planes
        except ConnectionError as e:
            print(e)

    def hoverable(self):
        return self.planes

    def blue_flight(self):
        return next((q for q in self.planes if q["icao24"] == self.radar_pick), None)

    def render(self, low):
        render_map_mode(low, self.view, self.background, self.planes, self.trails, time.time(), self.airports,
                        self.radar_pick)
