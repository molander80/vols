"""Проверки API создания маршрута (кнопка «Нарисовать»).

Запуск из корня проекта:
    python tests/test_routes_api.py

Реальный data/state.json не изменяется: путь подменяется на временный.
"""

import json
import os
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app as server  # noqa: E402

TMP = tempfile.mkdtemp(prefix='vols-routes-api-')
server.STATE_FILE = os.path.join(TMP, 'state.json')
server._routes.saver.path = server.STATE_FILE
server._routes.saver.cancel()
with server._routes._lock:
    server._routes.routes = {}
    server._routes.custom_folders = set()
    server._routes._next_id = 1

client = server.app.test_client()
failures = []


def check(name, cond, detail=''):
    print(('  OK  ' if cond else ' FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


print('1) создание маршрута')
r = client.post('/routes', json={'name': 'Нарисованный', 'points': [[45.0, 38.0], [45.1, 38.1]]})
body = r.get_json()
check('201', r.status_code == 201, r.status_code)
check('id выдан', body.get('id') == 1, body.get('id'))
check('имя сохранено', body.get('name') == 'Нарисованный', body.get('name'))
check('точки сохранены', body.get('points') == [[45.0, 38.0], [45.1, 38.1]], body.get('points'))
check('маршрут видим', body.get('visible') is True)
check('GET /routes содержит его', len(client.get('/routes').get_json()) == 1)

print('2) значения по умолчанию')
r = client.post('/routes', json={'points': [[45.0, 38.0], [45.1, 38.1]]})
body = r.get_json()
check('имя по умолчанию', body.get('name') == 'Новый маршрут', body.get('name'))
check('id увеличился', body.get('id') == 2, body.get('id'))
r = client.post('/routes', json={'name': 'В папке', 'folder': '/Голова 1/ветка/', 'points': [[45, 38], [45.1, 38.1]]})
body = r.get_json()
check('папка нормализована', body.get('folder') == 'Голова 1/ветка', body.get('folder'))

print('3) валидация точек')
bad = [
    ('нет points', {}),
    ('одна точка', {'points': [[45.0, 38.0]]}),
    ('points не список', {'points': 'nope'}),
    ('точка не пара', {'points': [[45.0], [45.1, 38.1]]}),
    ('не числа', {'points': [['a', 'b'], ['c', 'd']]}),
    ('широта вне диапазона', {'points': [[100.0, 38.0], [45.1, 38.1]]}),
    ('долгота вне диапазона', {'points': [[45.0, 200.0], [45.1, 38.1]]}),
    ('branch не строка', {'points': [[45, 38], [45.1, 38.1]], 'branch': 5}),
]
for name, payload in bad:
    r = client.post('/routes', json=payload)
    check(f'{name} -> 400', r.status_code == 400, r.get_json())

print('4) сохранение на диск')
server._routes.saver.flush()
with open(server.STATE_FILE, encoding='utf-8') as f:
    saved = json.load(f)
check('в файле 3 маршрута', len(saved.get('routes', [])) == 3, len(saved.get('routes', [])))
check('next_id на диске', saved.get('next_id') == 4, saved.get('next_id'))

print('5) координаты приводятся к float')
r = client.post('/routes', json={'name': 'Int', 'points': [[45, 38], [46, 39]]})
check('целые стали float', r.get_json()['points'] == [[45.0, 38.0], [46.0, 39.0]],
      r.get_json()['points'])

print('6) правка геометрии через PATCH /route/<id>')
new_points = [[45.5, 38.5], [45.6, 38.6], [45.7, 38.7]]
r = client.patch('/route/1', json={'points': new_points})
body = r.get_json()
check('200', r.status_code == 200, r.status_code)
check('точки заменены', body.get('points') == new_points, body.get('points'))
check('имя не изменилось', body.get('name') == 'Нарисованный', body.get('name'))
check('GET /routes отражает правку',
      client.get('/routes').get_json()[0]['points'] == new_points)

for name, payload in [
    ('одна точка', {'points': [[45.0, 38.0]]}),
    ('не числа', {'points': [['a', 'b'], ['c', 'd']]}),
    ('вне диапазона', {'points': [[45.0, 200.0], [45.1, 38.1]]}),
]:
    r = client.patch('/route/1', json=payload)
    check(f'{name} -> 400', r.status_code == 400, r.get_json())
check('после отказов точки целы',
      client.get('/routes').get_json()[0]['points'] == new_points)

r = client.patch('/route/1', json={'name': 'Переименован'})
check('переименование не сбрасывает точки',
      r.get_json()['points'] == new_points, r.get_json().get('points'))
r = client.patch('/route/999', json={'points': new_points})
check('несуществующий маршрут -> 404', r.status_code == 404, r.status_code)

print('7) CSRF и content-type')
r = client.post('/routes', data='{"name":"x","points":[[45,38],[45.1,38.1]]}',
                content_type='text/plain')
check('text/plain -> 415', r.status_code == 415, r.status_code)
r = client.post('/routes', json={'name': 'x', 'points': [[45, 38], [45.1, 38.1]]},
                headers={'Sec-Fetch-Site': 'cross-site'})
check('cross-site -> 403', r.status_code == 403, r.status_code)
r = client.post('/routes', json={'name': 'x', 'points': [[45, 38], [45.1, 38.1]]},
                headers={'Origin': 'http://evil.example'})
check('чужой Origin -> 403', r.status_code == 403, r.status_code)
r = client.post('/routes', json={'name': 'ok', 'points': [[45, 38], [45.1, 38.1]]},
                headers={'Origin': 'http://localhost'})
check('свой Origin -> 201', r.status_code == 201, r.status_code)

server._routes.saver.cancel()
print()
if failures:
    print('FAILED:', len(failures))
    for name in failures:
        print(' -', name)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)
shutil.rmtree(TMP, ignore_errors=True)
print('ALL CHECKS PASSED')
