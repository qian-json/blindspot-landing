"""Download the OpenStreetMap layers the landing page's 3D Houston map is built from.

    python3 tools/fetch_osm.py OUT_DIR

Writes major.json, minor.json, rail.json, water.json, parks.json and bldg.json (about 100 MB total)
into OUT_DIR. Then run tools/build_map.py OUT_DIR to rebuild the map data inside index.html.
Map data (c) OpenStreetMap contributors, ODbL.
"""
import sys, time, urllib.request, urllib.parse
from pathlib import Path

B = "29.52,-95.80,30.05,-95.00"      # greater Houston, a little past Beltway 8
IN = "29.66,-95.53,29.86,-95.22"     # Loop 610 plus a margin, for the street grid
Q = {
    "major": f'[out:json][timeout:240];way["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|secondary)$"]({B});out geom;',
    "minor": f'[out:json][timeout:300];way["highway"~"^(tertiary|residential|unclassified|living_street)$"]({IN});out geom qt;',
    "rail": f'[out:json][timeout:180];way["railway"="rail"]({B});out geom;',
    "water": f'[out:json][timeout:180];(way["waterway"~"^(river|canal|stream)$"]({B});way["natural"="water"]({B});relation["natural"="water"]({B});way["water"]({B}););out geom;',
    "parks": f'[out:json][timeout:180];(way["leisure"~"^(park|golf_course)$"]({B});relation["leisure"~"^(park|golf_course)$"]({B}););out geom;',
    "bldg": f'[out:json][timeout:240];(way["building"]["height"]({B});way["building"]["building:levels"]({B}););out geom;',
}

out = Path(sys.argv[1] if len(sys.argv) > 1 else "osm")
out.mkdir(parents=True, exist_ok=True)
for name, query in Q.items():
    for attempt in range(6):
        try:
            req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                                         data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": "BlindSpot-landing-build/1.0"})
            body = urllib.request.urlopen(req, timeout=400).read()
            if body.lstrip()[:1] == b"{":
                (out / f"{name}.json").write_bytes(body)
                print(f"{name}: {len(body):,} bytes")
                break
            print(f"{name}: server busy, retrying")
        except Exception as e:  # Overpass often answers 429/504 under load
            print(f"{name}: {e}, retrying")
        time.sleep(20 * (attempt + 1))
