import math
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pygame
from timezonefinder import TimezoneFinder

import aerobit as app
from aerobit import GRID_H, GRID_W, HOME_COORDS
from map import FEED_URLS, NM_TO_MILES, PLANE_ICON, MapView, aircraft_color, parse_plane

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
ZONE = ZoneInfo(TimezoneFinder().timezone_at(lat=HOME_COORDS[0], lng=HOME_COORDS[1]) or "UTC")  # the .env location's
DOT, DOT_GAP = 2, 3
AMBER = (255, 190, 0)
STARS = ((20, 12), (60, 30), (110, 8), (170, 22), (220, 14), (240, 40), (35, 50), (140, 45), (200, 60), (90, 20),
         (250, 6), (12, 70), (246, 80), (75, 62))
BAR_PLANE = [
    "....##.......",
    ".....##......",
    "......##.....",
    "#......##....",
    "##....#####..",
    "############.",
    "##....#####..",
    "#......##....",
    "......##.....",
    ".....##......",
    "....##.......",
]
BAR_Y, BAR_LEFT, BAR_RIGHT = 120, 40, 216
FINISHED_SECONDS = 10  # a finished timer blinks 0:00 this long, then the clock goes back to normal

timer = None  # (start, end) in time.time() seconds; kept across mode switches


def set_timer(seconds):
    global timer
    timer = (time.time(), time.time() + seconds) if seconds else None


def timer_text(now=None):
    left = max(0, math.ceil(timer[1] - (now or time.time())))
    d, rest = divmod(left, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    return f"{d}:{h:02}:{m:02}:{s:02}" if d else f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def font(file, size):
    """A Windows font, or pygame's own where it isn't installed."""
    path = os.path.join(FONT_DIR, file)
    return pygame.font.Font(path if os.path.exists(path) else None, size)


def ordinal(day):
    return "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def sun_position(lat, lon, when):
    """The sun's elevation in degrees, and whether it's morning (NOAA's approximation)."""
    utc = when.astimezone(timezone.utc)
    g = 2 * math.pi / 365 * (utc.timetuple().tm_yday - 1 + (utc.hour - 12) / 24)
    eq_time = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    hour_angle = (utc.hour * 60 + utc.minute + utc.second / 60 + eq_time + 4 * lon) / 4 - 180
    hour_angle = (hour_angle + 180) % 360 - 180
    lat, ha = math.radians(lat), math.radians(hour_angle)
    elevation = math.degrees(math.asin(math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)))
    return elevation, hour_angle < 0


def sky_name(when):
    elevation, morning = sun_position(*HOME_COORDS, when)
    if elevation >= 8:
        return "day"
    if elevation < -6:
        return "night"
    return "sunrise" if morning else "sunset"


# The skies, drawn on the LED grid
def gradient(low, stops):
    """stops: [(position 0-1, color), ...] from top to bottom."""
    for y in range(GRID_H):
        f = y / (GRID_H - 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= f <= p1:
                k = (f - p0) / (p1 - p0) if p1 > p0 else 0
                low.fill([round(a + (b - a) * k) for a, b in zip(c0, c1)], (0, y, GRID_W, 1))
                break


def disc(low, cx, cy, r, color):
    for y in range(-r, r + 1):
        for x in range(-r, r + 1):
            if x * x + y * y <= r * r + r:
                low.fill(color, (cx + x, cy + y, 1, 1))


def cloud(low, x, y, color, shade):
    for c, drop in ((shade, 1), (color, 0)):
        for dx, dy, r in ((0, 0, 4), (6, -3, 5), (13, 0, 4), (7, 2, 4)):
            disc(low, x + dx, y + dy + drop, r, c)


def hills(low, color, base=134):
    for x in range(GRID_W):
        top = base + round(3 * math.sin(x / 23) + 2 * math.sin(x / 9 + 1))
        low.fill(color, (x, top, 1, GRID_H - top))


def stars(low, below, color=(235, 235, 235)):
    for x, y in STARS:
        if y < below:
            low.fill(color if x % 3 else (160, 170, 220), (x, y, 1, 1))


def sunrise(low):
    gradient(low, [(0, (25, 45, 110)), (0.45, (110, 110, 170)), (0.78, (235, 140, 150)), (1, (245, 170, 130))])
    stars(low, 30)
    disc(low, 30, 126, 16, (255, 190, 70))
    disc(low, 30, 126, 12, (255, 250, 215))
    hills(low, (60, 45, 80))


def day(low):
    gradient(low, [(0, (60, 140, 230)), (1, (175, 220, 255))])
    disc(low, 228, 40, 9, (255, 236, 150))
    disc(low, 228, 40, 6, (255, 250, 210))
    for x, y in ((14, 48), (40, 122), (200, 118)):
        cloud(low, x, y, (245, 248, 255), (190, 205, 230))
    hills(low, (70, 140, 90))


def sunset(low):
    gradient(low, [(0, (35, 20, 70)), (0.4, (120, 40, 100)), (0.72, (220, 70, 60)), (1, (255, 150, 50))])
    stars(low, 40, (255, 230, 240))
    disc(low, 226, 126, 17, (255, 110, 50))
    disc(low, 226, 126, 13, (255, 185, 90))
    hills(low, (45, 15, 40))


def night(low):
    gradient(low, [(0, (8, 14, 40)), (1, (28, 40, 90))])
    stars(low, GRID_H)
    disc(low, 26, 42, 7, (230, 230, 210))
    disc(low, 29, 40, 6, (12, 20, 52))  # crescent moon
    hills(low, (10, 16, 34))


# each sky's drawing, and its plane's color with the contrail's bright and faded ends
SKIES = {"sunrise": (sunrise, (40, 50, 110), (255, 255, 255), (230, 170, 190)),
         "day": (day, (210, 40, 40), (255, 255, 255), (200, 225, 250)),
         "sunset": (sunset, (20, 10, 30), (255, 220, 180), (170, 70, 100)),
         "night": (night, (255, 170, 40), (210, 210, 220), (40, 50, 90))}


def sky_plane(low, now, plane, bright, faded):
    """A plane crossing high over the clock every 90 seconds, taking 72 to cross. The contrail's tail fades
    into the sky, and once the plane is gone the whole contrail fades out over 3 seconds."""
    f = (now % 90) / 72
    gone = max(0.0, (f - 1) * 72 / 3)  # 0 while flying, 1 once the contrail has faded out
    if gone >= 1:
        return
    path = lambda g: (-10 + g * (GRID_W + 20), 26 - 8 * math.sin(g * math.pi))  # noqa: E731
    # the dots stay put along the path, so they don't jitter as the plane moves; the nearest hide behind it
    step = 0.012
    for n in range(math.floor(min(f, 1) / step), -1, -1):
        k = (f - n * step) / step  # how many dot spaces behind the plane
        if k >= 20:
            break
        x, y = (round(v) for v in path(n * step))
        if k < 2 or not (0 <= x < GRID_W and 0 <= y < GRID_H):
            continue
        color = [a + (b - a) * k / 20 for a, b in zip(bright, faded)]
        fade = min(1, max(gone, (k - 13) / 6))  # how far into the sky this dot has faded
        sky = low.get_at((x, y))
        low.fill([round(c + (s - c) * fade) for c, s in zip(color, sky)], (x, y, 1, 1))
    if f <= 1:
        x, y = path(f)
        angle = math.degrees(math.atan2(-8 * math.pi * math.cos(f * math.pi) / (GRID_W + 20), 1))
        app.draw_outlined_icon(low, PLANE_ICON, round(x), round(y), angle, plane)


def progress_bar(low, progress, dim=(90, 90, 100)):
    """The timer's flight: an amber trail behind the plane, heading for the arrival ring."""
    x = BAR_LEFT + int((BAR_RIGHT - BAR_LEFT) * progress)  # one LED forward each 1/176 of the timer
    low.fill(AMBER, (BAR_LEFT, BAR_Y, max(0, x - BAR_LEFT - 6), 1))
    low.fill(AMBER, (BAR_LEFT - 1, BAR_Y - 1, 3, 3))
    for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
        low.fill(dim, (BAR_RIGHT + dx, BAR_Y + dy, 1, 1))
    app.draw_outlined_icon(low, BAR_PLANE, x, BAR_Y, 0, (255, 255, 255))


class ClockMode:
    """The time and date at the .env location over a sky for the time of day, a dot for each aircraft on the
    default map, and a countdown when a timer is set."""
    kind = "clock"

    def __init__(self, session):
        self.name = "clock"
        self.view = None  # nothing to hover or click
        pygame.font.init()
        self.fonts, self.fonts_dot = {}, None
        self.sky, self.sky_key = pygame.Surface((GRID_W, GRID_H)), None
        self.text, self.text_key = None, None
        self.map_view = MapView.around(pygame.Rect(0, 0, GRID_W, GRID_H), HOME_COORDS, app.DEFAULTS["MAP_RADIUS_MILES"])
        self.radius = math.ceil(self.map_view.reach_miles())
        self.aircraft = []

    def refresh(self, session):
        """One quick request with a short timeout: the dots are secondary, and a slow answer mustn't hold up the time."""
        lat, lon = HOME_COORDS
        try:
            resp = session.get(FEED_URLS[0].format(lat=lat, lon=lon, dist=min(self.radius / NM_TO_MILES, 250)), timeout=(0.4, 0.5))
            found = resp.json().get("ac") or resp.json().get("aircraft") or []
        except Exception:
            return  # keep the last dots
        now = time.time()
        planes = sorted((p for p in (parse_plane(ac, now, HOME_COORDS) for ac in found) if p), key=lambda p: p["dist"])
        self.aircraft = [p for p in planes if self.map_view.contains(p["lat"], p["lon"])]

    def hoverable(self):
        return []

    def blue_flight(self):
        return None

    def render(self, low):
        global timer
        now = time.time()
        if timer and now > timer[1] + FINISHED_SECONDS:
            timer = None
        name = sky_name(datetime.now(ZONE))
        if self.sky_key != name:
            draw, *_ = SKIES[name]
            draw(self.sky)
            self.sky_key = name
        low.blit(self.sky, (0, 0))
        if timer:
            progress_bar(low, min(1, (now - timer[0]) / max(1e-9, timer[1] - timer[0])))
        else:
            sky_plane(low, now, *SKIES[name][1:])
        # one dot per aircraft, nearest first, centered across the top until the row is full
        step = DOT + DOT_GAP
        count = min(len(self.aircraft), (GRID_W - 4 + DOT_GAP) // step)
        x = (GRID_W - (count * step - DOT_GAP)) // 2
        for i, p in enumerate(self.aircraft[:count]):
            low.fill(aircraft_color(p), (x + i * step, 8, DOT, DOT))

    def font(self, file, size, dot):
        if self.fonts_dot != dot:
            self.fonts, self.fonts_dot = {}, dot
        if (file, size) not in self.fonts:
            self.fonts[file, size] = font(file, size * dot)
        return self.fonts[file, size]

    def overlay(self, screen, ox, oy, dot):
        """The time, date and countdown, drawn smoothly at the window's full resolution on top of the LED grid,
        with a soft shadow so they read on any sky. Redrawn only when the text changes."""
        now = datetime.now(ZONE)
        countdown = None
        if timer:
            countdown = timer_text() if time.time() < timer[1] or int(time.time()) % 2 else ""  # 0:00 blinks
        key = (dot, f"{now:%I:%M:%S %p %x}", countdown)
        if self.text_key != key:
            self.text, self.text_key = self.draw_text(now, countdown, dot), key
        shadow, text = self.text
        screen.blit(shadow, (ox + dot // 2 + 1, oy + dot // 2 + 1))
        screen.blit(text, (ox, oy))

    def draw_text(self, now, countdown, dot):
        layer = pygame.Surface((GRID_W * dot, GRID_H * dot), pygame.SRCALPHA)
        big = (40 if countdown is not None else 70)
        small = (10 if countdown is not None else 16)
        clock = self.font("segoeuil.ttf", big, dot).render(f"{now:%I}".lstrip("0") + f":{now:%M}", True, (255, 255, 255))
        ampm = self.font("segoeuisl.ttf", small, dot).render(f"{now:%p}", True, (200, 200, 210))
        seconds = self.font("segoeuisl.ttf", small, dot).render(f"{now:%S}", True, (140, 140, 150))
        date = self.font("segoeui.ttf", 10 if countdown is not None else 15, dot).render(
            f"{now:%A}, {now:%B} {now.day}{ordinal(now.day)}, {now.year}", True, (210, 210, 220))
        # center by the visible ink, not the font's padding: the time with AM/PM and seconds as one group
        ink, date_ink = clock.get_bounding_rect(), date.get_bounding_rect()
        side = clock.get_width() + 3 * dot
        right = side + max(ampm.get_bounding_rect().right, seconds.get_bounding_rect().right)
        x = (GRID_W * dot - (right - ink.left)) // 2 - ink.left
        if countdown is None:  # the time and date as one block in the middle
            y = (GRID_H * dot - (84 * dot + date_ink.bottom - ink.top)) // 2 - ink.top
            date_y = y + 84 * dot
            layer.blit(ampm, (x + side, y + 20 * dot))
            layer.blit(seconds, (x + side, y + 40 * dot))
        else:  # moved up and smaller, with the countdown and its flight below
            y = 20 * dot - ink.top
            date_y = 56 * dot - date_ink.top
            top = y + ink.top + dot
            layer.blit(ampm, (x + side, top - ampm.get_bounding_rect().top))
            layer.blit(seconds, (x + side, top + 15 * dot - seconds.get_bounding_rect().top))
            if countdown:
                count = self.font("segoeuil.ttf", 30, dot).render(countdown, True, AMBER)
                c_ink = count.get_bounding_rect()
                layer.blit(count, ((GRID_W * dot - c_ink.width) // 2 - c_ink.left, 78 * dot - c_ink.top))
        layer.blit(clock, (x, y))
        layer.blit(date, ((GRID_W * dot - date_ink.width) // 2 - date_ink.left, date_y))
        shadow = layer.copy()
        shadow.fill((0, 0, 0), special_flags=pygame.BLEND_RGB_MULT)
        shadow.set_alpha(150)
        return shadow, layer
