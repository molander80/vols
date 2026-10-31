/* Smoke-тест клиентской функции gponStatusKey (из templates/index.html).

   Извлекает тело функции прямо из HTML и проверяет маппинг статусов
   на ключи active/off/block/other. Запуск (нужен node >= 14):

       node tests/test_gpon_status.js
*/

'use strict';

const fs = require('fs');
const path = require('path');

const htmlPath = path.join(__dirname, '..', 'templates', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');

// Извлекаем тело функции gponStatusKey (от 'function gponStatusKey' до парной '}')
const marker = 'function gponStatusKey(';
const start = html.indexOf(marker);
if (start === -1) {
  console.error('gponStatusKey not found in index.html');
  process.exit(1);
}

let depth = 0;
let bodyStart = html.indexOf('{', start);
let i = bodyStart;
let end = -1;
for (; i < html.length; i++) {
  if (html[i] === '{') depth++;
  else if (html[i] === '}') {
    depth--;
    if (depth === 0) { end = i; break; }
  }
}
if (end === -1) {
  console.error('could not locate end of gponStatusKey body');
  process.exit(1);
}

const body = html.slice(bodyStart + 1, end);
const gponStatusKey = new Function('s', body);

const cases = [
  // [вход, ожидаемый ключ]
  ['Активна (0)', 'active'],
  ['Заблокирована из-за нехватки средств (4)', 'block'],
  ['Отключена', 'off'],
  ['Active', 'active'],
  ['disabled', 'off'],
  ['Blocked', 'block'],
  ['Не оплачено', 'block'],
  ['', 'other'],
  [null, 'other'],
  [undefined, 'other'],
  ['какой-то неизвестный статус', 'other'],
];

let failed = 0;
for (const [input, expected] of cases) {
  const got = gponStatusKey(input);
  const ok = got === expected;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  gponStatusKey(${JSON.stringify(input)}) => ${got}${ok ? '' : ` (ожидалось ${expected})`}`);
}

if (failed) {
  console.error(`\n${failed} проверок не прошли`);
  process.exit(1);
}
console.log(`\nOK: ${cases.length} проверок`);
