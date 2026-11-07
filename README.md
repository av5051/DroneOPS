# DroneOps MVP

## Setup (Ubuntu 24.04)

```bash
cd droneops_mvp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # do this while you still have internet
python3 app.py
```

For offline laptops: `pip download -r requirements.txt -d wheels/` once online,
then `pip install --no-index --find-links wheels -r requirements.txt`.

## Use

1. Profiles > New profile (name, IP, port, user, optional SSH key). Connect.
2. Browse. Click a file to preview it (text, CSV, JSON, logs, images).
3. Download / double-click a file / right-click for more. Folders download recursively.
4. "Bookmark this folder" or "Bookmark selected" puts it in Quick access.
5. Select one or more data files (or a whole folder) > "Make KML". Folders are scanned for .txt/.csv/.log/.json.

Downloads go to `~/DroneOps/<profile>/<remote path>`. KMLs go to `~/DroneOps/<profile>/kml/`.
Config (profiles, bookmarks) lives in `~/.droneops/config.json`.

## Data formats it understands

| File | Preview | KML |
|---|---|---|
| `targets.txt` (det_id, lat, lon, conf, timestamp, image_stem, centre_dist_px) | table + stats | pins, green if conf >= 0.8 |
| `targets_clustered.txt` (person_id, lat, lon, `# person N:` notes) | table | yellow pins with the notes |
| `geo_gps` JSON (array, JSON-lines or single object) | rate, duration, bounds, rtt, first/last rows | track line + Start/End |
| `geo_calc` JSON (per image) | image position table | camera pin per image + path |
| any other `.txt` with two numbers per line | raw text | pins (uses the Coords dropdown) |

The two formats above with fixed columns ignore the Coords dropdown. Select several files at once and
they go into one KML with a folder per type. Raw detections are hidden by default when clustered
targets are present (tick them in Google Earth to see them). Other JSON files (e.g. per-image
detections) are shown pretty-printed and skipped for KML. Times in JSON logs are shown in UTC.

## Not in this MVP

Persistent task history, workflows, SHA-256 verification, CSV plotting, video, ArduPilot/ROS
analysis, remote delete/rename/upload, password storage.