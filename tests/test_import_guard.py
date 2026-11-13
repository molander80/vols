"""
Проверка защиты импорта состояния: /state/import и /gpon/state/import.

Запуск (pytest не нужен):
    python tests/test_import_guard.py

Реальные data/*.json не трогаются: пути подменяются на временный каталог.
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as server  # noqa: E402

TMP = tempfile.mkdtemp(prefix='vols-import-check-')
# временная папка с подменённым состоянием должна исчезать при любом исходе
atexit.register(shutil.rmtree, TMP, True)
server.STATE_FILE = os.path.join(TMP, 'state.json')
server.GPON_FILE = os.path.join(TMP, 'gpon_state.json')
server._routes.saver.path = server.STATE_FILE
server._gpon.saver.path = server.GPON_FILE
server._routes.routes.clear()
server._routes.custom_folders.clear()
server._gpon.reset()

client = server.app.test_client()
failures = []


def check(name, cond, detail=''):
    print(('  OK  ' if cond else ' FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


def post_json(path, payload):
    data = {'file': (io.BytesIO(json.dumps(payload).encode('utf-8')), 'state.json')}
    return client.post(path, data=data, content_type='multipart/form-data')


def write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


print('1) реальные файлы состояния проходят валидацию')
with open(os.path.join(ROOT, 'data', 'state.json'), encoding='utf-8') as f:
    check('routes state valid', server._validate_routes_state(json.load(f)) is None)
with open(os.path.join(ROOT, 'data', 'gpon_state.json'), encoding='utf-8') as f:
    check('gpon state valid', server._validate_gpon_state(json.load(f)) is None)

print('2) мусор вместо JSON не портит файл')
write(server.STATE_FILE, '{"routes": [], "next_id": 1, "customFolders": []}')
r = client.post('/state/import', data={'file': (io.BytesIO(b'not json'), 'state.json')},
                content_type='multipart/form-data')
check('bad json -> 400', r.status_code == 400, r.get_json())
check('file untouched', json.loads(read(server.STATE_FILE))['routes'] == [])

print('3) не-объект JSON отклоняется')
r = post_json('/state/import', [1, 2, 3])
check('list payload -> 400', r.status_code == 400, r.get_json())
check('file untouched', json.loads(read(server.STATE_FILE))['routes'] == [])

print('4) битые маршруты отклоняются')
r = post_json('/state/import', {'routes': [{'id': 'x'}]})
check('bad id -> 400', r.status_code == 400, r.get_json())
r = post_json('/state/import', {'routes': [{'id': 1, 'points': 'nope'}]})
check('bad points -> 400', r.status_code == 400, r.get_json())
r = post_json('/state/import', {'routes': [], 'customFolders': 'x'})
check('bad customFolders -> 400', r.status_code == 400, r.get_json())
check('file untouched', json.loads(read(server.STATE_FILE))['routes'] == [])

print('5) валидный импорт применяется и делает бэкап')
payload = {
    'next_id': 3,
    'customFolders': ['Голова 1'],
    'routes': [
        {'id': 1, 'name': 'a', 'branch': None, 'folder': '', 'visible': True,
         'points': [[45.0, 38.0], [45.1, 38.1]]},
        {'id': 2, 'name': 'b', 'branch': 'x/y', 'folder': 'Голова 1', 'visible': False,
         'points': []},
    ],
}
r = post_json('/state/import', payload)
body = r.get_json()
check('valid import -> 200', r.status_code == 200, body)
check('routes loaded', body.get('routes') == 2, body)
check('folders loaded', body.get('folders') == 1, body)
check('file on disk updated', len(json.loads(read(server.STATE_FILE))['routes']) == 2)
check('backup created', os.path.exists(server.STATE_FILE + '.bak'))
check('GET /routes returns 2', len(client.get('/routes').get_json()) == 2)

print('6) битый GPON-импорт не портит файл')
write(server.GPON_FILE, '{"geo": {}, "onus": [], "geoFiles": [], "vgFile": null}')
r = post_json('/gpon/state/import', {'geo': {'addr': {'lat': 'abc', 'lng': 1}}})
check('bad geo -> 400', r.status_code == 400, r.get_json())
r = post_json('/gpon/state/import', {'geo': [], 'onus': []})
check('geo not object -> 400', r.status_code == 400, r.get_json())
r = post_json('/gpon/state/import', {'geo': {}, 'onus': 'x'})
check('bad onus -> 400', r.status_code == 400, r.get_json())
check('gpon file untouched', json.loads(read(server.GPON_FILE))['onus'] == [])

print('7) валидный GPON-импорт применяется')
r = post_json('/gpon/state/import', {
    'geo': {'ул ленина, дом 1': {'lat': 45.2, 'lng': 38.3, 'address': 'ул Ленина, дом 1'}},
    'onus': [{'id': '1', 'login': 'a', 'address': 'ул Ленина, дом 1'}],
    'geoFiles': ['geo.xlsx'],
    'vgFile': 'vgroups.csv',
})
body = r.get_json()
check('gpon import -> 200', r.status_code == 200, body)
check('geo=1, onus=1', body.get('geo') == 1 and body.get('onus') == 1, body)
check('GET /gpon/state', client.get('/gpon/state').get_json()['geoFiles'] == ['geo.xlsx'])

print()
if failures:
    print('FAILED:', len(failures))
    for name in failures:
        print(' -', name)
    sys.exit(1)
print('ALL CHECKS PASSED')
