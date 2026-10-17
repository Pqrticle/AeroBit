# AeroBit

AeroBit is a live flight display drawn as a 256×144 LED grid in a desktop window. It pulls aircraft positions from free ADS-B feeds, works out where airline flights are going, and shows them on a satellite map. No account or API key is needed.

## Modes

**Radar** (the default) shows the nearest airline flight with a confirmed route: flight number, airline name and logo, origin and destination, distance flown and left, departure time and ETA, among additional technical data, on a map with the path flown so far and the path still to fly.

**Map** is a full-screen view of every aircraft in range, including small planes and helicopters, each with its recent path and heading. It doesn't need routes, so it shows everything the feed sees. Blue indicates the aircraft is the selected flight in radar mode, red is military, and a custom color set in `defaults.yml` marks a flagged aircraft.

**Tracking** follows one flight anywhere in the world. Give it a flight number (`AA123`) or an ICAO callsign (`AAL123`) and it keeps the whole route in view. If the route can't be confirmed, it shows the takeoff airport and the path since, or just the aircraft itself.

## How routes are found

The free route databases are often out of date or guessed, and airlines reuse flight numbers, so a callsign lookup alone is often wrong. AeroBit only shows a route once three independent sources agree:

1. **Backtracking the flight path.** It downloads the aircraft's path for the day and looks at where it begins. If the path starts on the ground or below 2,500 ft, the takeoff point is matched to the nearest airline airport within 10 miles. That airport is the only origin it will accept.
2. **Comparing with the databases.** It looks up the callsign in adsb.lol and adsbdb and accepts only a route that departs from that takeoff airport. The destination comes from the matching route, preferring adsb.lol, which lists multi-stop flights one leg at a time.
3. **Three sanity checks against the live position.** The route is rejected if any of these fail:
   - **Position:** the aircraft is more than 40 miles or 15% of the route length (whichever is larger) off the direct path between the two airports.
   - **Heading:** away from either end of the route (more than 30 miles from each), it's pointed more than 60° away from the destination.
   - **Climb and descent:** below 12,000 ft on a route over 100 miles, it's descending within 50 miles of the origin (so it's really landing there), or climbing within 50 miles of the destination (so it really just left there).

A flight shown in radar mode has passed all of this, so its route is almost always right. The cost is coverage: a flight whose takeoff the free feeds didn't receive, one with no matching database entry, or one from an operator without an airline code never qualifies, so radar mode skips plenty of real airline flights. AeroBit also downloads at most 4 flight paths per refresh to stay under the free APIs' rate limits, so a new flight can take a few refreshes to show up. With paid data you could show more flights; with free data, this is the trade between accuracy and coverage.

## Setup

Requires Python 3.10 or newer (developed on 3.14).

```bash
git clone https://github.com/Pqrticle/AeroBit.git
```

```bash
cd AeroBit
```

```bash
pip install -r requirements.txt
```

This installs [pygame-ce](https://pyga.me/), [requests](https://requests.readthedocs.io/) and [PyYAML](https://pyyaml.org/). pygame-ce conflicts with the original `pygame` package, so uninstall that one first if needed. A virtual environment is a good idea but isn't required.

Rename `.env.example` to `.env` and fill it in

`CENTER_COORDS` is required: the latitude and longitude of a specific place (e.g., your location), in decimal degrees, that radar and map mode center on.
`FLAGGED_AIRCRAFT` is an optional comma-separated list of callsigns or tail numbers to color separately.

Startup defaults, like the radar radius and whether roads are drawn, are in `defaults.yml` (see [Settings](#settings)).

Then run it:

```bash
python src/aerobit.py
```

On the first run it downloads the OurAirports database (`airports.csv`, about 12 MB) into the project folder, and refreshes it every 30 days.

## Commands

```
python src/aerobit.py [radar | map | FLIGHT] [options]
```
Commands can be entered at any time from either the command line or the display. To enter a command from the display, press `Enter` first to open the input prompt. Both methods are functionally equivalent.

Give at most one mode, plus any options, in any order. Note attributes reset to defaults when the mode is switched. The default mode is radar. `help` will print the below list.

| Option | Default | Effect |
|---|---|---|
| `r=MILES` | 100mi radar, 10mi map, 1000mi tracking | Map radius. The flight search reaches at most 287 miles |
| `num_flights=num\|all` | 1 radar, all map | How many of the closest flights to show (0 shows none) |
| `airports=true\|false` | true | Radar and tracking: the route's airports. Map: every airline airport |
| `airfields=true\|false` | false | Map only: every other airport without airline service |
| `heli_only=true\|false` | false | Map: only helicopters. Radar: the selected flight plus the closest helicopters |
| `roads=true\|false` | false | Draw roads over the satellite map |
| `set_coords=LAT,LON` | from `.env` | Move the radar and map center until the program closes, and uniquely **persists** between modes. |
| `recenter set\|default` | | Radar and map: `set` (or no argument) goes back to the last `set_coords` center, undoing a clicked map center; `default` goes back to the `.env` center, undoing `set_coords` too |

Use the following shortcut commands from the display while it's running:

| Key | Effect |
|---|---|
| R / M | Switch to radar / map |
| T | Track the selected blue flight |
| Mouse | Hover over an aircraft to see its callsign; click on an aircraft to track it; click on any terrain in map mode to temporarily set a new center (reverts when switching modes, unlike the `set_coords` command) |
| F11 | Toggle full screen |
| Esc | Cancel typing, or quit |

## Settings

`defaults.yml` in the project folder holds the startup defaults. Commands override them as long as the mode remains unchanged. Edits to the file take effect on the next launch.

| Setting | Default | Effect |
|---|---|---|
| `radar_radius_miles` | 100 | Radar search radius (`r=` in radar). At most 287 |
| `map_radius_miles` | 10 | Map zoom, from the center to the top edge (`r=` in map) |
| `satellite_map` | true | Satellite imagery behind maps; false shows a plain grid |
| `show_airports` | true | `airports=`: the route's airports in radar and tracking, every airline airport in map |
| `map_show_airfields` | false | `airfields=`: airports without airline service, in map |
| `map_heli_only` | false | `heli_only=` in map: only helicopters |
| `show_roads` | false | `roads=`: roads over the satellite map |
| `start_fullscreen` | false | Open in full screen |
| `color_flagged_aircraft` | [255, 150, 0] | RGB color of the aircraft in `FLAGGED_AIRCRAFT` |

The program checks the file on startup and names any setting that's missing or has the wrong type.

Less common settings are constants near the top of `src/aerobit.py`:

| Setting | Default | Effect |
|---|---|---|
| `NUM_FLIGHTS` / `MAP_NUM_FLIGHTS` | 1 / all | `num_flights=` defaults for radar and map |
| `TRACK_NO_ROUTE_RADIUS_MILES` | 1000 | Tracking map radius when the route is unknown (`r=` in tracking) |
| `HELI_ONLY` | false | `heli_only=` in radar |
| `REFRESH_SECONDS` | 15 | How often new data is downloaded |
| `MAX_TRACES` | 4 | Flight paths downloaded per refresh. Higher finds routes faster but risks rate limits |
| `MAX_LOOKUPS` | 8 | New flights looked up in adsbdb per refresh |
| `TAKEOFF_AIRPORT_MILES` | 10 | How close the start of a flight's path must be to an airport to count as its origin |
| `ETA_BUFFER_MINUTES` | 15 | Added to each ETA for the approach and landing |
| `MAX_AIRPORT_LABELS` | 40 | Airport codes drawn per map; the rest are just dots |
| `CROWDED_AIRPORTS` | 60 | Past this many airports on a map, minor ones shrink to single LEDs and only large airports keep codes |
| `COLOR_*` | | The other display colors |

## Code layout

| File | Contents |
|---|---|
| `src/aerobit.py` | Entry point: settings, `.env` and `defaults.yml` loading, the LED font, commands and the window loop |
| `src/map.py` | Map mode, plus what every mode's map uses: geography, the live aircraft feed, flight paths and takeoff detection, airports, satellite tiles and map drawing |
| `src/radar.py` | Radar mode, plus route lookup and verification and the flight info screen that tracking shares |
| `src/track.py` | Tracking mode |

## Data sources

| Source | What it provides |
|---|---|
| [adsb.fi](https://adsb.fi/) | The main live feed: every aircraft within a radius (radar and map) and worldwide search by callsign (tracking), each with its position, altitude, speed, heading, vertical rate, squawk, tail number, aircraft type and military flag |
| [adsb.lol](https://adsb.lol/) | The backup live feed when adsb.fi doesn't answer; the only source of flight paths (each aircraft's recorded positions today, which show where and when it took off); and a route database that lists each leg of multi-stop flights |
| [adsbdb](https://www.adsbdb.com/) | A second route database (origin and destination for a callsign), airline names and two-letter codes (used to find logos), and flight numbers turned into callsigns (AA123 → AAL123) |
| [Aviasales](https://www.aviasales.com/) | Airline logos, by two-letter airline code |
| [OurAirports](https://ourairports.com/) | The airport list, downloaded once a month: codes, names, locations, size, whether each has airline service, and the town, state or region and country it serves |
| [Esri](https://www.esri.com/) | Satellite imagery behind every map and the optional road overlay |

All of these are free to use. Check their terms before using AeroBit for anything beyond personal use. Airline names and logos belong to their owners; AeroBit isn't affiliated with any airline or data provider.

## License

[MIT](LICENSE) © 2026 Luca Pishos
