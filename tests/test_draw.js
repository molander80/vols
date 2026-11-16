/* Smoke-тесты геометрии маршрута из static/app.js:
   kmzDistanceKm и kmzPointSegmentDistance.

   Запуск (нужен node >= 14):
       node tests/test_draw.js
*/

'use strict';

const fs = require('fs');
const path = require('path');

const srcPath = path.join(__dirname, '..', 'static', 'app.js');
const src = fs.readFileSync(srcPath, 'utf8');

let failed = 0;
let checks = 0;

function check(name, ok, detail) {
  checks++;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  [${detail}]` : ''}`);
}

/* Извлекает тело функции по имени и превращает его в новую функцию. */
function extractFunction(name, argNames) {
  const marker = `function ${name}(`;
  const start = src.indexOf(marker);
  if (start === -1) {
    console.error(`${name} not found in static/app.js`);
    process.exit(1);
  }
  let depth = 0;
  const bodyStart = src.indexOf('{', start);
  let end = -1;
  for (let i = bodyStart; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) { end = i; break; }
    }
  }
  if (end === -1) {
    console.error(`could not locate end of ${name}`);
    process.exit(1);
  }
  return new Function(...argNames, src.slice(bodyStart + 1, end));
}

const kmzDistanceKm = extractFunction('kmzDistanceKm', ['points']);
const pointSegment = extractFunction('kmzPointSegmentDistance', ['lat', 'lng', 'a', 'b']);

console.log('1) длина маршрута');
const cases = [
  // [точки, ожидаемо км, допуск км]
  [[], 0, 0.0001],
  [[[45, 38]], 0, 0.0001],
  [[[45, 38], [45, 38]], 0, 0.0001],
  [[[45, 38], [46, 38]], 111.19, 1],
  [[[45, 38], [45, 39]], 78.7, 2],
  [[[45, 38], [45.01, 38]], 1.11, 0.1],
];
for (const [points, expected, tolerance] of cases) {
  const got = kmzDistanceKm(points);
  check(`${JSON.stringify(points)} => ${got.toFixed(3)} км`,
    Math.abs(got - expected) <= tolerance, `ожидалось ${expected} ± ${tolerance}`);
}

const whole = kmzDistanceKm([[45, 38], [45, 39], [45, 40]]);
const parts = kmzDistanceKm([[45, 38], [45, 39]]) + kmzDistanceKm([[45, 39], [45, 40]]);
check('отрезки складываются', Math.abs(whole - parts) < 0.001, `${whole.toFixed(3)} км`);

console.log('2) расстояние до сегмента (выбор места вставки)');
const segCases = [
  ['точка на середине', 45.0, 38.5, [45, 38], [45, 39], 0, 0.001],
  ['точка в 0.1° сбоку', 45.1, 38.5, [45, 38], [45, 39], 0.1, 0.01],
  ['точка за концом', 45.0, 40.0, [45, 38], [45, 39], 0.707, 0.01],
];
for (const [name, lat, lng, a, b, expected, tolerance] of segCases) {
  const got = pointSegment(lat, lng, a, b);
  check(`${name} => ${got.toFixed(3)}`, Math.abs(got - expected) <= tolerance,
    `ожидалось ${expected} ± ${tolerance}`);
}

console.log('3) привязка: ближайшая точка на сегменте');
const closest = extractFunction('kmzClosestOnSegment',
  ['px', 'py', 'ax', 'ay', 'bx', 'by']);
const nearCases = [
  // [название, результат, ожидаемый x, y, расстояние]
  ['середина отрезка', closest(5, 5, 0, 0, 10, 0), 5, 0, 5],
  ['за концом отрезка', closest(20, 0, 0, 0, 10, 0), 10, 0, 10],
  ['вырожденный сегмент', closest(3, 4, 0, 0, 0, 0), 0, 0, 5],
];
for (const [name, res, x, y, dist] of nearCases) {
  const ok = Math.abs(res.x - x) < 1e-6 && Math.abs(res.y - y) < 1e-6 &&
             Math.abs(res.dist - dist) < 1e-6;
  check(`${name} => (${res.x.toFixed(2)}, ${res.y.toFixed(2)}), d=${res.dist.toFixed(2)}`,
    ok, `ожидалось (${x}, ${y}), d=${dist}`);
}

console.log();
if (failed) {
  console.error(`${failed} из ${checks} проверок не прошли`);
  process.exit(1);
}
console.log(`OK: ${checks} проверок`);
