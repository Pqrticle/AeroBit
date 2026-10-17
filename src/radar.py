import io
import time

import pygame

import aerobit as app
from aerobit import (COLOR_AIRPORT, COLOR_CALLSIGN, COLOR_DOWN, COLOR_EMERGENCY, COLOR_EXTRA, COLOR_LABEL,
                     COLOR_LINE, COLOR_OFF, COLOR_TEXT, COLOR_UP, ETA_BUFFER_MINUTES, GLYPHS, GRID_H, GRID_W,
                     MAX_LOOKUPS, MAX_TRACES, clean, draw_icon, draw_outlined_icon, draw_text, draw_text_center,
                     draw_text_right, fit, text_width)
from map import (HELICOPTER_DOT, PLANE_ICON, MapView, aircraft_color, alert_color, bearing, current_position,
                 departure_cache, distance_miles, draw_airports, draw_dotted_path, draw_home, draw_map_base,
                 feed_failed, fetch_aircraft, fetch_trace, get_background, get_politely, great_circle,
                 is_candidate, is_helicopter, plane_box, renamed, takeoff_airport, takeoff_checked)

ADSBDB_URL = "https://api.adsbdb.com/v0/"
ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"
LOGO_URL = "http://img.wway.io/pics/root/{iata}@png?exar=1&rs=fit:256:256"
LOGO_H = 34
LOGO_MAX_W = 46
LOGO_STRETCH = 1.15
PANEL = pygame.Rect(130, 76, GRID_W - 132, GRID_H - 78)
NO_TRAIL = {"pts": [], "downloaded": False}

info_cache = {}
airline_cache = {}
logo_cache = {}
last_reading = {}


def airport(a):
    try:
        pos = (float(a["latitude"]), float(a["longitude"]))
    except (KeyError, TypeError, ValueError):
        pos = None
    return renamed({"code": a.get("iata_code") or a.get("icao_code") or "", "icao": a.get("icao_code") or "",
                    "city": a.get("municipality") or a.get("name") or "", "pos": pos})


def lol_airport(a):
    try:
        pos = (float(a["lat"]), float(a["lon"]))
    except (KeyError, TypeError, ValueError):
        pos = None
    return renamed({"code": a.get("iata") or a.get("icao") or "", "icao": a.get("icao") or "",
                    "city": a.get("location") or a.get("name") or "", "pos": pos})


def adsbdb(session, path):
    try:
        resp = session.get(ADSBDB_URL + path, timeout=10)
        if resp.status_code == 404:
            return {}
        response = resp.json().get("response")
    except Exception:
        return None
    return response if isinstance(response, dict) else {}


def lookup_route(session, cs):
    response = adsbdb(session, "callsign/" + cs)
    if response is None:
        return None
    r = response.get("flightroute")
    if not isinstance(r, dict):
        return ()
    orig, dest = airport(r.get("origin") or {}), airport(r.get("destination") or {})
    return (orig, dest) if orig["code"] and dest["code"] and orig["pos"] and dest["pos"] else ()


def lookup_airline(session, code):
    if code not in airline_cache:
        try:
            resp = session.get(ADSBDB_URL + "airline/" + code, timeout=10)
            airlines = [] if resp.status_code == 404 else resp.json().get("response")
        except Exception:
            return {}
        match = next((a for a in airlines if isinstance(a, dict) and a.get("icao") == code), None) \
            if isinstance(airlines, list) else None
        airline_cache[code] = {"name": match.get("name") or "", "iata": match.get("iata")} if match else {}
    return airline_cache[code]


def look_up_flight(session, cs):
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
    todo = [p for p in planes if p["callsign"] in info_cache and "adsblol" not in info_cache[p["callsign"]]][:100]
    if not todo:
        return
    body = {"planes": [{"callsign": p["callsign"], "lat": p["lat"], "lng": p["lon"]} for p in todo]}
    try:
        resp = get_politely(session, ROUTESET_URL, timeout=10, method="POST", json=body)
        routes = resp.json() if resp is not None and resp.status_code == 200 else None
    except Exception as e:
        feed_failed("api.adsb.lol routes", str(e))
        return
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
    try:
        resp = session.get(LOGO_URL.format(iata=iata), timeout=10)
        img = pygame.image.load(io.BytesIO(resp.content)).convert_alpha()
    except Exception:
        return None
    bounds = img.get_bounding_rect()
    if not bounds.w or not bounds.h:
        return None
    img = img.subsurface(bounds)
    scale = min(LOGO_MAX_W / bounds.w, LOGO_H / (bounds.h * LOGO_STRETCH))
    nw, nh = max(1, round(bounds.w * scale)), max(1, min(LOGO_H, round(bounds.h * scale * LOGO_STRETCH)))
    while img.get_width() > nw * 2:
        img = pygame.transform.smoothscale(img, (max(nw, img.get_width() // 2), max(nh, img.get_height() // 2)))
    small = pygame.transform.smoothscale(img, (nw, nh))
    pixels = []
    for y in range(nh):
        for x in range(nw):
            r, g, b, a = small.get_at((x, y))
            if a < 40:
                continue
            if max(r, g, b) < 45:
                r, g, b = 70, 70, 85
            f = a / 255
            pixels.append((x, y, tuple(round(c * f + o * (1 - f)) for c, o in zip((r, g, b), COLOR_OFF))))
    return {"w": nw, "h": nh, "pixels": pixels} if pixels else None


def update_lookups(session, planes):
    done = 0
    for p in planes:
        if not is_candidate(p["callsign"]) or p["callsign"] in info_cache:
            continue
        if done >= MAX_LOOKUPS:
            break
        done += 1
        look_up_flight(session, p["callsign"])
    lookup_routeset(session, planes)


def route_fits(p, orig, dest):
    """The 3 checks: position, heading, and climb or descent."""
    route = distance_miles(*orig, *dest)
    flown = distance_miles(*orig, p["lat"], p["lon"])
    left = distance_miles(p["lat"], p["lon"], *dest)
    if flown + left - route > max(40, 0.15 * route):
        return False
    if p["track"] is not None and flown > 30 and left > 30:
        if abs((bearing(p["lat"], p["lon"], *dest) - p["track"] + 180) % 360 - 180) > 60:
            return False
    if p["alt"] is not None and p["alt"] < 12000 and route > 100:
        if flown < 50 and (p["vrate"] or 0) <= -500:
            return False
        if left < 50 and (p["vrate"] or 0) >= 500:
            return False
    return True


def same_airport(a, b):
    return bool(a["icao"] and a["icao"] == b["icao"]) or distance_miles(*a["pos"], *b["pos"]) < 5


def best_route(p):
    """The confirmed route, or None. See "How routes are found" in the README."""
    info = info_cache.get(p["callsign"])
    orig = takeoff_airport(p["icao24"])
    if not info or not orig:
        return None
    db, legs = info.get("adsbdb"), info.get("adsblol") or []
    dest = next((d for o, d in legs if same_airport(o, orig)), None)
    if dest is None and db and same_airport(db[0], orig):
        dest = db[1]
    if dest is None or same_airport(orig, dest) or not route_fits(p, orig["pos"], dest["pos"]):
        return None
    return {"orig": orig, "dest": dest}


def qualifies(p):
    info = info_cache.get(p["callsign"])
    return bool(is_candidate(p["callsign"]) and info and info.get("iata") and best_route(p))


def trend(before, now, step):
    if before is None or now is None:
        return 0
    return 1 if now - before >= step else -1 if before - now >= step else 0


def note_trends(planes):
    for p in planes:
        before = last_reading.get(p["icao24"], (None, None))
        p["alt_trend"] = trend(before[0], p["alt"], 50)
        p["mph_trend"] = trend(before[1], p["mph"], 2)
        last_reading[p["icao24"]] = (p["alt"], p["mph"])


def flight_path(p, trail, now):
    here = current_position(p, now)
    info = best_route(p)
    behind = [(lat, lon) for lat, lon, *_ in trail["pts"]] + [here]
    if info and not trail["downloaded"]:
        behind = great_circle(info["orig"]["pos"], behind[0]) + behind
    ahead = great_circle(here, info["dest"]["pos"]) if info else []
    return behind, ahead


def path_miles(trail):
    pts = trail["pts"]
    done, miles = trail.get("miles_n", 0), trail.get("miles", 0.0)
    if done > len(pts):
        done, miles = 0, 0.0
    for a, b in zip(pts[max(0, done - 1):], pts[max(1, done):]):
        if "ground" not in (a[3:4] + b[3:4]):
            miles += distance_miles(a[0], a[1], b[0], b[1])
    trail["miles_n"], trail["miles"] = len(pts), miles
    return miles


def miles_flown(p, trail, now, orig=None):
    here = current_position(p, now)
    pts = trail["pts"] if trail else []
    if not pts:
        return distance_miles(*orig["pos"], *here) if orig else None
    miles = path_miles(trail) + distance_miles(pts[-1][0], pts[-1][1], *here)
    if orig and p["icao24"] not in departure_cache:
        miles += distance_miles(*orig["pos"], pts[0][0], pts[0][1])
    return miles


def eta(p, now):
    route = best_route(p)
    mph = p["mph"] or 0
    if not route or mph <= 80:
        return None
    left = distance_miles(*current_position(p, now), *route["dest"]["pos"])
    return now + left / mph * 3600 + ETA_BUFFER_MINUTES * 60


def departed(p, now):
    dep = departure_cache.get(p["icao24"])
    return dep if dep is not None and dep <= now else None


def draw_header(low, p, count, tracking=False, near=None):
    info = info_cache.get(p["callsign"], {})
    logo = logo_cache.get(info.get("iata"))
    if logo:
        ox, oy = 1, 1 + (LOGO_H - logo["h"]) // 2
        for px, py, color in logo["pixels"]:
            low.fill(color, (ox + px, oy + py, 1, 1))
        text_x = max(38, 1 + logo["w"] + 4)
    else:
        low.fill(COLOR_LINE, (1, 1, LOGO_H, LOGO_H))
        draw_text_center(low, p["callsign"][:3], 1 + LOGO_H // 2, 14, COLOR_TEXT)
        text_x = 38

    dist = f"{p['dist']:.1f}MI" if p["dist"] < 100 else f"{p['dist']:,.0f}MI"
    if tracking:  # the flight number is always scale 3; a distance too wide beside it goes small, below it
        scale = 3
        right = text_x + text_width(p["callsign"], scale) + 4
        draw_text_right(low, "TRACKING", GRID_W - 2, 3, COLOR_LABEL)
        big = GRID_W - 2 - text_width(dist, 2) >= right
        draw_text_right(low, dist, GRID_W - 2, 20 if big else 27, COLOR_TEXT, 2 if big else 1)
        block_left = GRID_W - 2 - text_width(dist, 2 if big else 1)
    else:
        labels = [f"{count} FLIGHT{'S' if count != 1 else ''}", f"({app.RADAR_RADIUS_MILES:g}MI RADIUS)"]
        block_left = GRID_W - 2 - max([text_width(dist, 2)] + [text_width(t) for t in labels])
        for i, t in enumerate(labels):
            draw_text_right(low, t, GRID_W - 2, 3 + i * 8, COLOR_LABEL)
        draw_text_right(low, dist, GRID_W - 2, 20, COLOR_TEXT, 2)
        scale = next((s for s in (4, 3) if text_x + text_width(p["callsign"], s) <= block_left - 4), 2)
        right = text_x + text_width(p["callsign"], scale) + 4

    color = (alert_color(p) if tracking else None) or COLOR_CALLSIGN
    draw_text(low, p["callsign"], text_x, 3, color, scale, [3] * 7)
    sub = info.get("airline") or p["callsign"][:3]
    draw_text(low, fit(sub, (block_left - 6 - text_x) // 6), text_x, 25, COLOR_TEXT, 1, [1, 1, 2, 1, 2, 1, 1])
    if near:  # the first label that fits beside the flight number, between TRACKING and the distance
        text = next((t for t in near if text_width(t) <= GRID_W - 2 - right), None)
        if text:
            draw_text_right(low, text, GRID_W - 2, 11, COLOR_LABEL)
        return text


def near_labels(town):
    """NEAR TOWN, REGION, COUNTRY, then without the country, then without the region."""
    parts = town.split(", ")
    return [f"NEAR {', '.join(parts[:n])}" for n in range(len(parts), 0, -1)]


def clock(t):
    return time.strftime("%I:%M%p", time.localtime(t)).lstrip("0")


def short_duration(secs):
    h, m = divmod(max(0, round(secs / 60)), 60)
    return f"{h}H{m:02d}M" if h else f"{m}M"


# Narrow lettering (each character only as wide as its lit LEDs) fits more between the airport codes
TIGHT_SPACE = 2


def glyph_cols(ch):
    cols = [c for c in range(5) if any(row[c] == "#" for row in GLYPHS.get(ch, GLYPHS[" "]))]
    return (cols[0], cols[-1]) if cols else (0, -1)


def tight_width(text):
    w = 0
    for ch in clean(text):
        first, last = glyph_cols(ch)
        w += TIGHT_SPACE if ch == " " else last - first + 2
    return max(0, w - 1)


def draw_text_fitting(low, options, center, y, width, color):
    text = next((t for t in options if tight_width(t) <= width), options[-1])
    draw_tight(low, text, center - tight_width(text) // 2, y, color)


def draw_tight(low, text, x, y, color):
    for ch in clean(text):
        if ch == " ":
            x += TIGHT_SPACE
            continue
        first, last = glyph_cols(ch)
        draw_icon(low, GLYPHS.get(ch, GLYPHS[" "]), x - first, y, color)
        x += last - first + 2


def progress_options(flown, pct, left):
    f, nf = f"{flown:,.0f}", f"{flown:.0f}"
    if left is None:
        return [f"{f}MI FLOWN", f"{f}MI"]
    l, nl = f"{left:,.0f}", f"{left:.0f}"
    return [f"{f}MI ({pct}%), {l}MI LEFT", f"{nf}MI ({pct}%), {nl}MI LEFT", f"{nf}MI ({pct}%), {nl} LEFT",
            f"{nf}MI {pct}%, {nl} LEFT", f"{nf}MI ({pct}%)"]


def times_options(dep, arrive, now):
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
        if took_off:
            draw_text(low, took_off["code"], 2, top, COLOR_AIRPORT, 3)
            draw_text(low, fit(took_off["city"], 20), 2, top + 24, COLOR_AIRPORT)
            left = 2 + text_width(took_off["code"], 3) + 8
            mid_x, width = (left + GRID_W - 2) // 2, GRID_W - 2 - left
            flown = miles_flown(p, trail, now, took_off)
            # evenly spaced from the code's top (row 40) to the town's bottom (row 70)
            draw_text_center(low, "ARRIVAL UNKNOWN", mid_x, top, COLOR_LABEL)
            if flown is not None:
                draw_text_fitting(low, progress_options(flown, None, None), mid_x, top + 12, width, COLOR_TEXT)
            draw_text_fitting(low, times_options(departed(p, now), None, now), mid_x, top + 24, width, COLOR_TEXT)
        else:
            draw_text_center(low, "ROUTE NOT AVAILABLE", GRID_W // 2, 52, COLOR_LABEL)
        return
    orig, dest = route["orig"], route["dest"]
    draw_text(low, orig["code"], 2, top, COLOR_AIRPORT, 3)
    draw_text_right(low, dest["code"], GRID_W - 2, top, COLOR_AIRPORT, 3)
    draw_text(low, fit(orig["city"], 20), 2, top + 24, COLOR_AIRPORT)
    draw_text_right(low, fit(dest["city"], 20), GRID_W - 2, top + 24, COLOR_AIRPORT)

    x0 = 2 + text_width(orig["code"], 3) + 3
    x1 = GRID_W - 2 - text_width(dest["code"], 3) - 3
    mid_x = (x0 + x1) // 2
    left = distance_miles(*current_position(p, now), *dest["pos"])
    flown = miles_flown(p, trail, now, orig)
    frac = min(1.0, max(0.0, flown / (flown + left))) if flown + left > 0 else 0.0

    bar_y = top + 10
    icon_w = len(PLANE_ICON[0])
    px = x0 + round(frac * (x1 - x0 - icon_w))
    low.fill(COLOR_AIRPORT, (x0, bar_y, max(0, px - x0), 1))
    for x in range(px + icon_w + 1, x1, 2):
        low.fill(COLOR_LINE, (x, bar_y, 1, 1))
    draw_icon(low, PLANE_ICON, px, bar_y - 3, COLOR_TEXT)
    draw_text_fitting(low, progress_options(flown, round(frac * 100), left), mid_x, top, x1 - x0, COLOR_LABEL)
    draw_text_fitting(low, times_options(departed(p, now), eta(p, now), now), mid_x, top + 16, x1 - x0, COLOR_TEXT)


def short_aircraft(p):
    desc = clean(p["desc"])
    if len(desc) > 10:
        desc = desc.split(" ", 1)[-1]
    if not desc or len(desc) > 10:
        desc = clean(p["type"])
    return desc or "---"


def compass(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(deg / 45) % 8]


def trend_color(t):
    return COLOR_UP if t > 0 else COLOR_DOWN if t < 0 else COLOR_TEXT


def draw_data(low, p, now):
    rows = [
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
        ("SQUAWK", p["squawk"] or "---", COLOR_EMERGENCY if p["squawk"] in ("7500", "7600", "7700", "7777") else COLOR_TEXT),
    ]
    for i, (label, value, color) in enumerate(rows):
        draw_text(low, label, 2, 77 + i * 9, COLOR_LABEL)
        draw_text(low, value, 66, 77 + i * 9, color)


def draw_info(low, p, count, now, trail, tracking=False, near=None):
    """Returns the near label drawn, if any."""
    shown = draw_header(low, p, count, tracking, near)
    low.fill(COLOR_LINE, (0, 36, GRID_W, 1))
    draw_route(low, p, now, trail)
    low.fill(COLOR_LINE, (0, 73, GRID_W, 1))
    draw_data(low, p, now)
    return shown


def draw_map(low, view, background, p, now, extras=(), trails=None, helis=()):
    trails = trails or {}
    draw_map_base(low, view, background)
    low.set_clip(view.inner)
    for e in extras:
        if best_route(e):
            behind, ahead = flight_path(e, trails.get(e["icao24"], NO_TRAIL), now)
            draw_dotted_path(low, view, behind, COLOR_EXTRA, 4)
            draw_dotted_path(low, view, ahead, COLOR_EXTRA, 4)
        ex, ey = (round(v) for v in view.project(*current_position(e, now)))
        draw_outlined_icon(low, PLANE_ICON, ex, ey, (e["track"] or 0) - 90, aircraft_color(e))
    route = best_route(p) if p else None
    if route:
        trail_color = COLOR_TEXT if background else COLOR_LABEL
        behind, ahead = flight_path(p, trails.get(p["icao24"], NO_TRAIL), now)
        draw_dotted_path(low, view, behind, trail_color, 2)
        draw_dotted_path(low, view, ahead, trail_color, 4)
    draw_home(low, view)
    route_airports = [a for a in ((route["dest"], route["orig"]) if route and app.SHOW_AIRPORTS else ())
                      if view.contains(*a["pos"], margin=3)]
    planes = [q for q in (p, *extras) if q]
    draw_airports(low, view, (), [plane_box(view, q, now) for q in planes] +
                  [plane_box(view, h, now, HELICOPTER_DOT) for h in helis], route_airports)
    for h in helis:
        hx, hy = (round(v) for v in view.project(*current_position(h, now)))
        draw_outlined_icon(low, HELICOPTER_DOT, hx, hy, 0, aircraft_color(h))
    if p:
        px, py = (round(v) for v in view.project(*current_position(p, now)))
        if view.inner.collidepoint(px, py):
            draw_outlined_icon(low, PLANE_ICON, px, py, (p["track"] or 0) - 90, COLOR_AIRPORT)
    low.set_clip(None)


def render(low, view, background, selected, count, now, extras=(), trails=None, helis=()):
    low.fill(COLOR_OFF)
    if not selected:
        draw_map(low, view, background, None, now, helis=helis)
        return
    draw_info(low, selected, count, now, (trails or {}).get(selected["icao24"]))
    draw_map(low, view, background, selected, now, extras, trails, helis)


class RadarMode:
    kind = "radar"

    def __init__(self, session):
        self.name = "radar"
        self.view = MapView.around(PANEL, app.CENTER_COORDS, app.RADAR_RADIUS_MILES)
        self.background = get_background(session, self.view)
        self.planes, self.trails = [], {}
        self.selected, self.extras, self.helis = None, [], []

    def refresh(self, session):
        try:
            planes = [q for q in fetch_aircraft(session, app.RADAR_RADIUS_MILES) if not q["ground"]]
            note_trends(planes)
            update_lookups(session, planes)
            self.planes = planes
        except ConnectionError as e:
            print(e)
        self.select(session)

    def settle(self, session, passes=15):
        """At startup: keep looking up routes and takeoffs until the flights to show are found or none are left
        to check, so the first screen is complete."""
        for _ in range(passes):
            shown = [q for q in (self.selected, *self.extras) if q]
            enough = self.wanted() is not None and len(shown) >= self.wanted()
            if not self.unchecked(shown[-1]["dist"] if enough else None):
                return
            update_lookups(session, self.planes)
            self.select(session)

    def unchecked(self, closer_than=None):
        """Whether any airline flight (closer than closer_than miles) still needs its route or takeoff checked."""
        return any(is_candidate(q["callsign"]) and (closer_than is None or q["dist"] < closer_than) and
                   (q["callsign"] not in info_cache or (info_cache[q["callsign"]] and not takeoff_checked(q["icao24"])))
                   for q in self.planes)

    def wanted(self):
        return app.NUM_FLIGHTS if not app.HELI_ONLY else (1 if app.NUM_FLIGHTS is None else min(1, app.NUM_FLIGHTS))

    def select(self, session):
        now = time.time()
        wanted = self.wanted()
        shown, checks = [], 0
        for q in self.planes:
            if wanted is not None and len(shown) >= wanted:
                break
            if not info_cache.get(q["callsign"]):
                continue
            pts = None
            if not takeoff_checked(q["icao24"]) and checks < MAX_TRACES:
                checks += 1
                pts = fetch_trace(session, q["icao24"], trim_far=False)
            if qualifies(q):
                shown.append(q)
                if q["icao24"] not in self.trails:
                    if pts is None:
                        pts = fetch_trace(session, q["icao24"], trim_far=False)
                    self.trails[q["icao24"]] = {"pts": pts, "downloaded": bool(pts)}
                self.trails[q["icao24"]]["pts"].append((q["lat"], q["lon"], now))
        nearby = {q["icao24"] for q in self.planes}
        for icao in [i for i in self.trails if i not in nearby]:
            del self.trails[icao]
        self.selected, self.extras = (shown[0] if shown else None), shown[1:]
        self.helis = []
        if app.HELI_ONLY:
            helis = [q for q in self.planes if is_helicopter(q) and q is not self.selected]
            self.helis = helis if app.NUM_FLIGHTS is None else helis[:max(0, app.NUM_FLIGHTS - len(shown))]

    def hoverable(self):
        return [q for q in (self.selected, *self.extras, *self.helis) if q]

    def blue_flight(self):
        return self.selected

    def render(self, low):
        render(low, self.view, self.background, self.selected, len(self.planes), time.time(), self.extras,
               self.trails, self.helis)
