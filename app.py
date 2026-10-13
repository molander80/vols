import io
import os
import zipfile
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, render_template, send_file
import simplekml

app = Flask(__name__)
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

ROUTES = {}
_next_id = 1


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[1] if '}' in tag else tag


def _parse_kml_bytes(data: bytes):
    """Return list of (name, [(lat, lon), ...]) for each LineString found."""
    result = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print('KML parse error:', e)
        return result

    for pm in root.iter():
        if _strip_ns(pm.tag) != 'Placemark':
            continue
        name = 'route'
        for child in pm:
            if _strip_ns(child.tag) == 'name' and child.text:
                name = child.text.strip()
                break
        for ls in pm.iter():
            if _strip_ns(ls.tag) != 'LineString':
                continue
            coords_el = None
            for child in ls.iter():
                if _strip_ns(child.tag) == 'coordinates':
                    coords_el = child
                    break
            if coords_el is None or not coords_el.text:
                continue
            pts = []
            for token in coords_el.text.split():
                parts = token.split(',')
                if len(parts) >= 2:
                    try:
                        lon = float(parts[0]); lat = float(parts[1])
                        pts.append([lat, lon])
                    except ValueError:
                        continue
            if pts:
                result.append((name, pts))
    return result


def parse_route_file(path: str, ext: str):
    if ext == '.kmz':
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.lower().endswith('.kml'):
                        return _parse_kml_bytes(z.read(n))
        except zipfile.BadZipFile:
            return []
        return []
    else:
        with open(path, 'rb') as f:
            return _parse_kml_bytes(f.read())


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    global _next_id
    files = request.files.getlist('files[]')
    paths = request.form.getlist('paths[]')
    added = 0
    for i, f in enumerate(files):
        fname = f.filename or f'file_{i}'
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ('.kmz', '.kml'):
            continue

        rel = paths[i] if i < len(paths) else fname
        parts = [p for p in rel.split('/') if p]
        folder_parts = parts[1:-1] if len(parts) > 1 else []
        folder = '/'.join(folder_parts)

        safe = fname.replace('/', '_').replace('\\', '_')
        tmp = os.path.join(UPLOAD_DIR, f'{_next_id}_{safe}')
        f.save(tmp)

        parsed = parse_route_file(tmp, ext)
        multi = len(parsed) > 1
        base_name = os.path.splitext(fname)[0]
        for name, pts in parsed:
            rid = _next_id
            _next_id += 1
            rname = f'{base_name} — {name}' if multi else name
            ROUTES[rid] = {
                'id': rid,
                'name': rname,
                'branch': None,
                'folder': folder,
                'visible': True,
                'points': pts,
            }
            added += 1
    return jsonify({'added': added})


@app.route('/routes')
def routes():
    return jsonify(list(ROUTES.values()))


@app.route('/route/<int:rid>', methods=['PATCH'])
def patch_route(rid):
    if rid not in ROUTES:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(force=True) or {}
    for k in ('name', 'branch', 'folder', 'visible'):
        if k in data:
            ROUTES[rid][k] = data[k]
    return jsonify(ROUTES[rid])


@app.route('/route/<int:rid>', methods=['DELETE'])
def delete_route(rid):
    ROUTES.pop(rid, None)
    return jsonify({'ok': True})


@app.route('/merge', methods=['POST'])
def merge():
    global _next_id
    data = request.get_json(force=True) or {}
    ids = data.get('ids', [])
    name = data.get('name', 'Объединённый маршрут')
    pts = []
    for rid in ids:
        r = ROUTES.get(rid)
        if r:
            pts.extend(r['points'])
    if not pts:
        return jsonify({'error': 'no points'}), 400
    rid = _next_id; _next_id += 1
    ROUTES[rid] = {
        'id': rid, 'name': name, 'branch': name,
        'folder': '', 'visible': True, 'points': pts,
    }
    for old in ids:
        ROUTES.pop(old, None)
    return jsonify(ROUTES[rid])


@app.route('/export', methods=['POST'])
def export():
    data = request.get_json(force=True) or {}
    ids = data.get('ids', [])
    group_by_branch = bool(data.get('group_by_branch'))
    kml = simplekml.Kml()
    folders = {}
    for rid in ids:
        r = ROUTES.get(rid)
        if not r:
            continue
        if group_by_branch and r.get('branch'):
            if r['branch'] not in folders:
                folders[r['branch']] = kml.newfolder(name=r['branch'])
            container = folders[r['branch']]
        else:
            container = kml
        ls = container.newlinestring(name=r['name'])
        ls.coords = [(p[1], p[0]) for p in r['points']]
    buf = io.BytesIO()
    kml.savekmz(buf)
    buf.seek(0)
    return send_file(buf, mimetype='application/vnd.google-earth.kmz',
                     as_attachment=True, download_name='routes.kmz')


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)