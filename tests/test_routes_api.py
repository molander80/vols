"""Проверки API маршрутов: сеть отрезков (segments) и совместимость с points.

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

SEG1 = [[[45.0, 38.0], [45.1, 38.1]]]
SEG2 = [[[45.0, 38.0], [45.1, 38.1]], [[45.1, 38.1], [45.2, 38.2]]]


def check(name, cond, detail=''):
    print(('  OK  ' if cond else ' FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


print('1) создание маршрута-сети')
r = client.post('/routes', json={'name': 'Сеть', 'segments': SEG2})
body = r.get_json()
check('201', r.status_code == 201, r.status_code)
check('id выдан', body.get('id') == 1, body.get('id'))
check('имя сохранено', body.get('name') == 'Сеть', body.get('name'))
check('отрезки сохранены', body.get('segments') == SEG2, body.get('segments'))
check('в ответе нет points', 'points' not in body, sorted(body.keys()))
check('GET /routes содержит его', len(client.get('/routes').get_json()) == 1)

print('2) значения по умолчанию и папка')
r = client.post('/routes', json={'segments': SEG1})
body = r.get_json()
check('имя по умолчанию', body.get('name') == 'Новый маршрут', body.get('name'))
check('id увеличился', body.get('id') == 2, body.get('id'))
r = client.post('/routes', json={'name': 'В папке', 'folder': '/Голова 1/ветка/', 'segments': SEG1})
body = r.get_json()
check('папка нормализована', body.get('folder') == 'Голова 1/ветка', body.get('folder'))

print('3) совместимость: points превращаются в цепочку отрезков')
r = client.post('/routes', json={'name': 'Из полилинии',
                                 'points': [[45.0, 38.0], [45.1, 38.1], [45.2, 38.2]]})
body = r.get_json()
check('points -> segments', body.get('segments') == SEG2, body.get('segments'))

print('4) валидация')
bad = [
    ('нет геометрии', {}),
    ('пустой segments', {'segments': []}),
    ('segments не список', {'segments': 'nope'}),
    ('отрезок не из двух точек', {'segments': [[[45.0, 38.0]]]}),
    ('не числа', {'segments': [[['a', 'b'], ['c', 'd']]]}),
    ('широта вне диапазона', {'segments': [[[100.0, 38.0], [45.0, 38.0]]]}),
    ('долгота вне диапазона', {'segments': [[[45.0, 200.0], [45.1, 38.1]]]}),
    ('points: одна точка', {'points': [[45.0, 38.0]]}),
    ('branch не строка', {'segments': SEG1, 'branch': 5}),
]
for name, payload in bad:
    r = client.post('/routes', json=payload)
    check(f'{name} -> 400', r.status_code == 400, r.get_json())

print('5) сохранение на диск')
server._routes.saver.flush()
with open(server.STATE_FILE, encoding='utf-8') as f:
    saved = json.load(f)
check('в файле 4 маршрута', len(saved.get('routes', [])) == 4, len(saved.get('routes', [])))
check('next_id на диске', saved.get('next_id') == 5, saved.get('next_id'))
check('у всех маршрутов segments, без points',
      all('segments' in r and 'points' not in r for r in saved['routes']))

print('6) правка геометрии через PATCH /route/<id>')
r = client.patch('/route/1', json={'segments': SEG1})
body = r.get_json()
check('200', r.status_code == 200, r.status_code)
check('отрезки заменены', body.get('segments') == SEG1, body.get('segments'))
check('имя не изменилось', body.get('name') == 'Сеть', body.get('name'))

for name, payload in [
    ('пустой segments', {'segments': []}),
    ('не числа', {'segments': [[['a', 'b'], ['c', 'd']]]}),
    ('вне диапазона', {'segments': [[[45.0, 200.0], [45.1, 38.1]]]}),
]:
    r = client.patch('/route/1', json=payload)
    check(f'{name} -> 400', r.status_code == 400, r.get_json())
check('после отказов отрезки целы',
      client.get('/routes').get_json()[0]['segments'] == SEG1)

r = client.patch('/route/1', json={'points': [[45.5, 38.5], [45.6, 38.6]]})
check('PATCH points -> цепочка отрезков',
      r.get_json()['segments'] == [[[45.5, 38.5], [45.6, 38.6]]], r.get_json().get('segments'))

r = client.patch('/route/1', json={'name': 'Переименован'})
check('переименование не сбрасывает геометрию',
      r.get_json()['segments'] == [[[45.5, 38.5], [45.6, 38.6]]])
r = client.patch('/route/999', json={'segments': SEG1})
check('несуществующий маршрут -> 404', r.status_code == 404, r.status_code)

print('7) CSRF и content-type')
r = client.post('/routes', data='{"name":"x","segments":[]}', content_type='text/plain')
check('text/plain -> 415', r.status_code == 415, r.status_code)
r = client.post('/routes', json={'name': 'x', 'segments': SEG1},
                headers={'Sec-Fetch-Site': 'cross-site'})
check('cross-site -> 403', r.status_code == 403, r.status_code)
r = client.post('/routes', json={'name': 'x', 'segments': SEG1},
                headers={'Origin': 'http://evil.example'})
check('чужой Origin -> 403', r.status_code == 403, r.status_code)
r = client.post('/routes', json={'name': 'ok', 'segments': SEG1},
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
