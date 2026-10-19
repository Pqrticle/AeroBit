import re
import time

import aerobit as app
from aerobit import (COLOR_CALLSIGN, COLOR_LABEL, COLOR_OFF, COLOR_TEXT, GRID_W, HOME_COORDS, clean,
                     draw_outlined_icon, draw_text_center)
from map import (HELICOPTER_DOT, MapView, aircraft_color, current_position, distance_miles, draw_airports,
                 draw_dotted_path, draw_home, draw_map_base, feed_get, fetch_trace, get_background,
                 map_icon, nearest_town, parse_plane, plane_box, takeoff_airport)
from radar import (PANEL, adsbdb, best_route, draw_info, flight_path, info_cache, look_up_flight,
                   lookup_routeset, near_labels, note_trends)

CALLSIGN_URLS = [
    "https://opendata.adsb.fi/api/v2/callsign/{cs}",
    "https://api.adsb.lol/v2/callsign/{cs}",
]


def callsign_options(session, flight):
    flight = clean(flight).replace(" ", "")
    options = [flight]
    response = adsbdb(session, "callsign/" + flight)
    route = response.get("flightroute") if isinstance(response, dict) else None
    if isinstance(route, dict) and route.get("callsign_icao"):
        options.append(route["callsign_icao"])
    m = re.fullmatch(r"([A-Z0-9]{2})(\d{1,4}[A-Z]?)", flight)
    if m:
        airlines = adsbdb(session, "airline/" + m.group(1))
        if isinstance(airlines, list):
            options += [a["icao"] + m.group(2) for a in airlines if a.get("icao") and a.get("iata") == m.group(1)]
    return list(dict.fromkeys(options))


def fetch_tracked(session, callsigns):
    now = time.time()
    for cs in callsigns:
        for ac in feed_get(session, CALLSIGN_URLS, cs=cs):
            p = parse_plane(ac, now)
            if p and p["callsign"]:
                return p
    return None


def track_flight(session, p, trails):
    if p["callsign"] not in info_cache:
        look_up_flight(session, p["callsign"])
    lookup_routeset(session, [p])
    trail = trails.get(p["icao24"])
    if trail is None:
        pts = fetch_trace(session, p["icao24"], trim_far=False)
        trail = trails[p["icao24"]] = {"pts": pts, "downloaded": bool(pts)}
    trail["pts"].append((p["lat"], p["lon"], time.time()))


def tracking_view(view, rect, p, trail, now):
    here = current_position(p, now)
    info = best_route(p)
    if info:
        behind, ahead = flight_path(p, trail, now)
        points = [info["orig"]["pos"], info["dest"]["pos"]] + behind + ahead
        if view and all(view.contains(*pt, margin=2) for pt in points[::10] + points[:2]):
            return view
        return MapView.fitting(rect, points)
    took_off = takeoff_airport(p["icao24"])
    if took_off:
        behind, _ = flight_path(p, trail, now)
        points = [took_off["pos"], here] + behind
        if view and all(view.contains(*pt, margin=2) for pt in points[::10] + points[:2]):
            return view
        return MapView.fitting(rect, points, min_radius_miles=50)
    if view and distance_miles(*view.center, *here) < app.TRACK_NO_ROUTE_RADIUS_MILES / 4:
        return view
    return MapView.around(rect, here, app.TRACK_NO_ROUTE_RADIUS_MILES)


def draw_tracking_map(low, view, background, p, trail, now):
    draw_map_base(low, view, background)
    low.set_clip(view.inner)
    trail_color = COLOR_TEXT if background else COLOR_LABEL
    behind, ahead = flight_path(p, trail, now)
    draw_dotted_path(low, view, behind, trail_color, 2)
    draw_dotted_path(low, view, ahead, trail_color, 4)
    draw_home(low, view, HOME_COORDS)
    info = best_route(p)
    took_off = takeoff_airport(p["icao24"])
    route_airports = [] if not app.SHOW_AIRPORTS else [info["orig"], info["dest"]] if info else [took_off] if took_off else []
    icon = map_icon(p)
    draw_airports(low, view, (), [plane_box(view, p, now, icon)], route_airports)
    px, py = (round(v) for v in view.project(*current_position(p, now)))
    draw_outlined_icon(low, icon, px, py, 0 if icon is HELICOPTER_DOT else (p["track"] or 0) - 90, aircraft_color(p, True))
    low.set_clip(None)


def render_tracking(low, view, background, p, trail, flight, now, near=None):
    low.fill(COLOR_OFF)
    if not p:
        draw_text_center(low, flight, GRID_W // 2, 40, COLOR_CALLSIGN, 3)
        draw_text_center(low, "NOT BROADCASTING RIGHT NOW", GRID_W // 2, 72, COLOR_LABEL)
        draw_text_center(low, "(LANDED, NOT DEPARTED, OR NO DATA)", GRID_W // 2, 84, COLOR_LABEL)
        return
    shown = draw_info(low, p, 1, now, trail, tracking=True, near=near)
    draw_tracking_map(low, view, background, p, trail, now)
    return shown


class TrackMode:
    kind = "track"

    def __init__(self, session, name):
        self.name = name
        self.flight = clean(name)
        self.options = callsign_options(session, self.flight)
        self.view = self.background = self.selected = self.near = self.last_near = None
        self.trails = {}

    def refresh(self, session):
        try:
            p = self.selected = fetch_tracked(session, self.options)
            if p:
                note_trends([p])
                self.near = nearest_town((p["lat"], p["lon"]))
                self.options = [p["callsign"]]
                track_flight(session, p, self.trails)
                view = tracking_view(self.view, PANEL, p, self.trails[p["icao24"]], time.time())
                if view is not self.view:
                    self.view, self.background = view, get_background(session, view)
        except ConnectionError:
            pass  # each feed already said what's wrong

    def hoverable(self):
        return [self.selected] if self.selected else []

    def blue_flight(self):
        return None

    def render(self, low):
        trail = self.trails.get(self.selected["icao24"]) if self.selected else None
        # when no version of the current town fits, the last label that did stays up; before any has, the town
        # without NEAR, or nothing
        fallback = self.last_near or (self.near.split(", ")[0] if self.near else None)
        near = near_labels(self.near) + [fallback] if self.near else None
        self.last_near = render_tracking(low, self.view, self.background, self.selected, trail, self.flight,
                                         time.time(), near) or self.last_near
