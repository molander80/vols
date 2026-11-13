"""Проверки хранилищ: перенос папок, нормализация ONU, копии снапшотов.

Запуск из корня проекта:
    python tests/test_store.py

Реальные data/*.json не изменяются: пути подменяются на временный каталог.
"""

import json
import os
import shutil
import sys
import tempfile
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app as server  # noqa: E402

TMP = tempfile.mkdtemp(prefix='vols-store-')
failures = []


def check(name, cond, detail=''):
    print(('  OK  ' if cond else ' FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


print('1) folder_move переносит папку с вложенными, не задевая похожие')
store = server.RoutesStore()
store.saver.path = os.path.join(TMP, 'state_routes.json')
store.saver.cancel()
with store._lock:
    store.routes = {
        1: {'id': 1, 'name': 'a', 'branch': None, 'folder': 'Голова 1',
            'visible': True, 'points': []},
        2: {'id': 2, 'name': 'b', 'branch': None, 'folder': 'Голова 1/ветка',
            'visible': True, 'points': []},
        3: {'id': 3, 'name': 'c', 'branch': None, 'folder': 'Голова 10',
            'visible': True, 'points': []},
    }
    store.custom_folders = {'Голова 1', 'Голова 1/ветка', 'Голова 10'}

moved = store.folder_move('Голова 1', 'Голова 2')
check('перемещено 2 маршрута', moved == 2, moved)
check('папка переименована', store.routes[1]['folder'] == 'Голова 2',
      store.routes[1]['folder'])
check('вложенная папка переименована', store.routes[2]['folder'] == 'Голова 2/ветка',
      store.routes[2]['folder'])
check('похожая папка не задета', store.routes[3]['folder'] == 'Голова 10',
      store.routes[3]['folder'])
check('customFolders обновлены',
      store.custom_folders == {'Голова 2', 'Голова 2/ветка', 'Голова 10'},
      store.custom_folders)

moved = store.folder_move('Голова 2', '')
check('перенос в корень', moved == 2 and store.routes[1]['folder'] == '',
      store.routes[1]['folder'])
check('вложенная становится верхней', store.routes[2]['folder'] == 'ветка',
      store.routes[2]['folder'])

print('2) folder_rename не задевает папки с общим префиксом')
store.custom_folders = {'A', 'AB'}
store.folder_rename('A', 'X')
check('A -> X, AB не тронута', store.custom_folders == {'X', 'AB'}, store.custom_folders)

print('3) ONU хранятся без вычисляемых полей')
gpon = server.GponStore()
gpon.saver.path = os.path.join(TMP, 'gpon.json')
gpon.saver.cancel()
gpon.set_geo({'addr': {'lat': 1.0, 'lng': 2.0, 'address': 'Addr'}})
gpon.set_onus([{
    'id': '1', 'login': 'login', 'address': 'Addr', 'status': 'Активна',
    'branch': 'b/1', 'lat': 1.0, 'lng': 2.0, '_statusKey': 'active',
}])
check('в onus нет lat/lng/_statusKey',
      set(gpon.onus[0]) == {'id', 'login', 'address', 'status', 'branch'},
      gpon.onus[0])
check('geo сохранён', gpon.snapshot()['geo']['addr']['lat'] == 1.0)

print('4) snapshot отдаёт копию, а не живые словари')
snap = gpon.snapshot()
snap['onus'][0]['id'] = 'changed'
snap['geo'].clear()
check('onus не изменился', gpon.onus[0]['id'] == '1', gpon.onus[0]['id'])
check('geo не изменился', 'addr' in gpon.geo)

print('5) параллельная запись не ломает JSON')
path = os.path.join(TMP, 'saver.json')
state = {'n': 0}
state_lock = threading.Lock()


def payload_fn():
    with state_lock:
        return {'n': state['n'], 'items': list(range(500))}


saver = server.DebouncedSaver(path, payload_fn, debounce_sec=0.01)


def writer():
    for i in range(25):
        with state_lock:
            state['n'] = i
        saver.save_now()


threads = [threading.Thread(target=writer) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)
check('файл читается как JSON', data.get('items') == list(range(500)))
check('временный .tmp убран', not os.path.exists(path + '.tmp'))

print('6) эндпоинт /folders/move')
server.STATE_FILE = os.path.join(TMP, 'api_state.json')
server._routes.saver.path = server.STATE_FILE
server._routes.saver.cancel()
with server._routes._lock:
    server._routes.routes = {
        1: {'id': 1, 'name': 'a', 'branch': None, 'folder': 'P',
            'visible': True, 'points': []},
        2: {'id': 2, 'name': 'b', 'branch': None, 'folder': 'P/Q',
            'visible': True, 'points': []},
        3: {'id': 3, 'name': 'c', 'branch': None, 'folder': 'PP',
            'visible': True, 'points': []},
    }
    server._routes.custom_folders = set()

client = server.app.test_client()
r = client.post('/folders/move', json={'old': 'P', 'new': 'N'})
body = r.get_json()
check('200 и 2 маршрута', r.status_code == 200 and body.get('routes') == 2, body)
check('PP не задет', server._routes.routes[3]['folder'] == 'PP',
      server._routes.routes[3]['folder'])
r = client.post('/folders/move', json={'old': 'N', 'new': 'N/sub'})
check('вложенность отклонена', r.status_code == 400, r.get_json())
r = client.post('/folders/move', json={'old': '', 'new': 'x'})
check('пустой old отклонён', r.status_code == 400, r.get_json())

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
