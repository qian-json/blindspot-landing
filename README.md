# BlindSpot landing page

Landing page for BlindSpot, a Houston traffic app that tells you *why* a road is slow: a crash, a train, a flood, an event, or just rush hour.

The page is a single `index.html`. The background is a 3D map of Houston built from OpenStreetMap data, and it flies to each slowdown as you scroll.

## Run it

```
python3 -m http.server 8000
```

Then open http://localhost:8000.

## Rebuilding the map

```
python3 tools/fetch_osm.py osm     # downloads ~100 MB of OpenStreetMap data
python3 tools/build_map.py osm     # rewrites the map data inside index.html
```

Traffic levels and incidents on the map are a sample weekday evening, not live data.

## Credits

- Map data © OpenStreetMap contributors (ODbL)
- Live camera video: Louisiana DOTD (511LA), standing in for Houston cameras
- Stats: University of Houston Hobby School of Public Affairs; Houston TranStar
- Built with three.js and hls.js
