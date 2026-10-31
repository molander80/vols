import io
import os
import re
import json
import atexit
import shutil
import threading
import zipfile
import gzip
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, render_template, send_file
import simplekml

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024  # 256 МБ лимит загрузки
BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATE_FILE = os.path.join(DATA_DIR, 'state.json')       # маршруты + папки
GPON_FILE  = os.path.join(DATA_DIR, 'gpon_state.json')  # geo + onus + имена файлов
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


@app.after_request
def _gzip_response(resp):
    """Сжимает JSON/HTML-ответы через gzip, если клиент это поддерживает.

    Значительно сокращает трафик для /gpon/state и /routes (большие JSON).
    Бинарные ответы (send_file: KMZ/ZIP) не сжимаются — у них свой mimetype.
    """
    if resp.direct_passthrough:
        return resp
    accept = request.headers.get('Accept-Encoding', '')
    if 'gzip' not in accept.lower():
        return resp
    ct = resp.content_type or ''
    if not (ct.startswith('application/json') or ct.startswith('text/html')):
        return resp
    body = resp.get_data()
    if len(body) < 512:  # не сжимаем мелочь
        return resp
    resp.set_data(gzip.compress(body))
    resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Content-Length'] = str(len(resp.get_data()))
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp


@app.after_request
def _security_headers(resp):
    """Добавляет базовые защитные заголовки ко всем ответам."""
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    return resp


# ============================ STATE (KMZ) ============================
ROUTES = {}
_next_id = 1
_state_lock = threading.RLock()
PERSIST = {'customFolders': set()}

# Ленивая запись state.json:
#   - _state_dirty — флаг «есть несохранённые изменения»
#   - _state_timer — отложенный таймер на запись через _STATE_DEBOUNCE_SEC
#   - _save_state_now()   — синхронная запись
#   - _save_state()       — пометить грязным + запустить таймер
_state_dirty = False
_state_timer = None
_STATE_DEBOUNCE_SEC = 0.8


def _save_state_now():
    """Синхронная атомарная запись state.json."""
    with _state_lock:
        payload = {
            'next_id': _next_id,
            'customFolders': sorted(PERSIST['customFolders']),
            'routes': list(ROUTES.values()),
        }
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)


def _save_state_flush():
    """Сбрасывает флаг и вызывает _save_state_now()."""
    global _state_dirty, _state_timer
    with _state_lock:
        if not _state_dirty:
            _state_timer = None
            return
        _state_dirty = False
    try:
        _save_state_now()
    finally:
        with _state_lock:
            _state_timer = None


def _save_state():
    """
    Ленивая запись: помечаем состояние грязным и, если таймер ещё не запущен,
    запускаем отложенную запись через _STATE_DEBOUNCE_SEC.
    """
    global _state_dirty, _state_timer
    with _state_lock:
        _state_dirty = True
        if _state_timer is not None:
            return
        _state_timer = threading.Timer(_STATE_DEBOUNCE_SEC, _save_state_flush)
        _state_timer.daemon = True
        _state_timer.start()


def _flush_state_on_exit():
    """atexit: гарантированно сбросить несохранённые изменения при выходе."""
    global _state_dirty
    with _state_lock:
        if not _state_dirty:
            return
        _state_dirty = False
    try:
        _save_state_now()
    except Exception as e:
        print('state flush on exit failed:', e)


atexit.register(_flush_state_on_exit)


def _load_state():
    """Восстанавливает только KMZ-часть из state.json."""
    global _next_id
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print('state load error:', e)
        return

    ROUTES.clear()
    for r in payload.get('routes', []):
        try:
            rid = int(r['id'])
        except (KeyError, TypeError, ValueError):
            continue
        ROUTES[rid] = {
            'id': rid,
            'name': r.get('name', ''),
            'branch': r.get('branch'),
            'folder': r.get('folder', ''),
            'visible': bool(r.get('visible', True)),
            'points': r.get('points', []),
        }
    PERSIST['customFolders'] = set(payload.get('customFolders', []))
    _next_id = int(payload.get('next_id', (max(ROUTES) + 1) if ROUTES else 1))


def _touch_routes():
    _save_state()


def _alloc_route_id():
    """Атомарно выдаёт следующий свободный id маршрута."""
    global _next_id
    with _state_lock:
        rid = _next_id
        _next_id += 1
        return rid


# ============================ STATE (GPON) ============================
# GPON-состояние живёт в памяти и персистится в data/gpon_state.json.
# Оно полностью независимо от state.json.
GPON = {'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': None}

_gpon_lock = threading.RLock()
_gpon_dirty = False
_gpon_timer = None
_GPON_DEBOUNCE_SEC = 0.8


def _save_gpon_now():
    """Синхронно пишет gpon_state.json (атомарно, через tmp + os.replace)."""
    with _gpon_lock:
        payload = {
            'geo': GPON['geo'],
            'onus': GPON['onus'],
            'geoFiles': GPON['geoFiles'],
            'vgFile': GPON['vgFile'],
        }
        tmp = GPON_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, GPON_FILE)


def _save_gpon_flush():
    """Сбрасывает флаг и вызывает _save_gpon_now()."""
    global _gpon_dirty, _gpon_timer
    with _gpon_lock:
        if not _gpon_dirty:
            _gpon_timer = None
            return
        _gpon_dirty = False
    try:
        _save_gpon_now()
    finally:
        with _gpon_lock:
            _gpon_timer = None


def _save_gpon():
    """Ленивая запись gpon_state.json с debounce."""
    global _gpon_dirty, _gpon_timer
    with _gpon_lock:
        _gpon_dirty = True
        if _gpon_timer is not None:
            return
        _gpon_timer = threading.Timer(_GPON_DEBOUNCE_SEC, _save_gpon_flush)
        _gpon_timer.daemon = True
        _gpon_timer.start()


# Отдельный, более длинный debounce для точечных правок geo-точек.
# Перетаскивание маркера шлёт запрос на каждый шаг, а gpon_state.json
# может весить мегабайты — перезаписывать его на каждый клик избыточно.
_gpon_point_timer = None
_GPON_POINT_DEBOUNCE_SEC = 3.0


def _save_gpon_point_flush():
    global _gpon_point_timer
    with _gpon_lock:
        _gpon_point_timer = None
        _gpon_dirty = False
    try:
        _save_gpon_now()
    except Exception as e:
        print('gpon point flush error:', e)


def _save_gpon_point():
    """Ленивая запись после точечной правки geo (замедленный debounce)."""
    global _gpon_dirty, _gpon_point_timer
    with _gpon_lock:
        _gpon_dirty = True
        if _gpon_point_timer is not None:
            return
        _gpon_point_timer = threading.Timer(_GPON_POINT_DEBOUNCE_SEC, _save_gpon_point_flush)
        _gpon_point_timer.daemon = True
        _gpon_point_timer.start()


def _flush_gpon_on_exit():
    """atexit: гарантированно сбросить несохранённые GPON-изменения."""
    global _gpon_dirty
    with _gpon_lock:
        if not _gpon_dirty:
            return
        _gpon_dirty = False
    try:
        _save_gpon_now()
    except Exception as e:
        print('gpon flush on exit failed:', e)


atexit.register(_flush_gpon_on_exit)


def _load_gpon():
    """Восстанавливает GPON из gpon_state.json (если файл есть)."""
    GPON['geo'] = {}
    GPON['onus'] = []
    GPON['geoFiles'] = []
    GPON['vgFile'] = None

    if not os.path.exists(GPON_FILE):
        return
    try:
        with open(GPON_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print('gpon state load error:', e)
        return

    GPON['geo'] = payload.get('geo') or {}
    GPON['onus'] = payload.get('onus') or []
    GPON['geoFiles'] = payload.get('geoFiles') or []
    GPON['vgFile'] = payload.get('vgFile')


# ============================ KML PARSER ============================
def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[1] if '}' in tag else tag


def _parse_kml_bytes(data: bytes):
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
                        lat = float(parts[1]); lon = float(parts[0])
                        pts.append([lat, lon])
                    except ValueError:
                        continue
            if pts:
                result.append((name, pts))
    return result


def parse_route_file(path, ext):
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


# ============================ ROUTES ============================
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
        tmp = os.path.join(UPLOAD_DIR, f'{_next_id}_{safe}')
        f.save(tmp)
        parsed = parse_route_file(tmp, ext)
        if not parsed:
            try:
                os.remove(tmp)
            except OSError:
                pass
            continue
        multi = len(parsed) > 1
        base = os.path.splitext(fname)[0]
        for name, pts in parsed:
            rid = _alloc_route_id()
            ROUTES[rid] = {
                'id': rid,
                'name': f'{base} — {name}' if multi else name,
                'branch': None,
                'folder': folder,
                'visible': True,
                'points': pts,
            }
            added += 1
    if added:
        _touch_routes()
    return jsonify({'added': added})


@app.route('/routes')
def routes_list():
    return jsonify(list(ROUTES.values()))


@app.route('/route/<int:rid>', methods=['PATCH'])
def patch_route(rid):
    if rid not in ROUTES:
        return jsonify({'error': 'not found'}), 404
    data = request.get_json(force=True) or {}
    changed = False
    for k in ('name', 'branch', 'folder', 'visible'):
        if k in data:
            ROUTES[rid][k] = data[k]
            changed = True
    if changed:
        _touch_routes()
    return jsonify(ROUTES[rid])


@app.route('/routes/bulk', methods=['PATCH'])
def patch_routes_bulk():
    """Массовое обновление маршрутов одним запросом."""
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get('ids', [])
    patch = data.get('patch', {})

    if not isinstance(ids, list) or not isinstance(patch, dict):
        return jsonify({'error': 'bad payload'}), 400

    allowed = {'name', 'branch', 'folder', 'visible'}
    patch = {k: v for k, v in patch.items() if k in allowed}
    if not patch:
        return jsonify({'error': 'no valid keys'}), 400

    applied = 0
    updated = []
    for rid in ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        r = ROUTES.get(rid)
        if not r:
            continue
        for k, v in patch.items():
            r[k] = v
        applied += 1
        updated.append(rid)

    if applied:
        _save_state()
    return jsonify({'ok': True, 'applied': applied, 'ids': updated})


@app.route('/routes/bulk', methods=['DELETE'])
def delete_routes_bulk():
    """Массовое удаление маршрутов."""
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
        if ROUTES.pop(rid, None) is not None:
            removed += 1
    if removed:
        _save_state()
    return jsonify({'ok': True, 'removed': removed})


@app.route('/route/<int:rid>', methods=['DELETE'])
def delete_route(rid):
    if ROUTES.pop(rid, None) is not None:
        _touch_routes()
    return jsonify({'ok': True})


@app.route('/merge', methods=['POST'])
def merge():
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
    rid = _alloc_route_id()
    ROUTES[rid] = {
        'id': rid, 'name': name, 'branch': name,
        'folder': '', 'visible': True, 'points': pts,
    }
    for old in ids:
        ROUTES.pop(old, None)
    _touch_routes()
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


# ============================ EXPORT TREE ============================
_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(s: str) -> str:
    """Превращает произвольную строку в безопасное имя файла/папки."""
    s = (s or '').strip()
    s = _SAFE_RE.sub('_', s)
    s = s.rstrip(' .')
    if not s:
        s = 'unnamed'
    if s.upper() in {'CON','PRN','AUX','NUL',
                     'COM1','COM2','COM3','COM4','COM5','COM6','COM7','COM8','COM9',
                     'LPT1','LPT2','LPT3','LPT4','LPT5','LPT6','LPT7','LPT8','LPT9'}:
        s = '_' + s
    return s


def _tree_route_relative_path(r: dict) -> str:
    """<folder1>/<folder2>/.../<branch или name>.kmz"""
    parts = []
    folder = (r.get('folder') or '').strip()
    if folder:
        parts.extend(_safe_name(p) for p in folder.split('/') if p.strip())
    base = r.get('branch') or r.get('name') or f'route_{r.get("id")}'
    parts.append(_safe_name(base) + '.kmz')
    return os.path.join(*parts)


def _write_route_kmz_to_file(r: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kml = simplekml.Kml()
    ls = kml.newlinestring(name=r.get('name') or '')
    ls.coords = [(p[1], p[0]) for p in r.get('points', [])]
    kml.savekmz(path)


@app.route('/export_tree', methods=['POST'])
def export_tree():
    """
    Зеркалит дерево маршрутов в uploads/tree/<folder...>/<branch|name>.kmz.
    Перед записью полностью очищает uploads/tree/.
    Возвращает ZIP со всей структурой.
    """
    data = request.get_json(force=True, silent=True) or {}
    only_visible = bool(data.get('only_visible'))

    tree_root = os.path.join(UPLOAD_DIR, 'tree')
    if os.path.exists(tree_root):
        try:
            shutil.rmtree(tree_root)
        except OSError as e:
            print('export_tree: rmtree failed:', e)
    os.makedirs(tree_root, exist_ok=True)

    written = 0
    skipped = 0
    used_paths = set()
    for r in ROUTES.values():
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
        for root, dirs, files in os.walk(tree_root):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, tree_root)
                z.write(full, rel)
    buf.seek(0)

    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name='routes_tree.zip',
    )


# ============================ FOLDERS ============================
@app.route('/folders', methods=['GET'])
def folders_get():
    return jsonify(sorted(PERSIST['customFolders']))


@app.route('/folders', methods=['POST'])
def folders_add():
    data = request.get_json(force=True) or {}
    path = (data.get('path') or '').strip().strip('/')
    if not path:
        return jsonify({'error': 'empty'}), 400
    PERSIST['customFolders'].add(path)
    _touch_routes()
    return jsonify({'ok': True, 'folders': sorted(PERSIST['customFolders'])})


@app.route('/folders', methods=['DELETE'])
def folders_delete():
    data = request.get_json(force=True) or {}
    path = (data.get('path') or '').strip().strip('/')
    if not path:
        return jsonify({'error': 'empty'}), 400
    PERSIST['customFolders'] = {
        f for f in PERSIST['customFolders']
        if f != path and not f.startswith(path + '/')
    }
    _touch_routes()
    return jsonify({'ok': True, 'folders': sorted(PERSIST['customFolders'])})


@app.route('/folders/rename', methods=['POST'])
def folders_rename():
    data = request.get_json(force=True) or {}
    old = (data.get('old') or '').strip().strip('/')
    new = (data.get('new') or '').strip().strip('/')
    if not old or not new:
        return jsonify({'error': 'empty'}), 400
    upd = set()
    for f in PERSIST['customFolders']:
        if f == old:
            upd.add(new)
        elif f.startswith(old + '/'):
            upd.add(new + f[len(old):])
        else:
            upd.add(f)
    PERSIST['customFolders'] = upd
    _touch_routes()
    return jsonify({'ok': True, 'folders': sorted(PERSIST['customFolders'])})


# ============================ STATE EXPORT / IMPORT (KMZ) ============================
@app.route('/state/export', methods=['GET'])
def state_export():
    if not os.path.exists(STATE_FILE):
        return jsonify({'routes': [], 'customFolders': [], 'next_id': 1})
    return send_file(STATE_FILE, mimetype='application/json',
                     as_attachment=True, download_name='state.json')


def _validate_state_payload(payload):
    """Проверяет структуру state.json. Возвращает None или строку ошибки."""
    if not isinstance(payload, dict):
        return 'state must be an object'
    if 'routes' in payload and not isinstance(payload['routes'], list):
        return 'routes must be a list'
    if 'customFolders' in payload and not isinstance(payload['customFolders'], list):
        return 'customFolders must be a list'
    if 'next_id' in payload and not isinstance(payload['next_id'], int):
        return 'next_id must be an int'
    return None


@app.route('/state/import', methods=['POST'])
def state_import():
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'no file'}), 400
    try:
        payload = json.loads(f.read().decode('utf-8'))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    err = _validate_state_payload(payload)
    if err:
        return jsonify({'error': err}), 400
    with _state_lock:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as out:
            json.dump(payload, out, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    _load_state()
    return jsonify({'ok': True,
                    'routes': len(ROUTES),
                    'folders': len(PERSIST['customFolders'])})


# ============================ GPON PARTIAL SAVE ============================
def _gpon_apply(changes: dict) -> str:
    """Применяет частичные изменения к GPON-состоянию с валидацией типов.

    Принимает словарь с любым подмножеством ключей {
        'geo': dict, 'onus': list, 'geoFiles': list, 'vgFile': str|None
    }. Возвращает строку ошибки или None при успехе.
    """
    if not isinstance(changes, dict):
        return 'payload must be an object'
    if 'geo' in changes:
        if not isinstance(changes['geo'], dict):
            return 'geo must be a dict'
        GPON['geo'] = changes['geo']
    if 'onus' in changes:
        if not isinstance(changes['onus'], list):
            return 'onus must be a list'
        GPON['onus'] = changes['onus']
    if 'geoFiles' in changes:
        if not isinstance(changes['geoFiles'], list):
            return 'geoFiles must be a list'
        GPON['geoFiles'] = list(changes['geoFiles'])
    if 'vgFile' in changes:
        vf = changes['vgFile']
        if vf is not None and not isinstance(vf, str):
            return 'vgFile must be a string or null'
        GPON['vgFile'] = vf
    return None


@app.route('/gpon/geo', methods=['POST'])
def gpon_geo_save():
    """
    Сохраняет весь geo-словарь и geoFiles.
    Тело: { "geo": {...}, "geoFiles": [...] }
    """
    data = request.get_json(force=True, silent=True) or {}
    if 'geo' not in data:
        return jsonify({'error': 'geo must be dict'}), 400
    changes = {'geo': data['geo']}
    if 'geoFiles' in data:
        changes['geoFiles'] = data.get('geoFiles') or []

    with _gpon_lock:
        err = _gpon_apply(changes)
    if err:
        return jsonify({'error': err}), 400
    _save_gpon()
    return jsonify({'ok': True, 'geo': len(GPON['geo'])})


@app.route('/gpon/onus', methods=['POST'])
def gpon_onus_save():
    """
    Сохраняет только onus и vgFile.
    Тело: { "onus": [...], "vgFile": "..." }
    """
    data = request.get_json(force=True, silent=True) or {}
    if 'onus' not in data:
        return jsonify({'error': 'onus must be list'}), 400
    changes = {'onus': data['onus']}
    if 'vgFile' in data:
        changes['vgFile'] = data.get('vgFile')

    with _gpon_lock:
        err = _gpon_apply(changes)
    if err:
        return jsonify({'error': err}), 400
    _save_gpon()
    return jsonify({'ok': True, 'onus': len(GPON['onus'])})


# ============================ GPON GEO POINT EDIT ============================
@app.route('/gpon/geo/point', methods=['POST'])
def gpon_geo_point_edit():
    """
    Обновляет одну точку geo: кладёт по нормализованному адресу {lat, lng, address}.
    Тело: { "addr": "...", "lat": 45.3, "lng": 38.1, "address": "исходный адрес" }
    """
    data = request.get_json(force=True, silent=True) or {}
    addr = (data.get('addr') or '').strip()
    try:
        lat = float(data.get('lat'))
        lng = float(data.get('lng'))
    except (TypeError, ValueError):
        return jsonify({'error': 'bad lat/lng'}), 400
    if not addr:
        return jsonify({'error': 'empty addr'}), 400

    with _gpon_lock:
        GPON['geo'][addr] = {
            'lat': lat,
            'lng': lng,
            'address': data.get('address') or addr,
        }
    _save_gpon_point()
    return jsonify({'ok': True, 'addr': addr, 'lat': lat, 'lng': lng})


# ============================ GPON STATE ============================
@app.route('/gpon/state', methods=['GET'])
def gpon_state_get():
    with _gpon_lock:
        return jsonify({
            'geo': GPON['geo'],
            'onus': GPON['onus'],
            'geoFiles': GPON['geoFiles'],
            'vgFile': GPON['vgFile'],
        })


@app.route('/gpon/state', methods=['DELETE'])
def gpon_state_delete():
    with _gpon_lock:
        GPON['geo'] = {}
        GPON['onus'] = []
        GPON['geoFiles'] = []
        GPON['vgFile'] = None
    _save_gpon()
    return jsonify({'ok': True})


@app.route('/gpon/state/export', methods=['GET'])
def gpon_state_export():
    if not os.path.exists(GPON_FILE):
        return jsonify({'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': None})
    return send_file(GPON_FILE, mimetype='application/json',
                     as_attachment=True, download_name='gpon_state.json')


def _validate_gpon_payload(payload):
    """Проверяет структуру gpon_state.json. Возвращает None или строку ошибки."""
    if not isinstance(payload, dict):
        return 'gpon state must be an object'
    if 'geo' in payload and not isinstance(payload['geo'], dict):
        return 'geo must be an object'
    if 'onus' in payload and not isinstance(payload['onus'], list):
        return 'onus must be a list'
    if 'geoFiles' in payload and not isinstance(payload['geoFiles'], list):
        return 'geoFiles must be a list'
    if 'vgFile' in payload and payload['vgFile'] is not None and not isinstance(payload['vgFile'], str):
        return 'vgFile must be a string or null'
    return None


@app.route('/gpon/state/import', methods=['POST'])
def gpon_state_import():
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'no file'}), 400
    try:
        payload = json.loads(f.read().decode('utf-8'))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    err = _validate_gpon_payload(payload)
    if err:
        return jsonify({'error': err}), 400

    with _gpon_lock:
        tmp = GPON_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as out:
            json.dump(payload, out, ensure_ascii=False)
        os.replace(tmp, GPON_FILE)

    _load_gpon()
    return jsonify({'ok': True,
                    'geo': len(GPON['geo']),
                    'onus': len(GPON['onus']),
                    'geoFiles': GPON['geoFiles'],
                    'vgFile': GPON['vgFile']})


# ============================ BOOT ============================
_load_state()
_load_gpon()

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)