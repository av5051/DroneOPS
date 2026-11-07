"""Format detection, previews and KML export for DroneOps data files. No dependencies.

Recognised formats
  targets_raw        det_id, lat, lon, conf, timestamp, image_stem, centre_dist_px
  targets_clustered  person_id, lat, lon         ('# person N: ...' comments become descriptions)
  geo_gps            JSON: array, JSON-lines, or single object with lat/lon/timestamp_epoch
  geo_calc           JSON: per-image position with gps_before / gps_after
  text               anything else with two numbers per line (uses the Coords order setting)
"""
import json
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape

NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
PERSON_RE = re.compile(r"^#\s*person\s+(\d+)\s*:\s*(.*)$", re.I)
MAX_TRACK_POINTS = 20000


# ---------------------------------------------------------------- helpers
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_int(s):
    return re.fullmatch(r"[-+]?\d+", str(s).strip()) is not None


def _valid(lat, lon):
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180


def _data_lines(text):
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield n, line


def _iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _hms(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    w = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w[i]) for i, c in enumerate(r))
    return "\n".join([line(headers), "  ".join("-" * x for x in w)] + [line(r) for r in rows])


# ------------------------------------------------------------------ JSON
def load_json_records(text):
    """Return a list of dict records from a JSON array, JSON-lines, single object,
    or a truncated array (preview reads only the first part of big files)."""
    t = text.strip()
    if not t:
        return []
    try:
        obj = json.loads(t)
    except ValueError:
        obj = None
    if obj is None:
        recs = []
        for line in t.splitlines():
            line = line.strip().rstrip(",")
            if line in ("", "[", "]"):
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass
        recs = [r for r in recs if isinstance(r, dict)]
        if recs:
            return recs
        if t.startswith("["):  # truncated pretty-printed array: cut back to a complete object
            cut, tries = t.rfind("}"), 0
            while cut != -1 and tries < 50:
                try:
                    r = json.loads(t[:cut + 1].rstrip().rstrip(",") + "]")
                    return [x for x in r if isinstance(x, dict)]
                except ValueError:
                    cut, tries = t.rfind("}", 0, cut), tries + 1
        return []
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("records", "points", "data", "frames", "items", "track"):
            if isinstance(obj.get(k), list):
                return [x for x in obj[k] if isinstance(x, dict)]
        return [obj]
    return []


def detect_kind(name, text):
    s = text.lstrip()
    if s[:1] in ("[", "{"):
        recs = load_json_records(text)
        r = recs[0] if recs else {}
        if "gps_before" in r or ("image" in r and "lat" in r and "lon" in r):
            return "geo_calc"
        if "lat" in r and "lon" in r:
            return "geo_gps"
        return "json"
    for _, line in _data_lines(text):
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 7 and _is_int(p[0]) and _valid(_f(p[1]), _f(p[2])) \
                and _f(p[3]) is not None and 0 <= _f(p[3]) <= 1:
            return "targets_raw"
        if len(p) == 3 and _is_int(p[0]) and _valid(_f(p[1]), _f(p[2])):
            return "targets_clustered"
        break
    return "text"


# ---------------------------------------------------------- text parsers
def _parse_raw(text):
    pts, sk = [], []
    for n, line in _data_lines(text):
        p = [x.strip() for x in line.split(",")]
        if len(p) < 7:
            sk.append((n, line, "expected 7 columns"))
            continue
        try:
            det, lat, lon, conf, dist = int(p[0]), float(p[1]), float(p[2]), float(p[3]), int(float(p[6]))
        except ValueError:
            sk.append((n, line, "bad number"))
            continue
        if not _valid(lat, lon):
            sk.append((n, line, "lat/lon out of range"))
            continue
        pts.append({"kind": "targets_raw", "name": f"#{det}  {conf:.2f}", "lat": lat, "lon": lon, "alt": 0.0,
                    "det_id": det, "conf": conf, "time": p[4], "image": p[5], "dist_px": dist})
    return pts, sk


def _parse_clustered(text):
    notes = {}
    for raw in text.splitlines():
        m = PERSON_RE.match(raw.strip())
        if m:
            notes[int(m.group(1))] = m.group(2).strip()
    pts, sk = [], []
    for n, line in _data_lines(text):
        p = [x.strip() for x in line.split(",")]
        if len(p) < 3 or not _is_int(p[0]):
            sk.append((n, line, "expected person_id, lat, lon"))
            continue
        lat, lon = _f(p[1]), _f(p[2])
        if not _valid(lat, lon):
            sk.append((n, line, "lat/lon out of range"))
            continue
        pid = int(p[0])
        pts.append({"kind": "targets_clustered", "name": f"Person {pid}", "lat": lat, "lon": lon, "alt": 0.0,
                    "person_id": pid, "info": notes.get(pid, "")})
    return pts, sk


def _parse_generic(text, order):
    pts, sk = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        nums = NUM.findall(line)
        if len(nums) < 2:
            sk.append((n, line, "fewer than two numbers"))
            continue
        label = re.split(r"[,:;=\s]", line, maxsplit=1)[0]
        numeric_label = NUM.fullmatch(label) is not None
        if not numeric_label and len(nums) >= 3 and re.search(r"\d", label):
            nums = nums[len(NUM.findall(label)):]  # label like "T2" contributes a stray number
        a, b = float(nums[0]), float(nums[1])
        alt = float(nums[2]) if len(nums) >= 3 else 0.0
        lat, lon = (a, b) if order == "lat,lon" else (b, a)
        if not _valid(lat, lon):
            sk.append((n, line, f"out of range (lat={lat}, lon={lon}) - check coordinate order"))
            continue
        keyword = label.lower() in ("lat", "lon", "lng", "latitude", "longitude")
        name = label if (not numeric_label and label and not keyword) else f"Target {len(pts) + 1}"
        pts.append({"kind": "generic", "name": name, "lat": lat, "lon": lon, "alt": alt})
    return pts, sk


def parse_targets(text, order="lat,lon"):
    """Return (points, skipped). Picks the right parser for the file's format."""
    kind = detect_kind("", text)
    if kind == "targets_raw":
        return _parse_raw(text)
    if kind == "targets_clustered":
        return _parse_clustered(text)
    return _parse_generic(text, order)


# ------------------------------------------------------------------- KML
STYLES = """
<Style id="hi"><IconStyle><color>ff00c800</color><scale>0.7</scale></IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>
<Style id="lo"><IconStyle><color>ff0080ff</color><scale>0.7</scale></IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>
<Style id="person"><IconStyle><color>ff00ffff</color><scale>1.2</scale></IconStyle></Style>
<Style id="frame"><IconStyle><color>ffffff00</color><scale>0.6</scale></IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>
<Style id="track"><LineStyle><color>ffff0000</color><width>3</width></LineStyle></Style>
<Style id="plain"><IconStyle><scale>1.0</scale></IconStyle></Style>"""


def _pm(name, lat, lon, alt=0.0, desc=None, style=None, when=None):
    x = ["<Placemark>", f"<name>{escape(str(name))}</name>"]
    if style:
        x.append(f"<styleUrl>#{style}</styleUrl>")
    if when:
        x.append(f"<TimeStamp><when>{when}</when></TimeStamp>")
    if desc:
        body = "<br>".join(f"<b>{escape(str(k))}</b>: {escape(str(v))}" for k, v in desc if v not in (None, ""))
        x.append("<description><![CDATA[" + body.replace("]]>", "]]&gt;") + "]]></description>")
    x.append(f"<Point><coordinates>{lon:.7f},{lat:.7f},{alt:.1f}</coordinates></Point>")
    x.append("</Placemark>")
    return "\n".join(x)


def _line(name, coords, style="track"):
    c = " ".join(f"{lon:.7f},{lat:.7f},{alt:.1f}" for lat, lon, alt in coords)
    return (f"<Placemark><name>{escape(name)}</name><styleUrl>#{style}</styleUrl>"
            f"<LineString><tessellate>1</tessellate><coordinates>{c}</coordinates></LineString></Placemark>")


def _folder(name, items, visible=True):
    return (f"<Folder><name>{escape(name)}</name><visibility>{1 if visible else 0}</visibility>\n"
            + "\n".join(items) + "\n</Folder>")


def _geo_records(texts_kind_pairs):
    out = []
    for text in texts_kind_pairs:
        for r in load_json_records(text):
            lat, lon = _f(r.get("lat")), _f(r.get("lon"))
            if _valid(lat, lon):
                out.append(r)
    return out


def build_kml(files, order="lat,lon", doc_name="DroneOps"):
    """files: list of (filename, text). Returns (kml, plotted_count, summary, notes)."""
    raw, clu, gen, gps_txt, calc_txt, notes = [], [], [], [], [], []
    for name, text in files:
        kind = detect_kind(name, text)
        if kind in ("targets_raw", "targets_clustered", "text"):
            pts, sk = parse_targets(text, order)
            if sk:
                notes.append(f"{name}: {len(sk)} lines skipped (first: line {sk[0][0]}, {sk[0][2]})")
            if not pts and kind == "text":
                notes.append(f"{name}: no coordinates found, ignored")
            {"targets_raw": raw, "targets_clustered": clu, "text": gen}[kind].extend(pts)
        elif kind == "geo_gps":
            gps_txt.append(text)
        elif kind == "geo_calc":
            calc_txt.append(text)
        else:
            notes.append(f"{name}: JSON without positions, ignored")

    gps = sorted(_geo_records(gps_txt), key=lambda r: _f(r.get("timestamp_epoch")) or 0)
    calc = sorted(_geo_records(calc_txt), key=lambda r: _f(r.get("timestamp_epoch")) or 0)

    folders, parts, plotted = [], [], 0
    if clu:
        items = [_pm(p["name"], p["lat"], p["lon"], desc=[("Details", p["info"])], style="person") for p in clu]
        folders.append(_folder(f"Clustered targets ({len(clu)})", items))
        parts.append(f"{len(clu)} clustered targets")
        plotted += len(clu)
    if raw:
        items = [_pm(p["name"], p["lat"], p["lon"], style="hi" if p["conf"] >= 0.8 else "lo",
                     desc=[("Detection id", p["det_id"]), ("Confidence", p["conf"]), ("Time", p["time"]),
                           ("Image", p["image"]), ("Px from centre", p["dist_px"])]) for p in raw]
        folders.append(_folder(f"Raw detections ({len(raw)})", items, visible=not clu))
        parts.append(f"{len(raw)} raw detections" + (" (hidden by default)" if clu else ""))
        plotted += len(raw)
    if gen:
        items = [_pm(p["name"], p["lat"], p["lon"], p["alt"], style="plain") for p in gen]
        if len(gen) > 1:
            items.append(_line("Path", [(p["lat"], p["lon"], p["alt"]) for p in gen]))
        folders.append(_folder(f"Targets ({len(gen)})", items))
        parts.append(f"{len(gen)} targets")
        plotted += len(gen)
    if gps:
        step = max(1, len(gps) // MAX_TRACK_POINTS)
        use = gps[::step]
        coords = [(float(r["lat"]), float(r["lon"]), _f(r.get("alt_m")) or 0.0) for r in use]
        first, last = gps[0], gps[-1]
        items = [_line("GPS track", coords),
                 _pm("Start", float(first["lat"]), float(first["lon"]), when=_iso(first.get("timestamp_epoch")),
                     style="plain", desc=[("Time (UTC)", _hms(first.get("timestamp_epoch")))]),
                 _pm("End", float(last["lat"]), float(last["lon"]), when=_iso(last.get("timestamp_epoch")),
                     style="plain", desc=[("Time (UTC)", _hms(last.get("timestamp_epoch")))])]
        folders.append(_folder(f"GPS track ({len(gps)} points)", items))
        parts.append(f"GPS track of {len(gps)} points")
        plotted += len(gps)
    if calc:
        items = []
        for r in calc:
            ts = r.get("timestamp_epoch")
            items.append(_pm(str(r.get("image", "frame")).rsplit(".", 1)[0], float(r["lat"]), float(r["lon"]),
                             _f(r.get("alt_m")) or 0.0, style="frame", when=_iso(ts),
                             desc=[("Image", r.get("image")), ("Time (UTC)", _hms(ts)), ("Method", r.get("method")),
                                   ("Interpolation weight", r.get("interpolation_weight")),
                                   ("Alt (m)", r.get("alt_m")), ("Roll", r.get("roll_deg")),
                                   ("Pitch", r.get("pitch_deg")), ("Yaw", r.get("yaw_deg")),
                                   ("Detections file", r.get("detections_file"))]))
        if len(calc) > 1:
            items.append(_line("Camera path", [(float(r["lat"]), float(r["lon"]), _f(r.get("alt_m")) or 0.0)
                                               for r in calc]))
        folders.append(_folder(f"Image positions ({len(calc)})", items))
        parts.append(f"{len(calc)} image positions")
        plotted += len(calc)

    kml = ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n'
           f"<name>{escape(doc_name)}</name>{STYLES}\n" + "\n".join(folders) + "\n</Document>\n</kml>")
    return kml, plotted, ", ".join(parts), notes


# --------------------------------------------------------------- previews
def preview_text(name, text, truncated=False):
    """Readable summary + table for known formats; None means 'show the raw text'."""
    kind = detect_kind(name, text)
    note = "\n\n(partial file: the figures above cover only the part loaded)" if truncated else ""
    if kind == "targets_raw":
        pts, sk = _parse_raw(text)
        if not pts:
            return None
        confs = [p["conf"] for p in pts]
        head = (f"RAW DETECTIONS: {len(pts)} rows, {len({p['image'] for p in pts})} images\n"
                f"confidence min/mean/max: {min(confs):.2f} / {sum(confs)/len(confs):.2f} / {max(confs):.2f}\n"
                f"lat {min(p['lat'] for p in pts):.6f}..{max(p['lat'] for p in pts):.6f}   "
                f"lon {min(p['lon'] for p in pts):.6f}..{max(p['lon'] for p in pts):.6f}"
                + (f"\n{len(sk)} lines could not be read" if sk else "") + "\n\n")
        rows = [(p["det_id"], f"{p['lat']:.7f}", f"{p['lon']:.7f}", f"{p['conf']:.3f}", p["time"], p["image"],
                 p["dist_px"]) for p in pts[:200]]
        more = f"\n... {len(pts) - 200} more rows" if len(pts) > 200 else ""
        return head + _table(["id", "lat", "lon", "conf", "time", "image", "px"], rows) + more + note
    if kind == "targets_clustered":
        pts, sk = _parse_clustered(text)
        if not pts:
            return None
        rows = [(p["person_id"], f"{p['lat']:.7f}", f"{p['lon']:.7f}", p["info"][:90]) for p in pts]
        return f"CLUSTERED TARGETS: {len(pts)} people\n\n" + _table(["id", "lat", "lon", "details"], rows) + note
    if kind == "geo_gps":
        recs = sorted(_geo_records([text]), key=lambda r: _f(r.get("timestamp_epoch")) or 0)
        if not recs:
            return None
        ts = [_f(r.get("timestamp_epoch")) for r in recs if _f(r.get("timestamp_epoch")) is not None]
        dur = (ts[-1] - ts[0]) if len(ts) > 1 else 0
        rate = f"{(len(ts) - 1) / dur:.1f} Hz" if dur > 0 else "n/a"
        alts = [_f(r.get("alt_m")) for r in recs if _f(r.get("alt_m")) is not None]
        rtts = [_f(r.get("rtt_ms")) for r in recs if _f(r.get("rtt_ms")) is not None]
        head = (f"GPS/IMU LOG: {len(recs)} records" + (f", {dur:.1f} s, ~{rate}" if ts else "") + "\n"
                + (f"from {_hms(ts[0])} to {_hms(ts[-1])} UTC\n" if ts else "")
                + f"lat {min(float(r['lat']) for r in recs):.6f}..{max(float(r['lat']) for r in recs):.6f}   "
                  f"lon {min(float(r['lon']) for r in recs):.6f}..{max(float(r['lon']) for r in recs):.6f}\n"
                + (f"alt {min(alts):.1f}..{max(alts):.1f} m\n" if alts else "")
                + (f"rtt mean/max: {sum(rtts)/len(rtts):.1f} / {max(rtts):.1f} ms\n" if rtts else "") + "\n")
        sample = recs if len(recs) <= 12 else recs[:6] + recs[-6:]
        rows = [(_hms(r.get("timestamp_epoch")), f"{float(r['lat']):.6f}", f"{float(r['lon']):.6f}",
                 r.get("alt_m", ""), r.get("roll_deg", ""), r.get("pitch_deg", ""), r.get("yaw_deg", ""),
                 r.get("rtt_ms", "")) for r in sample]
        gap = "\n   ... (first 6 and last 6 records shown)" if len(recs) > 12 else ""
        return head + _table(["time (UTC)", "lat", "lon", "alt", "roll", "pitch", "yaw", "rtt"], rows) + gap + note
    if kind == "geo_calc":
        recs = sorted(_geo_records([text]), key=lambda r: _f(r.get("timestamp_epoch")) or 0)
        if not recs:
            return None
        rows = [(r.get("image", ""), _hms(r.get("timestamp_epoch")), r.get("method", ""),
                 r.get("interpolation_weight", ""), f"{float(r['lat']):.7f}", f"{float(r['lon']):.7f}",
                 r.get("alt_m", ""), r.get("yaw_deg", "")) for r in recs[:200]]
        out = f"IMAGE POSITIONS: {len(recs)} frames\n\n" + _table(
            ["image", "time (UTC)", "method", "weight", "lat", "lon", "alt", "yaw"], rows)
        if len(recs) == 1 and isinstance(recs[0].get("gps_before"), dict):
            b, a = recs[0]["gps_before"], recs[0].get("gps_after") or {}
            out += "\n\ninterpolated between:\n" + _table(
                ["", "time (UTC)", "lat", "lon", "alt", "file"],
                [("before", _hms(b.get("timestamp_epoch")), b.get("lat"), b.get("lon"), b.get("alt_m"), b.get("file")),
                 ("after", _hms(a.get("timestamp_epoch")), a.get("lat"), a.get("lon"), a.get("alt_m"), a.get("file"))])
        return out + note
    if kind == "json":
        try:
            return json.dumps(json.loads(text), indent=2)[:200000]
        except ValueError:
            return None
    return None


if __name__ == "__main__":
    print(preview_text("t.txt", "12, 12.97, 77.59, 0.87, 2026-11-06 14:02:11.340, a, 30"))