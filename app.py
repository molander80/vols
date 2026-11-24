"""
KMZ + GPON Viewer — Flask-сервер.

Один файл state.json для маршрутов (KMZ) и один gpon_state.json
для geo + onus. Обе записи — с ленивым сохранением (debounce 0.8 с).
"""
import atexit
import copy
import gzip
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

def base_dir():
    """
    Каталог, рядом с которым живут data/ и uploads/.

    В onefile-сборке PyInstaller __file__ указывает внутрь временной папки
    _MEIPASS, которая удаляется при выходе из программы, поэтому состояние
    нужно хранить рядом с самим .exe. В dev-режиме — рядом с app.py.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

# ============================ Paths / app ============================

BASE_DIR = base_dir()


def resource_path(relative_path):
    """ Возвращает абсолютный путь к ресурсу, работает и для dev, и для PyInstaller """
    # В сборке PyInstaller ресурсы лежат во временной папке _MEIPASS
    base_path = getattr(sys, '_MEIPASS', None) or BASE_DIR
    return os.path.join(base_path, relative_path)


UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATE_FILE = os.path.join(DATA_DIR, 'state.json')
GPON_FILE = os.path.join(DATA_DIR, 'gpon_state.json')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(
    __name__,
    template_folder=resource_path('templates'),
    static_folder=resource_path('static'),
)
# Лимит на размер загружаемого файла: без него /upload принимал что угодно
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024  # 256 МБ

# Кириллица в JSON без \uXXXX: /gpon/state отдаёт мегабайты адресов,
# экранирование раздувало ответ примерно в 2.2 раза
app.json.ensure_ascii = False


@app.before_request
def _reject_cross_site():
    """Отбивает межсайтовые POST/PATCH/DELETE на localhost (CSRF).

    Без этой проверки любая открытая вкладка могла отправить простой запрос
    (text/plain не требует preflight) и, например, удалить GPON-состояние.
    """
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None

    site = request.headers.get('Sec-Fetch-Site')
    if site and site not in ('same-origin', 'none'):
        return jsonify({'error': 'cross-site request blocked'}), 403

    origin = request.headers.get('Origin')
    if origin:
        host = request.host_url.rstrip('/')
        if origin.rstrip('/') != host:
            return jsonify({'error': 'cross-site request blocked'}), 403

    # тело, отправленное как text/plain или форма, — типичный обход preflight
    if request.content_length and request.mimetype != 'multipart/form-data' and not request.is_json:
        return jsonify({'error': 'content-type must be application/json'}), 415
    return None


@app.after_request
def _gzip_response(resp):
    """Сжимает JSON/HTML-ответы gzip, если клиент это поддерживает.

    Основная экономия — /gpon/state (несколько мегабайт JSON).
    """
    if resp.direct_passthrough:
        return resp
    if 'gzip' not in request.headers.get('Accept-Encoding', '').lower():
        return resp
    ct = resp.content_type or ''
    if not (ct.startswith('application/json') or ct.startswith('text/html')):
        return resp
    body = resp.get_data()
    if len(body) < 512:
        return resp
    resp.set_data(gzip.compress(body))
    resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Content-Length'] = str(len(resp.get_data()))
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp


@app.after_request
def _security_headers(resp):
    """Базовые защитные заголовки для всех ответов."""
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    return resp


@app.after_request
def _static_cache_headers(resp):
    """Кэш статики: вендорные библиотеки надолго, код приложения — нет."""
    path = request.path
    if not path.startswith('/static/'):
        return resp
    if path == '/static/app.js':
        resp.headers['Cache-Control'] = 'no-cache'
    else:
        resp.headers['Cache-Control'] = 'public, max-age=2592000'
    return resp


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
        self._write_lock = threading.Lock()
        self._dirty = False
        self._timer = None

    def _write(self):
        # таймер и save_now могут прийти одновременно — пишем строго по
        # очереди, иначе два потока делят один .tmp и файл получается битым
        with self._write_lock:
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

    def cancel(self):
        """Отменяет отложенную запись (нужно при импорте состояния)."""
        with self._lock:
            self._dirty = False
            if self._timer is not None:
                self._timer.cancel()
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

def _points_to_segments(points):
    """Полилиния -> цепочка отрезков."""
    return [[points[i], points[i + 1]] for i in range(len(points) - 1)]


def _route_segments(route):
    """Геометрия маршрута как список отрезков.

    Новый формат — route['segments'] (сеть отрезков); старый ('points' —
    одна полилиния) конвертируется в цепочку.
    """
    segs = route.get('segments')
    if isinstance(segs, list) and segs:
        return segs
    pts = route.get('points') or []
    if not isinstance(pts, list) or len(pts) < 2:
        return []
    return _points_to_segments(pts)


def _flatten_points(segments):
    """Уникальные вершины всех отрезков — для габаритов и совместимости."""
    out, seen = [], set()
    for seg in segments or []:
        for p in seg:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            key = (round(float(p[0]), 7), round(float(p[1]), 7))
            if key in seen:
                continue
            seen.add(key)
            out.append([float(p[0]), float(p[1])])
    return out


def _seg_key(p):
    return (round(float(p[0]), 7), round(float(p[1]), 7))


def _deg_distance(a, b):
    """Приблизительное расстояние в градусах — для сшивки концов."""
    dlat = float(a[0]) - float(b[0])
    dlng = (float(a[1]) - float(b[1])) * 0.7
    return (dlat * dlat + dlng * dlng) ** 0.5


def _walk_chain(segments):
    """Один связный путь из набора отрезков + оставшиеся отрезки."""
    adj = {}
    for i, seg in enumerate(segments):
        a, b = seg
        ka, kb = _seg_key(a), _seg_key(b)
        adj.setdefault(ka, []).append((i, kb, a, b))
        adj.setdefault(kb, []).append((i, ka, b, a))

    if not adj:
        return [], list(segments)

    start = None
    for k, lst in adj.items():
        if len(lst) == 1:
            start = k
            break
    if start is None:
        start = next(iter(adj))

    edge = adj[start][0]
    points = [[float(edge[2][0]), float(edge[2][1])]]
    used = set()
    cur = start
    while True:
        step = None
        for (i, other, from_pt, to_pt) in adj.get(cur, []):
            if i in used:
                continue
            step = (i, other, to_pt)
            break
        if step is None:
            break
        i, other, to_pt = step
        used.add(i)
        points.append([float(to_pt[0]), float(to_pt[1])])
        cur = other

    leftover = [segments[i] for i in range(len(segments)) if i not in used]
    return points, leftover


def _split_chains(segments):
    """Разбивает отрезки на связные цепочки точек."""
    chains = []
    remaining = list(segments or [])
    guard = 0
    while remaining and guard < 10000:
        guard += 1
        pts, rest = _walk_chain(remaining)
        if len(pts) < 2:
            break
        chains.append(pts)
        if len(rest) >= len(remaining):
            break
        remaining = rest
    return chains


def _stitch_routes(routes, tol=1e-6):
    """Собирает отрезки нескольких маршрутов в одну цепочку.

    Цепочки упорядочиваются по ближайшим концам (направление при
    необходимости разворачивается). Соединяются только реально совпадающие
    концы, чтобы не появлялись лишние перемычки.
    """
    chains = []
    for r in routes:
        chains.extend(_split_chains(r.get('segments') or []))
    if not chains:
        return []

    sequences = []
    remaining = sorted(chains, key=len, reverse=True)
    while remaining:
        seq = list(remaining.pop(0))
        changed = True
        while changed:
            changed = False
            best = None
            for idx, ch in enumerate(remaining):
                options = (
                    (_deg_distance(seq[-1], ch[0]), idx, 'end', False),
                    (_deg_distance(seq[-1], ch[-1]), idx, 'end', True),
                    (_deg_distance(seq[0], ch[0]), idx, 'start', True),
                    (_deg_distance(seq[0], ch[-1]), idx, 'start', False),
                )
                for d, i2, where, rev in options:
                    if best is None or d < best[0]:
                        best = (d, i2, where, rev)
            if best is not None and best[0] <= tol:
                _, idx, where, rev = best
                ch = remaining.pop(idx)
                if rev:
                    ch = list(reversed(ch))
                if where == 'end':
                    seq.extend(ch[1:] if _deg_distance(seq[-1], ch[0]) <= tol else ch)
                else:
                    seq = (ch[:-1] if _deg_distance(ch[-1], seq[0]) <= tol else ch) + seq
                changed = True
        sequences.append(seq)

    segments = []
    for seq in sequences:
        for i in range(len(seq) - 1):
            if _deg_distance(seq[i], seq[i + 1]) > 0:
                segments.append([list(seq[i]), list(seq[i + 1])])
    return segments

class RoutesStore:
    """Маршруты KMZ + пользовательские папки."""

    def __init__(self):
        self._lock = threading.RLock()
        self.routes = {}
        self.closures = {}
        self.custom_folders = set()
        self._next_id = 1
        self.saver = DebouncedSaver(STATE_FILE, self._payload)

    # --- payload ---
    def _payload(self):
        with self._lock:
            # глубокая копия: json.dump идёт уже без лока, а маршруты
            # может править параллельный запрос
            return {
                'next_id': self._next_id,
                'customFolders': sorted(self.custom_folders),
                'routes': copy.deepcopy(list(self.routes.values())),
                'closures': copy.deepcopy(list(self.closures.values())),
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
                    'segments': _route_segments(r),
                }
            self.closures = {}
            for c in payload.get('closures', []) or []:
                if not isinstance(c, dict):
                    continue
                try:
                    cid = int(c['id'])
                    lat = float(c['lat'])
                    lng = float(c['lng'])
                except (KeyError, TypeError, ValueError):
                    continue
                self.closures[cid] = {
                    'id': cid,
                    'name': (c.get('name') or 'Муфта').strip() or 'Муфта',
                    'lat': lat,
                    'lng': lng,
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

    def patch(self, rid, data: dict, allowed=('name', 'branch', 'folder', 'visible', 'segments')):
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

    # --- муфты (точки) ---
    def closures_list(self):
        with self._lock:
            return list(self.closures.values())

    def closure_get(self, cid):
        with self._lock:
            return self.closures.get(cid)

    def closure_add(self, name, lat, lng):
        with self._lock:
            cid = self._next_id
            self._next_id += 1
            closure = {'id': cid, 'name': name, 'lat': lat, 'lng': lng}
            self.closures[cid] = closure
            return closure

    def closure_patch(self, cid, data):
        with self._lock:
            c = self.closures.get(cid)
            if not c:
                return None
            for key in ('name', 'lat', 'lng'):
                if key in data:
                    c[key] = data[key]
            return c

    def closure_pop(self, cid):
        with self._lock:
            return self.closures.pop(cid, None)

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
                (new if f == old else new + f[len(old):])
                if f == old or f.startswith(old + '/') else f
                for f in self.custom_folders
            }

    def folder_move(self, old: str, new: str) -> int:
        """Переносит папку old вместе с вложенными в new одним действием.

        Возвращает число перемещённых маршрутов. new == '' — в корень.
        """
        with self._lock:
            moved = 0
            for r in self.routes.values():
                folder = r.get('folder') or ''
                if folder == old or folder.startswith(old + '/'):
                    tail = folder[len(old):].lstrip('/')
                    r['folder'] = f'{new}/{tail}' if new and tail else (new or tail)
                    moved += 1

            folders = set()
            for f in self.custom_folders:
                if f == old or f.startswith(old + '/'):
                    tail = f[len(old):].lstrip('/')
                    target = f'{new}/{tail}' if new and tail else (new or tail)
                    if target:
                        folders.add(target)
                else:
                    folders.add(f)
            self.custom_folders = folders
            return moved


_routes = RoutesStore()


# ============================ GPON store ============================

# Поля ONU, которые действительно нужно хранить. lat/lng и _statusKey
# выводятся на клиенте из geo и статуса, поэтому в файле это дубли:
# у 8713 ONU они дают сотни килобайт лишнего веса.
_ONU_FIELDS = ('id', 'login', 'address', 'status', 'branch')


def _slim_onu(o):
    if not isinstance(o, dict):
        return None
    return {k: o.get(k) for k in _ONU_FIELDS}


def _slim_onus(onus):
    return [x for x in (_slim_onu(o) for o in onus) if x is not None]


def _slim_geo(geo):
    """Оставляет в geo только координаты.

    Ключ и так является нормализованным адресом, а строка адреса для
    отображения берётся из ONU — хранить её копию в geo избыточно.
    """
    if not isinstance(geo, dict):
        return {}
    out = {}
    for key, value in geo.items():
        if not isinstance(value, dict):
            continue
        try:
            out[key] = {'lat': float(value['lat']), 'lng': float(value['lng'])}
        except (KeyError, TypeError, ValueError):
            continue
    return out


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
            self.geo = _slim_geo(payload.get('geo') or {})
            self.onus = _slim_onus(payload.get('onus') or [])
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
        clean = _slim_geo(geo)
        with self._lock:
            self.geo = clean
            if files is not None:
                self.geo_files = list(files)

    def set_onus(self, onus: list, vg_file=None):
        clean = _slim_onus(onus)
        with self._lock:
            self.onus = clean
            if vg_file is not None:
                self.vg_file = vg_file

    def set_geo_point(self, addr: str, lat: float, lng: float, display: str):
        # display оставлен в сигнатуре для совместимости, но в файл не пишется
        with self._lock:
            self.geo[addr] = {'lat': lat, 'lng': lng}

    def snapshot(self):
        # копия под локом: jsonify сериализует уже без блокировки,
        # иначе параллельная правка координаты ломает ответ
        with self._lock:
            return {
                'geo': copy.deepcopy(self.geo),
                'onus': copy.deepcopy(self.onus),
                'geoFiles': list(self.geo_files),
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
    segments = r.get('segments') or []
    name = r.get('name') or ''
    if len(segments) <= 1:
        ls = kml.newlinestring(name=name)
        ls.coords = [(p[1], p[0]) for p in (segments[0] if segments else [])]
    else:
        mg = kml.newmultigeometry(name=name)
        for seg in segments:
            ls = mg.newlinestring()
            ls.coords = [(p[1], p[0]) for p in seg]
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
        if not parsed:
            # файл не дал ни одного маршрута — не оставляем мусор в uploads/
            try:
                os.remove(tmp)
            except OSError:
                pass
            continue
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
                'segments': _points_to_segments(pts),
            })
            added += 1

    if added:
        _routes.touch()
    return jsonify({'added': added})


@app.route('/routes')
def routes_list():
    return jsonify(_routes.all())


POINT_LIMIT = 100_000


def _validate_points(points):
    """Проверяет геометрию маршрута. Возвращает (clean_points, error)."""
    if not isinstance(points, list):
        return None, 'points must be a list'
    if not 2 <= len(points) <= POINT_LIMIT:
        return None, f'points must contain 2..{POINT_LIMIT} items'

    clean = []
    for i, p in enumerate(points):
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            return None, f'points[{i}] must be [lat, lon]'
        try:
            lat, lng = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None, f'points[{i}] must contain numbers'
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            return None, f'points[{i}] out of range'
        clean.append([lat, lng])
    return clean, None


def _validate_segments(segments):
    """Проверяет геометрию-сеть: список отрезков [[lat, lon], [lat, lon]]."""
    if not isinstance(segments, list):
        return None, 'segments must be a list'
    if not 1 <= len(segments) <= POINT_LIMIT:
        return None, f'segments must contain 1..{POINT_LIMIT} items'

    clean = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, (list, tuple)) or len(seg) != 2:
            return None, f'segments[{i}] must be [[lat, lon], [lat, lon]]'
        pair = []
        for j, p in enumerate(seg):
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                return None, f'segments[{i}][{j}] must be [lat, lon]'
            try:
                lat, lng = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                return None, f'segments[{i}][{j}] must contain numbers'
            if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
                return None, f'segments[{i}][{j}] out of range'
            pair.append([lat, lng])
        clean.append(pair)
    return clean, None


@app.route('/routes', methods=['POST'])
def routes_create():
    """Создаёт маршрут, нарисованный на карте."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip() or 'Новый маршрут'
    folder = (data.get('folder') or '').strip().strip('/')
    branch = data.get('branch')

    if branch is not None and not isinstance(branch, str):
        return jsonify({'error': 'branch must be a string or null'}), 400

    error = None
    if 'segments' in data:
        segments, error = _validate_segments(data['segments'])
    else:
        points, error = _validate_points(data.get('points'))
        segments = _points_to_segments(points) if points else None
    if error:
        return jsonify({'error': error}), 400

    rid = _routes.new_id()
    route = {
        'id': rid,
        'name': name,
        'branch': branch,
        'folder': folder,
        'visible': True,
        'segments': segments,
    }
    _routes.put(route)
    _routes.touch()
    return jsonify(route), 201


@app.route('/route/<int:rid>', methods=['PATCH'])
def patch_route(rid):
    data = request.get_json() or {}
    if 'segments' in data:
        segments, error = _validate_segments(data['segments'])
        if error:
            return jsonify({'error': error}), 400
        data = dict(data, segments=segments)
    elif 'points' in data:
        # совместимость: полилиния превращается в цепочку отрезков
        points, error = _validate_points(data['points'])
        if error:
            return jsonify({'error': error}), 400
        data = dict(data, segments=_points_to_segments(points))
    r = _routes.patch(rid, data)
    if r is None:
        return jsonify({'error': 'not found'}), 404
    _routes.touch()
    return jsonify(r)


@app.route('/routes/bulk', methods=['PATCH'])
def patch_routes_bulk():
    data = request.get_json(silent=True) or {}
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


@app.route('/routes/rename', methods=['POST'])
def rename_routes():
    """Массовое переименование по шаблону: '#' заменяется на номер по порядку."""
    data = request.get_json() or {}
    ids = data.get('ids')
    template = (data.get('template') or '').strip()

    if not isinstance(ids, list):
        return jsonify({'error': 'ids must be a list'}), 400
    if not template:
        return jsonify({'error': 'template must not be empty'}), 400
    if len(ids) > 5000:
        return jsonify({'error': 'too many ids'}), 400

    renamed, seen, n = 0, set(), 0
    for rid in ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if rid in seen:
            continue
        seen.add(rid)
        n += 1
        name = template.replace('#', str(n))
        if _routes.patch(rid, {'name': name}) is None:
            n -= 1
            continue
        renamed += 1

    if renamed:
        _routes.touch()
    return jsonify({'ok': True, 'renamed': renamed})


@app.route('/routes/bulk', methods=['DELETE'])
def delete_routes_bulk():
    data = request.get_json(silent=True) or {}
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


def _validate_closure(data):
    """Проверяет точку муфты: имя, широта, долгота."""
    try:
        lat = float(data.get('lat'))
        lng = float(data.get('lng'))
    except (TypeError, ValueError):
        return None, 'lat/lng must be numbers'
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None, 'lat/lng out of range'
    name = (data.get('name') or '').strip() or 'Муфта'
    return (name, lat, lng), None


@app.route('/closures', methods=['GET'])
def closures_list_route():
    return jsonify(_routes.closures_list())


@app.route('/closures', methods=['POST'])
def closure_create():
    data = request.get_json() or {}
    parsed, error = _validate_closure(data)
    if error:
        return jsonify({'error': error}), 400
    name, lat, lng = parsed
    closure = _routes.closure_add(name, lat, lng)
    _routes.touch()
    return jsonify(closure), 201


@app.route('/closures/<int:cid>', methods=['PATCH'])
def closure_update(cid):
    current = _routes.closure_get(cid)
    if not current:
        return jsonify({'error': 'not found'}), 404

    data = request.get_json() or {}
    merged = {
        'name': data.get('name', current['name']),
        'lat': data.get('lat', current['lat']),
        'lng': data.get('lng', current['lng']),
    }
    parsed, error = _validate_closure(merged)
    if error:
        return jsonify({'error': error}), 400

    name, lat, lng = parsed
    closure = _routes.closure_patch(cid, {'name': name, 'lat': lat, 'lng': lng})
    _routes.touch()
    return jsonify(closure)


@app.route('/closures/<int:cid>', methods=['DELETE'])
def closure_delete(cid):
    if _routes.closure_pop(cid) is not None:
        _routes.touch()
    return jsonify({'ok': True})


@app.route('/merge', methods=['POST'])
def merge():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip() or 'Объединённый маршрут'
    raw_ids = data.get('ids')
    if not isinstance(raw_ids, list):
        return jsonify({'error': 'ids must be a list'}), 400

    routes, seen = [], set()
    for rid in raw_ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if rid in seen:
            continue
        seen.add(rid)
        r = _routes.get(rid)
        if r:
            routes.append(r)

    if len(routes) < 2:
        return jsonify({'error': 'need at least 2 routes'}), 400

    segments = _stitch_routes(routes)
    if not segments:
        return jsonify({'error': 'no geometry'}), 400

    # папку сохраняем, если все части лежали в одной
    folders = {(r.get('folder') or '') for r in routes}
    folder = next(iter(folders)) if len(folders) == 1 else ''
    visible = any(r.get('visible') for r in routes)

    rid = _routes.new_id()
    _routes.put({
        'id': rid, 'name': name, 'branch': name,
        'folder': folder, 'visible': visible, 'segments': segments,
    })
    for r in routes:
        _routes.pop(r['id'])

    _routes.touch()
    return jsonify(_routes.get(rid)), 201


@app.route('/export', methods=['POST'])
def export():
    data = request.get_json() or {}
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
        segments = r.get('segments') or []
        if len(segments) == 1:
            ls = container.newlinestring(name=r['name'])
            ls.coords = [(p[1], p[0]) for p in segments[0]]
        elif segments:
            mg = container.newmultigeometry(name=r['name'])
            for seg in segments:
                ls = mg.newlinestring()
                ls.coords = [(p[1], p[0]) for p in seg]

    closures = _routes.closures_list()
    if closures:
        cfol = kml.newfolder(name='Муфты')
        for c in closures:
            pt = cfol.newpoint(name=c['name'])
            pt.coords = [(c['lng'], c['lat'])]

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
    data = request.get_json(silent=True) or {}
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
    data = request.get_json() or {}
    path = (data.get('path') or '').strip().strip('/')
    if not path:
        return jsonify({'error': 'empty'}), 400
    _routes.folder_add(path)
    _routes.touch()
    return jsonify({'ok': True, 'folders': _routes.folders_list()})


@app.route('/folders', methods=['DELETE'])
def folders_delete():
    data = request.get_json() or {}
    path = (data.get('path') or '').strip().strip('/')
    if not path:
        return jsonify({'error': 'empty'}), 400
    _routes.folder_delete(path)
    _routes.touch()
    return jsonify({'ok': True, 'folders': _routes.folders_list()})


@app.route('/folders/rename', methods=['POST'])
def folders_rename():
    data = request.get_json() or {}
    old = (data.get('old') or '').strip().strip('/')
    new = (data.get('new') or '').strip().strip('/')
    if not old or not new:
        return jsonify({'error': 'empty'}), 400
    _routes.folder_rename(old, new)
    _routes.touch()
    return jsonify({'ok': True, 'folders': _routes.folders_list()})


@app.route('/folders/move', methods=['POST'])
def folders_move():
    """Переносит папку со всем содержимым одним запросом вместо N+1 PATCH."""
    data = request.get_json(silent=True) or {}
    old = (data.get('old') or '').strip().strip('/')
    new = (data.get('new') or '').strip().strip('/')

    if not old:
        return jsonify({'error': 'empty old'}), 400
    if new == old or new.startswith(old + '/'):
        return jsonify({'error': 'bad target'}), 400

    moved = _routes.folder_move(old, new)
    if moved:
        _routes.touch()
    return jsonify({'ok': True, 'routes': moved, 'folders': _routes.folders_list()})


# ============================ JSON state import helpers ============================

def _json_upload_payload(field: str = 'file'):
    """Читает JSON из multipart-загрузки. Возвращает (payload, ready_response)."""
    f = request.files.get(field)
    if not f:
        return None, (jsonify({'error': 'no file'}), 400)
    try:
        payload = json.loads(f.read().decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, (jsonify({'error': f'bad json: {e}'}), 400)
    if not isinstance(payload, dict):
        return None, (jsonify({'error': 'json must be an object'}), 400)
    return payload, None


def _atomic_write_json(path: str, payload: dict):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _replace_state_file(path: str, payload: dict, cancel_pending, reload_fn):
    """
    Заменяет файл состояния с бэкапом и откатом.

    Возвращает None при успехе или ответ (jsonify, код) при ошибке, чтобы
    на диске не остался нечитаемый файл.
    """
    backup = None
    if os.path.exists(path):
        backup = path + '.bak'
        try:
            shutil.copy2(path, backup)
        except OSError as e:
            print('import: backup failed:', e)
            backup = None

    # отложенная запись старого состояния не должна перетереть импорт
    cancel_pending()
    _atomic_write_json(path, payload)

    try:
        reload_fn()
    except Exception as e:
        print('import: reload failed:', e)
        if backup:
            try:
                os.replace(backup, path)
            except OSError as e2:
                print('import: rollback failed:', e2)
        try:
            reload_fn()
        except Exception:
            pass
        return jsonify({'error': f'import failed: {e}'}), 500
    return None


def _validate_routes_state(payload: dict):
    if not isinstance(payload, dict):
        return 'state must be an object'
    routes = payload.get('routes', [])
    if not isinstance(routes, list):
        return 'routes must be a list'
    for i, r in enumerate(routes):
        if not isinstance(r, dict):
            return f'routes[{i}] must be an object'
        try:
            int(r['id'])
        except (KeyError, TypeError, ValueError):
            return f'routes[{i}].id must be an integer'
        if 'segments' in r:
            _, seg_err = _validate_segments(r['segments'])
            if seg_err:
                return f'routes[{i}].{seg_err}'
        if 'points' in r and not isinstance(r['points'], list):
            return f'routes[{i}].points must be a list'
        for key in ('name', 'folder'):
            if key in r and not isinstance(r[key], str):
                return f'routes[{i}].{key} must be a string'
        if r.get('branch') is not None and not isinstance(r['branch'], str):
            return f'routes[{i}].branch must be a string or null'

    folders = payload.get('customFolders', [])
    if not isinstance(folders, list) or any(not isinstance(x, str) for x in folders):
        return 'customFolders must be a list of strings'

    closures = payload.get('closures', [])
    if not isinstance(closures, list):
        return 'closures must be a list'
    for i, c in enumerate(closures):
        if not isinstance(c, dict):
            return f'closures[{i}] must be an object'
        try:
            float(c['lat'])
            float(c['lng'])
        except (KeyError, TypeError, ValueError):
            return f'closures[{i}] must have numeric lat/lng'
    return None


def _validate_gpon_state(payload: dict):
    if not isinstance(payload, dict):
        return 'state must be an object'
    geo = payload.get('geo')
    if geo is not None:
        if not isinstance(geo, dict):
            return 'geo must be an object'
        for key, value in geo.items():
            if not isinstance(value, dict):
                return f'geo[{key!r}] must be an object'
            for axis in ('lat', 'lng'):
                try:
                    float(value[axis])
                except (KeyError, TypeError, ValueError):
                    return f'geo[{key!r}].{axis} must be a number'

    onus = payload.get('onus')
    if onus is not None and not isinstance(onus, list):
        return 'onus must be a list'

    files = payload.get('geoFiles')
    if files is not None and (
        not isinstance(files, list) or any(not isinstance(x, str) for x in files)
    ):
        return 'geoFiles must be a list of strings'

    vg = payload.get('vgFile')
    if vg is not None and not isinstance(vg, str):
        return 'vgFile must be a string'
    return None


# ============================ KMZ state export/import ============================

@app.route('/state/export', methods=['GET'])
def state_export():
    if not os.path.exists(STATE_FILE):
        return jsonify({'routes': [], 'customFolders': [], 'next_id': 1})
    return send_file(STATE_FILE, mimetype='application/json',
                     as_attachment=True, download_name='state.json')


@app.route('/state/import', methods=['POST'])
def state_import():
    payload, error = _json_upload_payload()
    if error:
        return error

    problem = _validate_routes_state(payload)
    if problem:
        return jsonify({'error': problem}), 400

    error = _replace_state_file(STATE_FILE, payload, _routes.saver.cancel, _routes.load)
    if error:
        return error

    return jsonify({
        'ok': True,
        'routes': len(_routes.all()),
        'folders': len(_routes.folders_list()),
    })


# ============================ GPON API ============================

@app.route('/gpon/geo', methods=['POST'])
def gpon_geo_save():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('geo'), dict):
        return jsonify({'error': 'geo must be dict'}), 400

    _gpon.set_geo(data['geo'], files=data.get('geoFiles'))
    _gpon.touch()
    return jsonify({'ok': True, 'geo': len(_gpon.geo)})


@app.route('/gpon/onus', methods=['POST'])
def gpon_onus_save():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('onus'), list):
        return jsonify({'error': 'onus must be list'}), 400

    _gpon.set_onus(data['onus'], vg_file=data.get('vgFile'))
    _gpon.touch()
    return jsonify({'ok': True, 'onus': len(_gpon.onus)})


@app.route('/gpon/geo/point', methods=['POST'])
def gpon_geo_point_edit():
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}
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
    payload, error = _json_upload_payload()
    if error:
        return error

    problem = _validate_gpon_state(payload)
    if problem:
        return jsonify({'error': problem}), 400

    error = _replace_state_file(GPON_FILE, payload, _gpon.saver.cancel, _gpon.load)
    if error:
        return error

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
    # В сборке PyInstaller отладчик и reloader только вредят: reloader
    # запускает второй процесс с другим _MEIPASS, а Werkzeug-отладчик
    # открывает интерактивное выполнение кода на локальном порту.
    debug = not getattr(sys, 'frozen', False) and os.environ.get('VOLS_DEBUG') == '1'
    app.run(debug=debug, host='127.0.0.1', port=5000)