"""Проверки HTTP-обвязки: gzip, security-заголовки, локальная статика.

Запуск из корня проекта:
    python tests/test_http.py

Реальные data/*.json только читаются, ничего не пишется.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app  # noqa: E402

client = app.app.test_client()
failures = []


def check(name, cond, detail=''):
    print(('  OK  ' if cond else ' FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


print('1) лимит размера загрузки')
check('MAX_CONTENT_LENGTH = 256 МБ',
      app.app.config.get('MAX_CONTENT_LENGTH') == 256 * 1024 * 1024,
      app.app.config.get('MAX_CONTENT_LENGTH'))

print('2) локальная статика вместо CDN')
r = client.get('/static/leaflet/leaflet.js')
check('leaflet.js отдаётся', r.status_code == 200 and len(r.data) > 100_000,
      f'{r.status_code}, {len(r.data)} байт')
r = client.get('/static/xlsx/xlsx.full.min.js')
check('xlsx.full.min.js отдаётся', r.status_code == 200 and len(r.data) > 500_000,
      f'{r.status_code}, {len(r.data)} байт')
for path in ('/static/leaflet/leaflet.css',
             '/static/markercluster/MarkerCluster.css',
             '/static/markercluster/MarkerCluster.Default.css',
             '/static/markercluster/leaflet.markercluster.js'):
    r = client.get(path)
    check(f'{path}', r.status_code == 200, r.status_code)

print('3) в HTML нет внешних CDN')
r = client.get('/')
html = r.data.decode('utf-8')
check('index 200', r.status_code == 200)
check('нет unpkg/jsdelivr', 'unpkg.com' not in html and 'jsdelivr' not in html)
check('есть /static/leaflet/leaflet.js', '/static/leaflet/leaflet.js' in html)

print('4) gzip и заголовки')
r = client.get('/routes', headers={'Accept-Encoding': 'gzip'})
check('JSON сжат gzip', r.headers.get('Content-Encoding') == 'gzip',
      r.headers.get('Content-Encoding'))
check('Vary: Accept-Encoding', 'Accept-Encoding' in (r.headers.get('Vary') or ''))
check('X-Content-Type-Options', r.headers.get('X-Content-Type-Options') == 'nosniff')
check('X-Frame-Options', r.headers.get('X-Frame-Options') == 'DENY')
check('Referrer-Policy', r.headers.get('Referrer-Policy') == 'no-referrer')

r_plain = client.get('/routes')
check('без Accept-Encoding не сжимает', r_plain.headers.get('Content-Encoding') is None,
      r_plain.headers.get('Content-Encoding'))
check('Content-Length сжатого меньше несжатого',
      len(r.data) < len(r_plain.data), f'{len(r.data)} < {len(r_plain.data)}')

print('5) большой GPON-ответ сжимается')
r = client.get('/gpon/state', headers={'Accept-Encoding': 'gzip'})
check('/gpon/state gzip', r.headers.get('Content-Encoding') == 'gzip')
raw = client.get('/gpon/state').data
check('экономия не меньше 5x', len(r.data) * 5 < len(raw),
      f'{len(r.data)} vs {len(raw)} байт')

print('6) app.js вынесен из шаблона и статика кэшируется')
r = client.get('/static/app.js')
check('/static/app.js отдаётся', r.status_code == 200 and len(r.data) > 50_000,
      f'{r.status_code}, {len(r.data)} байт')
check('app.js с no-cache', r.headers.get('Cache-Control') == 'no-cache',
      r.headers.get('Cache-Control'))
check('HTML ссылается на /static/app.js', '/static/app.js' in html)
r = client.get('/static/leaflet/leaflet.js')
check('вендорная статика в кэше', 'max-age=2592000' in (r.headers.get('Cache-Control') or ''),
      r.headers.get('Cache-Control'))

print()
if failures:
    print('FAILED:', len(failures))
    for name in failures:
        print(' -', name)
    sys.exit(1)
print('ALL CHECKS PASSED')
