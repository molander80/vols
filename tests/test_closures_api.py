"""Проверки API муфт: CRUD, валидация, сохранение в state.json.

Запуск из корня проекта:
    python tests/test_closures_api.py
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

TMP = tempfile.mkdtemp(prefix='vols-closures-')
server.STATE_FILE = os.path.join(TMP, 'state.json')
server._routes.saver.path = server.STATE_FILE
server._routes.saver.cancel()
with server._routes._lock:
    server._routes.closures = {}
    server._routes.routes = {}
    server._routes.custom_folders = set()
    server._routes._next_id = 1

client = server.app.test_client()
failures = []


def check(name, cond, detail=''):
    print(('  OK  ' if cond else ' FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


print('1) создание муфты')
r = client.post('/closures', json={'name': 'Муфта 1', 'lat': 45.3, 'lng': 38.1})
body = r.get_json()
check('201', r.status_code == 201, r.status_code)
check('id выдан', body.get('id') == 1, body.get('id'))
check('координаты сохранены',
      body.get('lat') == 45.3 and body.get('lng') == 38.1, body)
check('GET /closures отдаёт точку', len(client.get('/closures').get_json()) == 1)

print('2) имя по умолчанию')
r = client.post('/closures', json={'lat': 45.4, 'lng': 38.2})
body = r.get_json()
check('имя «Муфта»', body.get('name') == 'Муфта', body.get('name'))
check('id увеличился', body.get('id') == 2, body.get('id'))

print('3) валидация')
bad = [
    ('нет координат', {'name': 'x'}),
    ('не числа', {'lat': 'a', 'lng': 'b'}),
    ('широта вне диапазона', {'lat': 100, 'lng': 38}),
    ('долгота вне диапазона', {'lat': 45, 'lng': 200}),
]
for name, payload in bad:
    r = client.post('/closures', json=payload)
    check(f'{name} -> 400', r.status_code == 400, r.get_json())

print('4) изменение')
r = client.patch('/closures/1', json={'name': 'Муфта A'})
check('имя изменено', r.get_json().get('name') == 'Муфта A', r.get_json())
r = client.patch('/closures/1', json={'lat': 45.5, 'lng': 38.5})
body = r.get_json()
check('координаты изменены',
      body.get('lat') == 45.5 and body.get('lng') == 38.5, body)
r = client.patch('/closures/1', json={'lat': 200})
check('невалидные координаты -> 400', r.status_code == 400, r.get_json())
r = client.patch('/closures/999', json={'name': 'x'})
check('несуществующая -> 404', r.status_code == 404, r.status_code)

print('5) удаление')
r = client.delete('/closures/2')
check('200', r.status_code == 200, r.status_code)
check('осталась одна', len(client.get('/closures').get_json()) == 1)
r = client.delete('/closures/999')
check('удаление несуществующей безопасно', r.status_code == 200, r.status_code)

print('6) сохранение на диск')
server._routes.saver.flush()
with open(server.STATE_FILE, encoding='utf-8') as f:
    saved = json.load(f)
check('closures в файле', len(saved.get('closures', [])) == 1, saved.get('closures'))
check('поля сохранены', saved['closures'][0]['name'] == 'Муфта A',
      saved['closures'][0])

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
