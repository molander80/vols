"""
KMZ + GPON Viewer — Flask-сервер.

Один файл state.json для маршрутов (KMZ) и один gpon_state.json
для geo + onus. Обе записи — с ленивым сохранением (debounce 0.8 с).
"""
import atexit
import io
import json
import re
import shutil
import threading
import xml.etree.ElementTree as ET
import zipfile

from flask import Flask, jsonify, render_template, request, send_file
import simplekml
import sys
import os

def resource_path(relative_path):
    """ Возвращает абсолютный путь к ресурсу, работает и для dev, и для PyInstaller """
    try:
        # PyInstaller создает временную папку и сохраняет путь в _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

# ============================ Paths / app ============================

BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATE_FILE = os.path.join(DATA_DIR, 'state.json')
GPON_FILE = os.path.join(DATA_DIR, 'gpon_state.json')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, template_folder=resource_path('templates'))


# ============================ Debounced saver ============================

class DebouncedSaver:
    """
    Ленивая запись JSON-файла:
      - mark_dirty()  ставит флаг и запускает таймер (если ещё не запущен);
      - flush()       сбрасывает флаг и пишет на диск;
      - save_now()    пишет принудительно;
      - _payload_fn() возвращает dict, который будет сериализован.

    Все методы потокобезопасны.
    """

    def __init__(self, path: str, payload_fn, debounce_sec: float = 0.8):
        self.path = path
        self.payload_fn = payload_fn
        self.debounce_sec = debounce_sec
        self._lock = threading.Lock()
        self._dirty = False
        self._timer = None

    def _write(self):
        tmp = self.path + '.tmp'
        payload = self.payload_fn()
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def save_now(self):
        with self._lock:
            self._dirty = False
        self._write()

    def flush(self):
        with self._lock:
            if not self._dirty:
                self._timer = None
                return
            self._dirty = False
        try:
            self._write()
        finally:
            with self._lock:
                self._timer = None

    def mark_dirty(self):
        with self._lock:
            self._dirty = True
            if self._timer is not None:
                return
            self._timer = threading.Timer(self.debounce_sec, self.flush)
            self._timer.daemon = True
            self._timer.start()


# ============================ Routes store ============================

class RoutesStore:
    """Маршруты KMZ + пользовательские папки."""

    def __init__(self):
        self._lock = threading.RLock()
        self.routes = {}
        self.custom_folders = set()
        self._next_id = 1
        self.saver = DebouncedSaver(STATE_FILE, self._payload)

    # --- payload ---
    def _payload(self):
        with self._lock:
            return {
                'next_id': self._next_id,
                'customFolders': sorted(self.custom_folders),
                'routes': list(self.routes.values()),
            }

    def load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print('state load error:', e)
            return

        with self._lock:
            self.routes.clear()
            for r in payload.get('routes', []):
                try:
                    rid = int(r['id'])
                except (KeyError, TypeError, ValueError):
                    continue
                self.routes[rid] = {
                    'id': rid,
                    'name': r.get('name', ''),
                    'branch': r.get('branch'),
                    'folder': r.get('folder', ''),
                    'visible': bool(r.get('visible', True)),
                    'points': r.get('points', []),
                }
            self.custom_folders = set(payload.get('customFolders', []))
            self._next_id = int(payload.get(
                'next_id',
                (max(self.routes) + 1) if self.routes else 1,
            ))

    # --- access ---
    def touch(self):
        self.saver.mark_dirty()

    def all(self):
        with self._lock:
            return list(self.routes.values())

    def get(self, rid):
        with self._lock:
            return self.routes.get(rid)

    def new_id(self):
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def put(self, route: dict):
        with self._lock:
            self.routes[route['id']] = route

    def patch(self, rid, data: dict, allowed=('name', 'branch', 'folder', 'visible')):
        with self._lock:
            r = self.routes.get(rid)
            if not r:
                return None
            for k in allowed:
                if k in data:
                    r[k] = data[k]
            return r

    def pop(self, rid):
        with self._lock:
            return self.routes.pop(rid, None)

    # --- folders ---
    def folders_list(self):
        with self._lock:
            return sorted(self.custom_folders)

    def folder_add(self, path):
        with self._lock:
            self.custom_folders.add(path)

    def folder_delete(self, path):
        with self._lock:
            self.custom_folders = {
                f for f in self.custom_folders
                if f != path and not f.startswith(path + '/')
            }

    def folder_rename(self, old, new):
        with self._lock:
            self.custom_folders = {
                (new if f == old else new + f[len(old):]) if f.startswith(old) else f
                for f in self.custom_folders
            }


_routes = RoutesStore()


# ============================ GPON store ============================

class GponStore:
    """geo + onus + имена файлов."""

    def __init__(self):
        self._lock = threading.RLock()
        self.geo = {}
        self.onus = []
        self.geo_files = []
        self.vg_file = None
        self.saver = DebouncedSaver(GPON_FILE, self._payload)

    def _payload(self):
        with self._lock:
            return {
                'geo': self.geo,
                'onus': self.onus,
                'geoFiles': self.geo_files,
                'vgFile': self.vg_file,
            }

    def load(self):
        self.reset()
        if not os.path.exists(GPON_FILE):
            return
        try:
            with open(GPON_FILE, 'r', encoding='utf-8') as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print('gpon state load error:', e)
            return
        with self._lock:
            self.geo = payload.get('geo') or {}
            self.onus = payload.get('onus') or []
            self.geo_files = payload.get('geoFiles') or []
            self.vg_file = payload.get('vgFile')

    def reset(self):
        with self._lock:
            self.geo = {}
            self.onus = []
            self.geo_files = []
            self.vg_file = None

    def touch(self):
        self.saver.mark_dirty()

    # --- updates ---
    def set_geo(self, geo: dict, files=None):
        with self._lock:
            self.geo = geo
            if files is not None:
                self.geo_files = list(files)

    def set_onus(self, onus: list, vg_file=None):
        with self._lock:
            self.onus = onus
            if vg_file is not None:
                self.vg_file = vg_file

    def set_geo_point(self, addr: str, lat: float, lng: float, display: str):
        with self._lock:
            self.geo[addr] = {'lat': lat, 'lng': lng, 'address': display}

    def snapshot(self):
        with self._lock:
            return {
                'geo': self.geo,
                'onus': self.onus,
                'geoFiles': self.geo_files,
                'vgFile': self.vg_file,
            }


_gpon = GponStore()


@atexit.register
def _flush_all():
    for s in (_routes.saver, _gpon.saver):
        try:
            s.flush()
        except Exception as e:
            print('flush failed:', e)


# ============================ KML parsing ============================

def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[1] if '}' in tag else tag


def _parse_kml_bytes(data: bytes):
    """Возвращает [(name, [[lat, lon], ...]), ...]."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print('KML parse error:', e)
        return []

    result = []
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
            coords_el = next(
                (c for c in ls.iter() if _strip_ns(c.tag) == 'coordinates' and c.text),
                None,
            )
            if coords_el is None:
                continue
            pts = []
            for token in coords_el.text.split():
                parts = token.split(',')
                if len(parts) < 2:
                    continue
                try:
                    lat, lon = float(parts[1]), float(parts[0])
                except ValueError:
                    continue
                pts.append([lat, lon])
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
    with open(path, 'rb') as f:
        return _parse_kml_bytes(f.read())


# ============================ KMZ export helpers ============================

_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WIN = {
    'CON', 'PRN', 'AUX', 'NUL',
    *[f'COM{i}' for i in range(1, 10)],
    *[f'LPT{i}' for i in range(1, 10)],
}


def _safe_name(s: str) -> str:
    s = (s or '').strip()
    s = _SAFE_RE.sub('_', s).rstrip(' .')
    if not s:
        s = 'unnamed'
    if s.upper() in _RESERVED_WIN:
        s = '_' + s
    return s


def _tree_route_relative_path(r: dict) -> str:
    parts = []
    folder = (r.get('folder') or '').strip()
    if folder:
        parts.extend(_safe_name(p) for p in folder.split('/') if p.strip())
    base = r.get('branch') or r.get('name') or f'route_{r.get("id")}'
    parts.append(_safe_name(base) + '.kmz')
    return os.path.join(*parts)


def _write_route_kmz_to_file(r: dict, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kml = simplekml.Kml()
    ls = kml.newlinestring(name=r.get('name') or '')
    ls.coords = [(p[1], p[0]) for p in r.get('points', [])]
    kml.savekmz(path)


# ============================ Routes API ============================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
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
        folder = '/'.join(parts[1:-1]) if len(parts) > 1 else ''
        safe = fname.replace('/', '_').replace('\\', '_')
        tmp = os.path.join(UPLOAD_DIR, f'{_routes.new_id()}_{safe}')
        f.save(tmp)

        parsed = parse_route_file(tmp, ext)
        multi = len(parsed) > 1
        base = os.path.splitext(fname)[0]

        for name, pts in parsed:
            rid = _routes.new_id()
            _routes.put({
                'id': rid,
                'name': f'{base} — {name}' if multi else name,
                'branch': None,
                'folder': folder,
                'visible': True,
                'points': pts,
            })
            added += 1

    if added:
        _routes.touch()
    return jsonify({'added': added})


@app.route('/routes')
def routes_list():
    return jsonify(_routes.all())


@app.route('/route/<int:rid>', methods=['PATCH'])
def patch_route(rid):
    r = _routes.patch(rid, request.get_json(force=True) or {})
    if r is None:
        return jsonify({'error': 'not found'}), 404
    _routes.touch()
    return jsonify(r)


@app.route('/routes/bulk', methods=['PATCH'])
def patch_routes_bulk():
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get('ids', [])
    patch = data.get('patch', {})

    if not isinstance(ids, list) or not isinstance(patch, dict):
        return jsonify({'error': 'bad payload'}), 400

    allowed = {'name', 'branch', 'folder', 'visible'}
    patch = {k: v for k, v in patch.items() if k in allowed}
    if not patch:
        return jsonify({'error': 'no valid keys'}), 400

    applied, updated = 0, []
    for rid in ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if _routes.patch(rid, patch) is None:
            continue
        applied += 1
        updated.append(rid)

    if applied:
        _routes.touch()
    return jsonify({'ok': True, 'applied': applied, 'ids': updated})


@app.route('/routes/bulk', methods=['DELETE'])
def delete_routes_bulk():
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get('ids', [])
    if not isinstance(ids, list):
        return jsonify({'error': 'bad payload'}), 400

    removed = 0
    for rid in ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if _routes.pop(rid) is not None:
            removed += 1

    if removed:
        _routes.touch()
    return jsonify({'ok': True, 'removed': removed})


@app.route('/route/<int:rid>', methods=['DELETE'])
def delete_route(rid):
    if _routes.pop(rid) is not None:
        _routes.touch()
    return jsonify({'ok': True})


@app.route('/merge', methods=['POST'])
def merge():
    data = request.get_json(force=True) or {}
    ids = data.get('ids', [])
    name = data.get('name', 'Объединённый маршрут')

    pts = []
    for rid in ids:
        r = _routes.get(rid)
        if r:
            pts.extend(r['points'])

    if not pts:
        return jsonify({'error': 'no points'}), 400

    rid = _routes.new_id()
    _routes.put({
        'id': rid, 'name': name, 'branch': name,
        'folder': '', 'visible': True, 'points': pts,
    })
    for old in ids:
        _routes.pop(old)

    _routes.touch()
    return jsonify(_routes.get(rid))


@app.route('/export', methods=['POST'])
def export():
    data = request.get_json(force=True) or {}
    ids = data.get('ids', [])
    group_by_branch = bool(data.get('group_by_branch'))

    kml = simplekml.Kml()
    folders = {}
    for rid in ids:
        r = _routes.get(rid)
        if not r:
            continue
        if group_by_branch and r.get('branch'):
            container = folders.setdefault(
                r['branch'], kml.newfolder(name=r['branch'])
            )
        else:
            container = kml
        ls = container.newlinestring(name=r['name'])
        ls.coords = [(p[1], p[0]) for p in r['points']]

    buf = io.BytesIO()
    kml.savekmz(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/vnd.google-earth.kmz',
        as_attachment=True,
        download_name='routes.kmz',
    )


@app.route('/export_tree', methods=['POST'])
def export_tree():
    data = request.get_json(force=True, silent=True) or {}
    only_visible = bool(data.get('only_visible'))

    tree_root = os.path.join(UPLOAD_DIR, 'tree')
    if os.path.exists(tree_root):
        try:
            shutil.rmtree(tree_root)
        except OSError as e:
            print('export_tree: rmtree failed:', e)
    os.makedirs(tree_root, exist_ok=True)

    used_paths = set()
    written = skipped = 0

    for r in _routes.all():
        if only_visible and not r.get('visible'):
            skipped += 1
            continue

        rel = _tree_route_relative_path(r)
        full = os.path.join(tree_root, rel)
        if full in used_paths:
            base, ext = os.path.splitext(full)
            i = 2
            while f'{base}__{i}{ext}' in used_paths:
                i += 1
            full = f'{base}__{i}{ext}'
        used_paths.add(full)

        try:
            _write_route_kmz_to_file(r, full)
            written += 1
        except OSError as e:
            print('export_tree: write failed for', r.get('id'), e)
            skipped += 1

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(tree_root):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, tree_root))
    buf.seek(0)

    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name='routes_tree.zip')


# ============================ Folders API ============================

@app.route('/folders', methods=['GET'])
def folders_get():
    return jsonify(_routes.folders_list())


@app.route('/folders', methods=['POST'])
def folders_add():
    data = request.get_json(force=True) or {}
    path = (data.get('path') or '').strip().strip('/')
    if not path:
        return jsonify({'error': 'empty'}), 400
    _routes.folder_add(path)
    _routes.touch()
    return jsonify({'ok': True, 'folders': _routes.folders_list()})


@app.route('/folders', methods=['DELETE'])
def folders_delete():
    data = request.get_json(force=True) or {}
    path = (data.get('path') or '').strip().strip('/')
    if not path:
        return jsonify({'error': 'empty'}), 400
    _routes.folder_delete(path)
    _routes.touch()
    return jsonify({'ok': True, 'folders': _routes.folders_list()})


@app.route('/folders/rename', methods=['POST'])
def folders_rename():
    data = request.get_json(force=True) or {}
    old = (data.get('old') or '').strip().strip('/')
    new = (data.get('new') or '').strip().strip('/')
    if not old or not new:
        return jsonify({'error': 'empty'}), 400
    _routes.folder_rename(old, new)
    _routes.touch()
    return jsonify({'ok': True, 'folders': _routes.folders_list()})


# ============================ KMZ state export/import ============================

@app.route('/state/export', methods=['GET'])
def state_export():
    if not os.path.exists(STATE_FILE):
        return jsonify({'routes': [], 'customFolders': [], 'next_id': 1})
    return send_file(STATE_FILE, mimetype='application/json',
                     as_attachment=True, download_name='state.json')


@app.route('/state/import', methods=['POST'])
def state_import():
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'no file'}), 400
    try:
        payload = json.loads(f.read().decode('utf-8'))
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as out:
        json.dump(payload, out, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

    _routes.load()
    return jsonify({
        'ok': True,
        'routes': len(_routes.all()),
        'folders': len(_routes.folders_list()),
    })


# ============================ GPON API ============================

@app.route('/gpon/geo', methods=['POST'])
def gpon_geo_save():
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data.get('geo'), dict):
        return jsonify({'error': 'geo must be dict'}), 400

    _gpon.set_geo(data['geo'], files=data.get('geoFiles'))
    _gpon.touch()
    return jsonify({'ok': True, 'geo': len(_gpon.geo)})


@app.route('/gpon/onus', methods=['POST'])
def gpon_onus_save():
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data.get('onus'), list):
        return jsonify({'error': 'onus must be list'}), 400

    _gpon.set_onus(data['onus'], vg_file=data.get('vgFile'))
    _gpon.touch()
    return jsonify({'ok': True, 'onus': len(_gpon.onus)})


@app.route('/gpon/geo/point', methods=['POST'])
def gpon_geo_point_edit():
    data = request.get_json(force=True, silent=True) or {}
    addr = (data.get('addr') or '').strip()
    if not addr:
        return jsonify({'error': 'empty addr'}), 400

    try:
        lat = float(data.get('lat'))
        lng = float(data.get('lng'))
    except (TypeError, ValueError):
        return jsonify({'error': 'bad lat/lng'}), 400

    _gpon.set_geo_point(addr, lat, lng, data.get('address') or addr)
    _gpon.touch()
    return jsonify({'ok': True, 'addr': addr, 'lat': lat, 'lng': lng})


@app.route('/gpon/state', methods=['GET'])
def gpon_state_get():
    return jsonify(_gpon.snapshot())


@app.route('/gpon/state', methods=['POST'])
def gpon_state_post():
    data = request.get_json(force=True, silent=True) or {}
    if isinstance(data.get('geo'), dict):
        _gpon.set_geo(data['geo'], files=data.get('geoFiles'))
    if isinstance(data.get('onus'), list):
        _gpon.set_onus(data['onus'], vg_file=data.get('vgFile'))
    _gpon.touch()
    return jsonify({'ok': True, 'geo': len(_gpon.geo), 'onus': len(_gpon.onus)})


@app.route('/gpon/state', methods=['DELETE'])
def gpon_state_delete():
    _gpon.reset()
    _gpon.touch()
    return jsonify({'ok': True})


@app.route('/gpon/state/export', methods=['GET'])
def gpon_state_export():
    if not os.path.exists(GPON_FILE):
        return jsonify({'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': None})
    return send_file(GPON_FILE, mimetype='application/json',
                     as_attachment=True, download_name='gpon_state.json')


@app.route('/gpon/state/import', methods=['POST'])
def gpon_state_import():
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'no file'}), 400
    try:
        payload = json.loads(f.read().decode('utf-8'))
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    tmp = GPON_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as out:
        json.dump(payload, out, ensure_ascii=False)
    os.replace(tmp, GPON_FILE)

    _gpon.load()
    return jsonify({
        'ok': True,
        'geo': len(_gpon.geo),
        'onus': len(_gpon.onus),
        'geoFiles': _gpon.geo_files,
        'vgFile': _gpon.vg_file,
    })


# ============================ Boot ============================

_routes.load()
_gpon.load()

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)