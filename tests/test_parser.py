"""Smoke-тесты парсера KML/KMZ.

Проверяют только функции разбора маршрутов (без запуска Flask-сервера):
  - _strip_ns
  - _parse_kml_bytes
  - parse_route_file
  - _safe_name (генерация имён файлов при экспорте дерева)

Запуск (из корня проекта, с активированным окружением):
    python -m unittest discover -s tests -v
    # или
    python -m pytest tests/
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

# Добавляем корень проекта в sys.path, чтобы импортировать app.py
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app  # noqa: E402


KML_NAMESPACE = 'xmlns="http://www.opengis.net/kml/2.2"'


def _kml_body(placemarks: str) -> bytes:
    """Оборачивает placemarks в минимальный корректный KML-документ."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<kml {KML_NAMESPACE}>\n'
        f'<Document>\n'
        f'{placemarks}\n'
        f'</Document>\n'
        f'</kml>\n'
    ).encode('utf-8')


class TestStripNs(unittest.TestCase):
    def test_plain_tag(self):
        self.assertEqual(app._strip_ns('Placemark'), 'Placemark')

    def test_namespaced_tag(self):
        self.assertEqual(
            app._strip_ns('{http://www.opengis.net/kml/2.2}Placemark'),
            'Placemark',
        )

    def test_namespaced_without_brace_returns_as_is(self):
        self.assertEqual(app._strip_ns('coordinates'), 'coordinates')


class TestParseKmlBytes(unittest.TestCase):
    def test_basic_placemark(self):
        body = _kml_body(
            '<Placemark>'
            '<name>Test Route</name>'
            '<LineString>'
            '<coordinates>38.0,45.0,0 38.5,45.5,0</coordinates>'
            '</LineString>'
            '</Placemark>'
        )
        result = app._parse_kml_bytes(body)
        self.assertEqual(len(result), 1)
        name, pts = result[0]
        self.assertEqual(name, 'Test Route')
        # Координаты меняются местами: [lat, lon]
        self.assertEqual(pts, [[45.0, 38.0], [45.5, 38.5]])

    def test_default_name_when_missing(self):
        body = _kml_body(
            '<Placemark>'
            '<LineString>'
            '<coordinates>10.0,20.0</coordinates>'
            '</LineString>'
            '</Placemark>'
        )
        result = app._parse_kml_bytes(body)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 'route')

    def test_multiple_linestrings_in_one_placemark(self):
        body = _kml_body(
            '<Placemark>'
            '<name>Multi</name>'
            '<LineString><coordinates>1,2 3,4</coordinates></LineString>'
            '<LineString><coordinates>5,6</coordinates></LineString>'
            '</Placemark>'
        )
        result = app._parse_kml_bytes(body)
        # Каждый LineString — отдельная запись, но с тем же именем
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], 'Multi')
        self.assertEqual(result[1][0], 'Multi')

    def test_multiple_placemarks(self):
        body = _kml_body(
            '<Placemark><name>A</name>'
            '<LineString><coordinates>1,2</coordinates></LineString></Placemark>'
            '<Placemark><name>B</name>'
            '<LineString><coordinates>3,4</coordinates></LineString></Placemark>'
        )
        result = app._parse_kml_bytes(body)
        self.assertEqual([r[0] for r in result], ['A', 'B'])

    def test_namespaced_placemark_with_coordinates(self):
        body = _kml_body(
            '<Placemark><name>NS</name>'
            '<LineString><coordinates>1,2 3,4</coordinates></LineString>'
            '</Placemark>'
        )
        result = app._parse_kml_bytes(body)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], [[2.0, 1.0], [4.0, 3.0]])

    def test_malformed_xml_returns_empty(self):
        result = app._parse_kml_bytes(b'<kml><Placemark>')
        self.assertEqual(result, [])

    def test_empty_coordinates_skipped(self):
        body = _kml_body(
            '<Placemark><name>Empty</name>'
            '<LineString><coordinates>   </coordinates></LineString>'
            '</Placemark>'
        )
        result = app._parse_kml_bytes(body)
        self.assertEqual(result, [])


class TestParseRouteFile(unittest.TestCase):
    def test_kml_file(self):
        body = _kml_body(
            '<Placemark><name>R</name>'
            '<LineString><coordinates>10,20</coordinates></LineString>'
            '</Placemark>'
        )
        with tempfile.NamedTemporaryFile(suffix='.kml', delete=False) as f:
            f.write(body)
            tmp = f.name
        try:
            result = app.parse_route_file(tmp, '.kml')
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0][0], 'R')
        finally:
            os.remove(tmp)

    def test_kmz_file(self):
        body = _kml_body(
            '<Placemark><name>InZip</name>'
            '<LineString><coordinates>11,22</coordinates></LineString>'
            '</Placemark>'
        )
        with tempfile.NamedTemporaryFile(suffix='.kmz', delete=False) as f:
            with zipfile.ZipFile(f, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr('doc.kml', body)
            tmp = f.name
        try:
            result = app.parse_route_file(tmp, '.kmz')
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0][0], 'InZip')
            self.assertEqual(result[0][1], [[22.0, 11.0]])
        finally:
            os.remove(tmp)

    def test_corrupt_kmz_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix='.kmz', delete=False) as f:
            f.write(b'this is not a zip file')
            tmp = f.name
        try:
            result = app.parse_route_file(tmp, '.kmz')
            self.assertEqual(result, [])
        finally:
            os.remove(tmp)


class TestStateValidation(unittest.TestCase):
    def test_valid_state_payload(self):
        payload = {'routes': [], 'customFolders': [], 'next_id': 1}
        self.assertIsNone(app._validate_state_payload(payload))

    def test_state_payload_must_be_dict(self):
        self.assertIsNotNone(app._validate_state_payload([]))

    def test_state_payload_bad_next_id(self):
        payload = {'routes': [], 'customFolders': [], 'next_id': 'x'}
        self.assertIsNotNone(app._validate_state_payload(payload))

    def test_state_payload_bad_routes(self):
        payload = {'routes': {}, 'customFolders': [], 'next_id': 1}
        self.assertIsNotNone(app._validate_state_payload(payload))


class TestGponValidation(unittest.TestCase):
    def test_valid_gpon_payload(self):
        payload = {'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': None}
        self.assertIsNone(app._validate_gpon_payload(payload))

    def test_gpon_payload_with_string_vgfile(self):
        payload = {'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': 'v.csv'}
        self.assertIsNone(app._validate_gpon_payload(payload))

    def test_gpon_payload_bad_geo(self):
        payload = {'geo': [], 'onus': [], 'geoFiles': [], 'vgFile': None}
        self.assertIsNotNone(app._validate_gpon_payload(payload))

    def test_gpon_payload_bad_vgfile_type(self):
        payload = {'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': 123}
        self.assertIsNotNone(app._validate_gpon_payload(payload))


class TestAllocRouteId(unittest.TestCase):
    def test_returns_sequential_ids(self):
        a = app._alloc_route_id()
        b = app._alloc_route_id()
        c = app._alloc_route_id()
        self.assertEqual(b, a + 1)
        self.assertEqual(c, b + 1)


class TestGponApply(unittest.TestCase):
    def test_apply_geo(self):
        before_geo = app.GPON['geo']
        before_onus = app.GPON['onus']
        try:
            err = app._gpon_apply({'geo': {'x': 'y'}})
            self.assertIsNone(err)
            self.assertEqual(app.GPON['geo'], {'x': 'y'})
            self.assertEqual(app.GPON['onus'], before_onus)
        finally:
            app.GPON['geo'] = before_geo
            app.GPON['onus'] = before_onus

    def test_apply_bad_type(self):
        self.assertIsNotNone(app._gpon_apply({'geo': []}))
        self.assertIsNotNone(app._gpon_apply({'onus': 'x'}))

    def test_apply_requires_dict(self):
        self.assertIsNotNone(app._gpon_apply([]))


class TestSafeName(unittest.TestCase):
    def test_strip_and_sanitize(self):
        self.assertEqual(app._safe_name('  Голова 6 / ветка 11  '), 'Голова 6 _ ветка 11')

    def test_reserved_windows_name(self):
        self.assertEqual(app._safe_name('CON'), '_CON')
        self.assertEqual(app._safe_name('aux'), '_aux')

    def test_empty_becomes_unnamed(self):
        self.assertEqual(app._safe_name('   '), 'unnamed')
        self.assertEqual(app._safe_name(None), 'unnamed')

    def test_trailing_dots_and_spaces(self):
        self.assertEqual(app._safe_name('name... '), 'name')


if __name__ == '__main__':
    unittest.main()
