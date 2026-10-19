#!/usr/bin/env python3
import math
import os
import queue
import sys
import threading
import time
import unicodedata

import pygame
import requests
import yaml

# When run as a script this module is __main__; this lets the modes `import aerobit` and share its settings
sys.modules.setdefault("aerobit", sys.modules[__name__])

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path=os.path.join(ROOT, ".env")):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


DEFAULT_TYPES = {
    "radar_radius_miles": "miles", "map_radius_miles": "miles", "satellite_map": "bool", "show_airports": "bool",
    "map_show_airfields": "bool", "map_heli_only": "bool", "show_roads": "bool", "start_fullscreen": "bool",
    "color_flagged_aircraft": "color",
}


def load_defaults(path=os.path.join(ROOT, "defaults.yml")):
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        sys.exit(f"Couldn't read defaults.yml: {e}")
    if not isinstance(data, dict):
        sys.exit("defaults.yml should be a list of name: value settings")
    for name in data.keys() - DEFAULT_TYPES.keys():
        sys.exit(f"Unknown setting in defaults.yml: {name}")
    for name, kind in DEFAULT_TYPES.items():
        if name not in data:
            sys.exit(f"defaults.yml is missing {name}")
        v = data[name]
        if kind == "bool":
            ok = isinstance(v, bool)
        elif kind == "miles":
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 5000
        else:
            ok = isinstance(v, list) and len(v) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in v)
        if not ok:
            want = {"bool": "true or false", "miles": "a number of miles", "color": "an RGB list like [255, 150, 0]"}
            sys.exit(f"defaults.yml: {name} should be {want[kind]}")
    return data


def parse_coords(text):
    try:
        lat, lon = (float(x) for x in text.split(","))
    except ValueError:
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


load_env()
HOME_COORDS = parse_coords(os.environ.get("CENTER_COORDS", ""))
if not HOME_COORDS:
    sys.exit("Set CENTER_COORDS=LAT,LON (the center of the map) in a .env file in the project folder; see .env.example")
FLAGGED_AIRCRAFT = [n for n in os.environ.get("FLAGGED_AIRCRAFT", "").split(",") if n.strip()]

yml = load_defaults()
RADAR_RADIUS_MILES = yml["radar_radius_miles"]
MAP_RADIUS_MILES = yml["map_radius_miles"]
SATELLITE_MAP = yml["satellite_map"]
SHOW_AIRPORTS = yml["show_airports"]
MAP_SHOW_AIRFIELDS = yml["map_show_airfields"]
MAP_HELI_ONLY = yml["map_heli_only"]
SHOW_ROADS = yml["show_roads"]
START_FULLSCREEN = yml["start_fullscreen"]
COLOR_FLAGGED_AIRCRAFT = tuple(yml["color_flagged_aircraft"])

# Settings
CENTER_COORDS = HOME_COORDS  # set_coords= changes it until the program closes
NUM_FLIGHTS = 1
MAP_NUM_FLIGHTS = None  # None = all
TRACK_NO_ROUTE_RADIUS_MILES = 1000  # tracking without a known route
MAX_TRACES = 4  # per refresh; more risks rate limits
REFRESH_SECONDS = 15
TIMEOUT_SECONDS = 3  # how long to wait for a server's answer (plus up to 2 s to connect) before giving up
ETA_BUFFER_MINUTES = 15  # added for the approach and landing
MAX_LOOKUPS = 8  # per refresh
TAKEOFF_AIRPORT_MILES = 10
MAX_AIRPORT_LABELS = 40
CROWDED_AIRPORTS = 60
HELI_ONLY = False

GRID_W, GRID_H = 256, 144

COLOR_OFF = (20, 20, 20)
COLOR_CALLSIGN = (255, 190, 0)
COLOR_AIRPORT = (0, 210, 255)
COLOR_TEXT = (230, 230, 230)
COLOR_LABEL = (120, 120, 120)
COLOR_LINE = (60, 60, 60)
COLOR_GRID = (34, 34, 34)
COLOR_HOME = (110, 255, 110)
COLOR_MILITARY = (255, 60, 60)
COLOR_EMERGENCY = (255, 60, 60)  # squawk 7500 hijack, 7600 radio failure, 7700 emergency, 7777 military intercept
COLOR_ROAD = (240, 200, 110)
COLOR_EXTRA = (200, 200, 200)
COLOR_SMALL_AIRPORT = (170, 170, 170)
COLOR_UP = (110, 255, 110)
COLOR_DOWN = (255, 140, 60)

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


def clean(text):
    return unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().upper()


def fit(text, max_chars):
    text = clean(text)
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "."


def text_width(text, scale=1):
    return max(0, len(str(text)) * 6 * scale - scale)


def draw_icon(surf, rows, x, y, color, scale=1, heights=None):
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
    rows = GLYPHS.get(clean(text)[-1:], GLYPHS[" "])
    blank = next((i for i in range(5) if any(r[4 - i] == "#" for r in rows)), 0)
    draw_text(surf, text, right - text_width(text, scale) + blank * scale, y, color, scale)


def draw_text_center(surf, text, center, y, color, scale=1):
    draw_text(surf, text, center - text_width(text, scale) // 2, y, color, scale)


def draw_text_outlined(surf, text, x, y, color):
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw_text(surf, text, x + dx, y + dy, (0, 0, 0))
    draw_text(surf, text, x, y, color)


def draw_rotated_icon(surf, rows, cx, cy, angle_deg, color):
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
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw_rotated_icon(surf, rows, cx + dx, cy + dy, angle_deg, (0, 0, 0))
    draw_rotated_icon(surf, rows, cx, cy, angle_deg, color)


# The modes import the settings and helpers above, so they come after them
import clock
import map as mapmode
import radar
import track


def make_mode(session, name, center=None):
    if name == "radar":
        mode = radar.RadarMode(session)
    elif name == "map":
        mode = mapmode.MapMode(session, center)
    elif name == "clock":
        mode = clock.ClockMode(session)
    else:
        mode = track.TrackMode(session, name)
    mode.last_fetch = 0.0
    return mode


def set_caption(mode):
    pygame.display.set_caption(f"Tracking {mode.flight}" if mode.kind == "track" else mode.kind.title())


def plane_at(mode, point):
    if not mode.view:
        return None
    now, best = time.time(), None
    for p in mode.hoverable():
        x, y = mode.view.project(*mapmode.current_position(p, now))
        d = math.hypot(x - point[0], y - point[1])
        if d <= 6 and mode.view.inner.collidepoint(round(x), round(y)) and (best is None or d < best[0]):
            best = (d, p, round(x), round(y))
    return best[1:] if best else None


def draw_hover(low, mode, point):
    found = plane_at(mode, point)
    if found:
        p, x, y = found
        text, box = clean(p["callsign"] or p["reg"] or p["icao24"]), mode.view.inner
        tx = max(box.left + 1, min(box.right - 1 - text_width(text), x - text_width(text) // 2))
        ty = y - 14 if y - 14 >= box.top + 1 else y + 8
        draw_text_outlined(low, text, tx, ty, COLOR_CALLSIGN)


USAGE = f"""usage: python src/aerobit.py [radar | map | clock | track FLIGHT] [options]
  radar      (the default) nearest airline flight with a confirmed route
  map        every plane and helicopter nearby, full screen
  clock      the time and date at the .env location, with a dot for each aircraft on the default map
  clock set  a timer, as days:hours:minutes:seconds; type only what you need (30 = 30 s, 1:30 = 1 min 30 s);
  clock cancel  stop the timer and go back to the plain clock
  track FLIGHT  track one flight anywhere, e.g. track UAL123 or track UA123
  r=         map radius in miles (defaults: {RADAR_RADIUS_MILES:g} radar, {MAP_RADIUS_MILES:g} map, {TRACK_NO_ROUTE_RADIUS_MILES:g} tracking without a route)
  airports=  radar/tracking: show the route's departure and arrival airports; map: show every airline airport
             (default: true)
  airfields= map mode only: show every other airport too (default: false)
  num_flights=  how many of the closest flights to show, or all (defaults: 1 radar, all map; 0 shows none)
  heli_only= map: only helicopters; radar: the selected flight plus the closest helicopters, num_flights in all
             (default: false)
  roads=     draw roads on the satellite map (default: false)
  set_coords= radar and map: move the center to LAT,LON until the program closes
             (default: CENTER_COORDS from .env; tracking mode always uses .env)
  recenter, recenter set  radar and map: back to the last set_coords center (or .env), undoing a clicked map center
  recenter default        radar and map: back to the .env center, undoing set_coords too

While running: press Enter in the display (or type in this terminal) to give a new command;
R = radar, M = map, C = clock, T = track the blue flight (radar and map mode).
A command naming a mode starts it fresh; options alone (like r=30) change only those in the current mode."""

CREDITS = """AeroBit, by Luca Pishos. MIT License.
https://github.com/Pqrticle/AeroBit

Data
  adsb.fi            live aircraft
  OpenSky Network    flight paths
  adsbdb             routes, airlines and flight numbers
  adsb.lol           backup live aircraft and routes
  Aviasales          airline logos
  OurAirports        airport list
  Esri               satellite imagery

Airline names and logos belong to their owners. AeroBit isn't affiliated
with any airline or data provider."""

OPTION_SETTINGS = ("RADAR_RADIUS_MILES", "MAP_RADIUS_MILES", "TRACK_NO_ROUTE_RADIUS_MILES", "SHOW_AIRPORTS",
                   "MAP_SHOW_AIRFIELDS", "NUM_FLIGHTS", "MAP_NUM_FLIGHTS",
                   "HELI_ONLY", "MAP_HELI_ONLY", "SHOW_ROADS")
DEFAULTS = {name: globals()[name] for name in OPTION_SETTINGS}


def parse_command(args, base=None):
    """Returns (mode, settings) for a command; raises ValueError on mistakes."""
    mode, settings = None, dict(base or DEFAULTS)
    words = []
    for w in " ".join(args).replace(", ", ",").replace(" ,", ",").split():
        if words and words[-1].lower() == "recenter" and w.lower() in ("set", "default"):
            words[-1] += " " + w  # "recenter set" and "recenter default" are one option
        elif words and (words[-1].lower() == "clock" and w.lower() in ("set", "cancel")
                        or words[-1].lower() in ("clock set", "track")):
            words[-1] += " " + w  # "clock set 1:30", "clock cancel" and "track UAL123" are one command
        else:
            words.append(w)
    for arg in words:
        key, has_value, value = arg.partition("=")
        key, value = key.lower(), value.lower()
        if not has_value:
            if key in ("help", "-h", "--help", "/?"):
                return "help", settings
            if key == "credits":  # not listed in help
                return "credits", settings
            if key in ("recenter", "recenter set", "recenter default"):  # the last set_coords center, or .env
                settings["CENTER_COORDS"] = HOME_COORDS if key == "recenter default" else CENTER_COORDS
                continue
            if mode:
                raise ValueError("Only one mode or flight at a time")
            if key.startswith("clock set"):
                settings["TIMER_SECONDS"] = parse_duration(key[9:].strip())
                key = "clock"
            elif key == "clock cancel":
                settings["TIMER_SECONDS"] = 0
                key = "clock"
            if key == "track":
                raise ValueError("track needs a flight, like track UAL123")
            if key.startswith("track "):
                mode = arg[6:].strip()
            elif key in ("radar", "map", "clock"):
                mode = key
            else:
                raise ValueError(f"Unknown command {arg}")
        elif key in ("r", "radius"):
            try:
                r = float(value)
            except ValueError:
                r = 0
            if not 0 < r <= 5000:
                raise ValueError("r= needs a number, like r=15")
            r = int(r) if r == int(r) else r
            for name in ("RADAR_RADIUS_MILES", "MAP_RADIUS_MILES", "TRACK_NO_ROUTE_RADIUS_MILES"):
                settings[name] = r
        elif key == "set_coords":
            coords = parse_coords(value)
            if not coords:
                raise ValueError("set_coords= needs LAT,LON, like set_coords=40.71,-74.01")
            settings["CENTER_COORDS"] = coords
        elif key == "num_flights":
            if value == "all":
                n = None
            elif value.isdigit():
                n = int(value)
            else:
                raise ValueError("num_flights= needs a number or all")
            settings["NUM_FLIGHTS"] = settings["MAP_NUM_FLIGHTS"] = n
        elif key in ("airports", "airfields", "heli_only", "roads"):
            if value in ("true", "yes", "on", "1"):
                on = True
            elif value in ("false", "no", "off", "0"):
                on = False
            else:
                raise ValueError(f"{key}= should be true or false")
            names = {"airports": ("SHOW_AIRPORTS",),
                     "airfields": ("MAP_SHOW_AIRFIELDS",), "heli_only": ("HELI_ONLY", "MAP_HELI_ONLY"),
                     "roads": ("SHOW_ROADS",)}[key]
            for name in names:
                settings[name] = on
        else:
            raise ValueError(f"Unknown option {arg}")
    return mode, settings


def parse_duration(text):
    """Seconds in days:hours:minutes:seconds, filled from the right: 30, 1:30, 2:00:00, 1:00:00:00."""
    parts = text.split(":")
    if not text or len(parts) > 4 or not all(p.isdigit() for p in parts):
        raise ValueError("clock set needs a time like 30, 1:30 or 1:00:00")
    return sum(int(p) * unit for p, unit in zip(reversed(parts), (1, 60, 3600, 86400)))


def apply_settings(settings):
    seconds = settings.pop("TIMER_SECONDS", None)
    if seconds is not None:
        clock.set_timer(seconds)
    globals().update(settings)


def run_command(session, text, current):
    """Returns the new mode, an error message, or None to keep the current mode."""
    words = text.split()
    for i, w in enumerate(words):
        if w.lower().endswith(".py"):
            words = words[i + 1:]
            break
    center = None
    try:
        mode, settings = parse_command(words)
        if mode is None:
            mode = current.name
            settings = parse_command(words, {name: globals()[name] for name in OPTION_SETTINGS})[1]
            if current.kind == "map" and "CENTER_COORDS" not in settings:
                center = current.center  # options alone keep a map moved by clicking
    except ValueError as e:
        return str(e)
    if mode in ("help", "credits"):
        print("\n" + (USAGE if mode == "help" else CREDITS))
        return None  # the current mode keeps running
    if "recenter" in (w.lower() for w in words) and mode not in ("radar", "map"):
        return "recenter only works in radar and map"
    apply_settings(settings)
    return make_mode(session, mode, center)


def describe(mode):
    if mode.kind == "clock":
        return "clock" + (f" (timer: {clock.timer_text()} left)" if clock.timer else "")
    is_map = mode.kind == "map"
    r = MAP_RADIUS_MILES if is_map else TRACK_NO_ROUTE_RADIUS_MILES if mode.kind == "track" else RADAR_RADIUS_MILES
    n = MAP_NUM_FLIGHTS if is_map else NUM_FLIGHTS
    options = [("r", f"{r:g}"), ("airports", SHOW_AIRPORTS),
               ("airfields", MAP_SHOW_AIRFIELDS if is_map else None),
               ("num_flights", None if mode.kind == "track" else "all" if n is None else n),
               ("heli_only", None if mode.kind == "track" else MAP_HELI_ONLY if is_map else HELI_ONLY),
               ("roads", SHOW_ROADS)]
    name = f"track {mode.name}" if mode.kind == "track" else mode.name
    return f"{name} " + " ".join(f"{k}={str(v).lower()}" for k, v in options if v is not None)


def build_dot_mask(dot):
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


def draw_command_bar(low, text, color, typing=False):
    max_chars = (GRID_W - 4) // 6
    text = clean(text)[-max_chars:] if typing else fit(text, max_chars)
    low.fill((0, 0, 0), (0, GRID_H - 11, GRID_W, 11))
    low.fill(COLOR_LINE, (0, GRID_H - 12, GRID_W, 1))
    draw_text(low, text, 2, GRID_H - 9, color)


def grid_on_screen():
    """(x, y, LED size) of the LED grid, scaled to fit the window and centered."""
    sw, sh = pygame.display.get_surface().get_size()
    dot = max(1, min(sw // GRID_W, sh // GRID_H))
    return (sw - GRID_W * dot) // 2, (sh - GRID_H * dot) // 2, dot


def show(low, cache, overlay=None):
    """overlay(screen, x, y, LED size), if given, draws on top at the window's full resolution."""
    ox, oy, dot = grid_on_screen()
    if cache.get("dot") != dot:
        cache["mask"], cache["dot"] = build_dot_mask(dot), dot
    screen = pygame.display.get_surface()
    screen.fill((0, 0, 0))
    screen.blit(pygame.transform.scale(low, (GRID_W * dot, GRID_H * dot)), (ox, oy))
    screen.blit(cache["mask"], (ox, oy))
    if overlay:
        overlay(screen, ox, oy, dot)
    pygame.display.flip()


class Session(requests.Session):
    """Lets the window answer Windows between downloads, so it isn't marked as not responding."""
    def request(self, *args, **kwargs):
        if threading.current_thread() is threading.main_thread() and pygame.display.get_init():
            pygame.event.pump()
        return super().request(*args, **kwargs)


def read_terminal(commands):
    try:
        for line in sys.stdin:
            commands.put(line)
    except Exception:
        pass


def main(mode="radar"):
    pygame.display.init()
    fullscreen = START_FULLSCREEN
    open_window(fullscreen)
    scene = pygame.Surface((GRID_W, GRID_H))
    low = pygame.Surface((GRID_W, GRID_H))
    mask_cache = {}

    session = Session()
    session.headers["User-Agent"] = "led-flight-display"
    mapmode.load_airports(session)
    current = make_mode(session, mode)
    set_caption(current)
    pygame.key.start_text_input()

    commands = queue.Queue()
    threading.Thread(target=read_terminal, args=(commands,), daemon=True).start()
    print("Switch modes any time: type a command here (like: map r=30) and press Enter,")
    print("or in the display press Enter to type one, R for radar, M for map, T to track the blue flight.")
    typing = None
    message, message_until = "", 0.0
    last_scene = 0.0
    last_mouse = 0.0
    ox, oy, dot = grid_on_screen()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEMOTION:
                last_mouse = time.time()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and typing is None and current.kind != "track":
                led = ((event.pos[0] - ox) // dot, (event.pos[1] - oy) // dot)
                found = plane_at(current, led)
                if found and found[0]["callsign"]:
                    commands.put(("track", found[0]["callsign"]))
                elif found:
                    print("That aircraft isn't broadcasting a flight number, so it can't be tracked")
                elif current.kind == "map" and current.view.inner.collidepoint(led):
                    lat, lon = current.view.unproject(*led)
                    current, message, last_scene = make_mode(session, "map", (lat, lon)), "", 0.0
                    print(f"Map centered on {lat:.4f}, {lon:.4f}")
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
                elif event.key == pygame.K_c:
                    commands.put("clock")
                elif event.key == pygame.K_t:
                    target = current.blue_flight()
                    if target and target["callsign"]:
                        commands.put(("track", target["callsign"]))

        while not commands.empty():
            item = commands.get()
            if isinstance(item, tuple):  # a clicked or T-key aircraft: its callsign isn't parsed as a command
                globals().update(DEFAULTS)
                result = make_mode(session, item[1])
            else:
                text = item.strip()
                if not text:
                    continue
                result = run_command(session, text, current)
            if result is None:
                continue
            if isinstance(result, str):
                print(result)
                message, message_until = result, time.time() + 6
            else:
                current, message, last_scene = result, "", 0.0
                set_caption(current)
                print(f"Now showing: {describe(current)}")

        now = time.time()
        # Downloads can take seconds, so they wait while you type
        if typing is None and now - current.last_fetch >= REFRESH_SECONDS and current.kind != "clock":
            current.last_fetch = now
            current.refresh(session)

        now = time.time()
        if now - last_scene >= 0.25 or current.kind == "clock":  # the clock's sky plane moves every frame
            current.render(scene)
            last_scene = now

        low.blit(scene, (0, 0))
        ox, oy, dot = grid_on_screen()
        if pygame.mouse.get_focused():
            mx, my = pygame.mouse.get_pos()
            draw_hover(low, current, ((mx - ox) // dot, (my - oy) // dot))
        if typing is not None:
            cursor = "_" if int(now * 2) % 2 else " "
            draw_command_bar(low, "> " + typing + cursor, COLOR_CALLSIGN, typing=True)
        elif message and now < message_until:
            draw_command_bar(low, message, COLOR_DOWN)
        show(low, mask_cache, getattr(current, "overlay", None))
        # the clock refreshes right after showing a new second, so its quick request is done before the next one
        if (typing is None and current.kind == "clock" and time.time() - current.last_fetch >= REFRESH_SECONDS
                and time.time() % 1 < 0.1):
            current.last_fetch = time.time()
            current.refresh(session)
        quick = typing is not None or now - last_mouse < 1
        pause = 0.03 if quick else 0.25
        if current.kind == "clock":  # 10 frames a second, and wake right as the next second starts
            pause = min(pause, 0.1, 1.005 - time.time() % 1)
        time.sleep(pause)
    pygame.quit()


if __name__ == "__main__":
    try:
        start_mode, start_settings = parse_command(sys.argv[1:])
    except ValueError as e:
        sys.exit(f"{e}.\n\n{USAGE}")
    if start_mode == "help":
        sys.exit("\n" + USAGE)
    if start_mode == "credits":
        sys.exit("\n" + CREDITS)
    apply_settings(start_settings)
    main(start_mode or "radar")
