/* Smoke-тест расчёта длины маршрута (kmzDistanceKm из static/app.js).

   Запуск (нужен node >= 14):
       node tests/test_draw.js
*/

'use strict';

const fs = require('fs');
const path = require('path');

const srcPath = path.join(__dirname, '..', 'static', 'app.js');
const src = fs.readFileSync(srcPath, 'utf8');

const marker = 'function kmzDistanceKm(';
const start = src.indexOf(marker);
if (start === -1) {
  console.error('kmzDistanceKm not found in static/app.js');
  process.exit(1);
}

let depth = 0;
let i = src.indexOf('{', start);
let end = -1;
for (; i < src.length; i++) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') {
    depth--;
    if (depth === 0) { end = i; break; }
  }
}
if (end === -1) {
  console.error('could not locate end of kmzDistanceKm body');
  process.exit(1);
}

const bodyStart = src.indexOf('{', start);
const kmzDistanceKm = new Function('points', src.slice(bodyStart + 1, end));

const cases = [
  // [точки, ожидаемо км, допуск км]
  [[], 0, 0.0001],
  [[[45, 38]], 0, 0.0001],
  [[[45, 38], [45, 38]], 0, 0.0001],
  [[[45, 38], [46, 38]], 111.19, 1],
  [[[45, 38], [45, 39]], 78.7, 2],
  [[[45, 38], [45.01, 38]], 1.11, 0.1],
];

let failed = 0;
for (const [points, expected, tolerance] of cases) {
  const got = kmzDistanceKm(points);
  const ok = Math.abs(got - expected) <= tolerance;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${JSON.stringify(points)} => ${got.toFixed(3)} км` +
    (ok ? '' : ` (ожидалось ${expected} ± ${tolerance})`));
}

// сумма двух отрезков должна складываться
const whole = kmzDistanceKm([[45, 38], [45, 39], [45, 40]]);
const parts = kmzDistanceKm([[45, 38], [45, 39]]) + kmzDistanceKm([[45, 39], [45, 40]]);
const sumOk = Math.abs(whole - parts) < 0.001;
if (!sumOk) failed++;
console.log(`${sumOk ? 'PASS' : 'FAIL'}  отрезки складываются (${whole.toFixed(3)} км)`);

if (failed) {
  console.error(`\n${failed} проверок не прошли`);
  process.exit(1);
}
console.log(`\nOK: ${cases.length + 1} проверок`);
