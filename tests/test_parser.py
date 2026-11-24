"""Smoke-тесты парсера KML/KMZ и валидаторов состояния.

Проверяют разбор маршрутов и защиту импорта, без запуска Flask-сервера:
  - _strip_ns
  - _parse_kml_bytes
  - parse_route_file
  - _safe_name (имена файлов при экспорте дерева)
  - _validate_routes_state / _validate_gpon_state

Запуск из корня проекта:
    python tests/test_parser.py
    # или
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
import zipfile

# Корень проекта в sys.path, чтобы импортировать app.py
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
        # Каждый LineString — отдельная запись с тем же именем
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
        self.assertEqual(app._parse_kml_bytes(b'<kml><Placemark>'), [])

    def test_empty_coordinates_skipped(self):
        body = _kml_body(
            '<Placemark><name>Empty</name>'
            '<LineString><coordinates>   </coordinates></LineString>'
            '</Placemark>'
        )
        self.assertEqual(app._parse_kml_bytes(body), [])


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
            self.assertEqual(app.parse_route_file(tmp, '.kmz'), [])
        finally:
            os.remove(tmp)


class TestRoutesValidation(unittest.TestCase):
    def test_valid_minimal(self):
        payload = {'routes': [], 'customFolders': [], 'next_id': 1}
        self.assertIsNone(app._validate_routes_state(payload))

    def test_valid_route(self):
        payload = {
            'routes': [{
                'id': 1, 'name': 'a', 'branch': None, 'folder': 'голова 1',
                'visible': True, 'points': [[45.0, 38.0]],
            }],
            'customFolders': ['a/b'],
            'next_id': 2,
        }
        self.assertIsNone(app._validate_routes_state(payload))

    def test_payload_must_be_dict(self):
        self.assertIsNotNone(app._validate_routes_state([]))

    def test_bad_routes_type(self):
        self.assertIsNotNone(app._validate_routes_state({'routes': {}}))

    def test_bad_route_id(self):
        self.assertIsNotNone(app._validate_routes_state({'routes': [{'id': 'x'}]}))

    def test_bad_points(self):
        payload = {'routes': [{'id': 1, 'points': 'nope'}]}
        self.assertIsNotNone(app._validate_routes_state(payload))

    def test_bad_custom_folders(self):
        self.assertIsNotNone(app._validate_routes_state({'routes': [], 'customFolders': 'x'}))


    def test_valid_segments(self):
        payload = {'routes': [{'id': 1, 'segments': [[[45.0, 38.0], [45.1, 38.1]]]}]}
        self.assertIsNone(app._validate_routes_state(payload))

    def test_bad_segments(self):
        payload = {'routes': [{'id': 1, 'segments': [[[45.0, 38.0]]]}]}
        self.assertIsNotNone(app._validate_routes_state(payload))


    def test_valid_closures(self):
        payload = {'routes': [], 'closures': [
            {'id': 1, 'name': 'Муфта', 'lat': 45.0, 'lng': 38.0}]}
        self.assertIsNone(app._validate_routes_state(payload))

    def test_bad_closures(self):
        payload = {'routes': [], 'closures': [
            {'id': 1, 'name': 'Муфта', 'lat': 'x', 'lng': 38.0}]}
        self.assertIsNotNone(app._validate_routes_state(payload))


class TestGponValidation(unittest.TestCase):
    def test_valid_payload(self):
        payload = {'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': None}
        self.assertIsNone(app._validate_gpon_state(payload))

    def test_valid_vgfile(self):
        payload = {'geo': {}, 'onus': [], 'geoFiles': [], 'vgFile': 'v.csv'}
        self.assertIsNone(app._validate_gpon_state(payload))

    def test_bad_geo(self):
        payload = {'geo': [], 'onus': [], 'geoFiles': [], 'vgFile': None}
        self.assertIsNotNone(app._validate_gpon_state(payload))

    def test_bad_geo_point(self):
        payload = {'geo': {'ул ленина, дом 1': {'lat': 'abc', 'lng': 38.0}}}
        self.assertIsNotNone(app._validate_gpon_state(payload))

    def test_bad_onus(self):
        self.assertIsNotNone(app._validate_gpon_state({'geo': {}, 'onus': 'x'}))

    def test_bad_vgfile_type(self):
        self.assertIsNotNone(app._validate_gpon_state({'geo': {}, 'vgFile': 123}))


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
