"""Проверки офлайн-карт (MBTiles): список, отдача плиток, загрузка, удаление.

Запуск из корня проекта:
    python tests/test_maps_api.py
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app as server  # noqa: E402

TMP = tempfile.mkdtemp(prefix='vols-maps-')
server.MAPS_DIR = TMP

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24


def make_mbtiles(path, name='Тестовая карта'):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE metadata (name TEXT, value TEXT)')
    conn.execute(
        'CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, '
        'tile_row INTEGER, tile_data BLOB)'
    )
    conn.executemany(
        'INSERT INTO metadata VALUES (?, ?)',
        [('name', name), ('minzoom', '1'), ('maxzoom', '3'),
         ('scheme', 'tms'), ('bounds', '37.0,45.0,39.0,46.0')],
    )
    # MBTiles хранит строки в TMS: z=1, row=1 — это нижняя половина
    conn.execute('INSERT INTO tiles VALUES (1, 0, 1, ?)', (PNG,))
    conn.commit()
    conn.close()


SRC = os.path.join(TMP, 'test.mbtiles')
make_mbtiles(SRC)

client = server.app.test_client()
failures = []


def check(name, cond, detail=''):
    print(('  OK  ' if cond else ' FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


print('1) список карт')
r = client.get('/maps')
items = r.get_json()
check('200', r.status_code == 200, r.status_code)
check('одна карта', len(items) == 1, items)
info = items[0] if items else {}
check('имя файла', info.get('file') == 'test.mbtiles', info.get('file'))
check('зумы', info.get('minzoom') == 1 and info.get('maxzoom') == 3, info)
check('схема tms', info.get('scheme') == 'tms', info.get('scheme'))
check('границы', info.get('bounds') == [37.0, 45.0, 39.0, 46.0], info.get('bounds'))

print('2) плитки')
r = client.get('/maps/test.mbtiles/tile/1/0/1')
check('200', r.status_code == 200, r.status_code)
check('байты совпадают', r.data == PNG, len(r.data))
check('mime png', r.mimetype == 'image/png', r.mimetype)
r = client.get('/maps/test.mbtiles/tile/1/0/0')
check('отсутствующая плитка -> 204', r.status_code == 204, r.status_code)
r = client.get('/maps/nope.mbtiles/tile/1/0/1')
check('неизвестная карта -> 404', r.status_code == 404, r.status_code)
r = client.get('/maps/../app.py/tile/1/0/1')
check('выход из каталога -> 404', r.status_code == 404, r.status_code)

print('3) загрузка')
blob = open(SRC, 'rb').read()
r = client.post('/maps', data={'file': (io.BytesIO(blob), 'uploaded.mbtiles')},
                content_type='multipart/form-data')
check('201', r.status_code == 201, r.status_code)
check('файл на диске', os.path.exists(os.path.join(TMP, 'uploaded.mbtiles')))
check('в списке две', len(client.get('/maps').get_json()) == 2)

r = client.post('/maps', data={'file': (io.BytesIO(b'not a database'), 'bad.mbtiles')},
                content_type='multipart/form-data')
check('битый файл -> 400', r.status_code == 400, r.get_json())
check('битый файл не остался',
      not os.path.exists(os.path.join(TMP, 'bad.mbtiles')))

r = client.post('/maps', data={'file': (io.BytesIO(blob), 'map.zip')},
                content_type='multipart/form-data')
check('не .mbtiles -> 400', r.status_code == 400, r.get_json())

print('4) удаление')
r = client.delete('/maps/uploaded.mbtiles')
check('200', r.status_code == 200, r.status_code)
check('файл удалён', not os.path.exists(os.path.join(TMP, 'uploaded.mbtiles')))
r = client.delete('/maps/uploaded.mbtiles')
check('повторное удаление -> 404', r.status_code == 404, r.status_code)

print()
if failures:
    print('FAILED:', len(failures))
    for name in failures:
        print(' -', name)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)
shutil.rmtree(TMP, ignore_errors=True)
print('ALL CHECKS PASSED')
