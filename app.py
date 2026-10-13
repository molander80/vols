import os
import io
import zipfile
import xml.etree.ElementTree as ET
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
import simplekml

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024   # 500 MB суммарно

# Хранилище маршрутов в памяти.
# { id: {"id", "name", "points": [[lat,lon],...], "filename", "branch", "visible"} }
ROUTES = {}
_next_id = [1]

KML_NS = 'http://www.opengis.net/kml/2.2'


# ---------------------------------------------------------------------------
# Разбор KMZ
# ---------------------------------------------------------------------------
def parse_kmz(path):
    """
    Возвращает список маршрутов из KMZ-файла.
    Формат: [(name, [[lat, lon], ...]), ...]
    """
    with zipfile.ZipFile(path, 'r') as z:
        kml_name = next(
            (n for n in z.namelist() if n.lower().endswith('.kml')),
            None
        )
        if not kml_name:
            return []
        data = z.read(kml_name)

    root = ET.fromstring(data)
    result = []

    for pm in root.iter(f'{{{KML_NS}}}Placemark'):
        name_el = pm.find(f'{{{KML_NS}}}name')
        name = (name_el.text if name_el is not None and name_el.text
                else 'Без имени')

        for ls in pm.iter(f'{{{KML_NS}}}LineString'):
            coords_el = ls.find(f'{{{KML_NS}}}coordinates')
            if coords_el is None or not coords_el.text:
                continue

            pts = []
            for token in coords_el.text.strip().split():
                parts = token.split(',')
                if len(parts) >= 2:
                    try:
                        lon = float(parts[0])
                        lat = float(parts[1])
                        pts.append([lat, lon])
                    except ValueError:
                        continue
            if len(pts) >= 2:
                result.append((name, pts))

    return result


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    files = request.files.getlist('files')
    if not files:
        return jsonify({'added': [], 'total': len(ROUTES)})

    added = []
    for f in files:
        if not f or not f.filename:
            continue
        if not f.filename.lower().endswith(('.kmz', '.kml')):
            continue

        fname = secure_filename(f.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        f.save(save_path)

        try:
            routes = parse_kmz(save_path)
        except Exception as e:
            app.logger.warning(f'Ошибка чтения {fname}: {e}')
            continue

        if not routes:
            app.logger.info(f'В файле {fname} не найдено ни одного LineString')
            continue

        base_name = os.path.splitext(fname)[0]

        for i, (name, pts) in enumerate(routes):
            rid = _next_id[0]
            _next_id[0] += 1

            # Если в файле один маршрут — берём имя как есть.
            # Если несколько — добавляем префикс файла, чтобы не путались.
            display_name = name if len(routes) == 1 else f'{base_name} — {name}'

            ROUTES[rid] = {
                'id': rid,
                'name': display_name,
                'points': pts,
                'filename': fname,
                'branch': None,
                'visible': True,
            }
            added.append(rid)

    return jsonify({'added': added, 'total': len(ROUTES)})


@app.route('/routes')
def get_routes():
    return jsonify(list(ROUTES.values()))


@app.route('/route/<int:rid>', methods=['PATCH'])
def update_route(rid):
    if rid not in ROUTES:
        return jsonify({'error': 'not found'}), 404

    data = request.get_json(silent=True) or {}

    if 'name' in data:
        ROUTES[rid]['name'] = str(data['name'])

    if 'branch' in data:
        b = data['branch']
        ROUTES[rid]['branch'] = (str(b).strip() or None) if b is not None else None

    if 'visible' in data:
        ROUTES[rid]['visible'] = bool(data['visible'])

    return jsonify(ROUTES[rid])


@app.route('/route/<int:rid>', methods=['DELETE'])
def delete_route(rid):
    ROUTES.pop(rid, None)
    return jsonify({'ok': True})


@app.route('/merge', methods=['POST'])
def merge_routes():
    """
    Объединяет несколько маршрутов в один общий — новую ветвь.
    Точки просто склеиваются в порядке ids.
    """
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    new_name = (data.get('name') or 'Ветвь').strip()

    if len(ids) < 2:
        return jsonify({'error': 'Нужно минимум 2 маршрута'}), 400

    merged_points = []
    for rid in ids:
        r = ROUTES.get(int(rid))
        if r:
            merged_points.extend(r['points'])

    if not merged_points:
        return jsonify({'error': 'Нет точек'}), 400

    rid = _next_id[0]
    _next_id[0] += 1

    ROUTES[rid] = {
        'id': rid,
        'name': new_name,
        'points': merged_points,
        'filename': 'merged',
        'branch': new_name,
        'visible': True,
        'merged_from': list(ids),
    }
    return jsonify(ROUTES[rid])


@app.route('/export', methods=['POST'])
def export_kmz():
    """
    Экспортирует выбранные маршруты в один KMZ.
    Если group_by_branch=true — раскладывает по папкам KML по полю branch.
    """
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    group_by_branch = data.get('group_by_branch', True)

    kml = simplekml.Kml()

    if group_by_branch:
        folders = {}
        for rid in ids:
            r = ROUTES.get(int(rid))
            if not r:
                continue
            bname = r.get('branch') or 'Без ветви'
            if bname not in folders:
                folders[bname] = kml.newfolder(name=bname)
            line = folders[bname].newlinestring(name=r['name'])
            line.coords = [(lon, lat) for lat, lon in r['points']]
    else:
        for rid in ids:
            r = ROUTES.get(int(rid))
            if not r:
                continue
            line = kml.newlinestring(name=r['name'])
            line.coords = [(lon, lat) for lat, lon in r['points']]

    buf = io.BytesIO()
    kml.savekmz(buf)
    buf.seek(0)

    return send_file(
        buf,
        mimetype='application/vnd.google-earth.kmz',
        as_attachment=True,
        download_name='routes_export.kmz'
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    # debug=False — важно при 40 файлах, иначе Werkzeug сильно тормозит
    app.run(debug=False, host='127.0.0.1', port=5000)