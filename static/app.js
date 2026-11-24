'use strict';

/* ==========================================================================
   CONSTANTS
   ========================================================================== */
const ACT = { SAVE: 'save', CANCEL: 'cancel', LINK: 'link-gpon' };
const CLS = {
  EDITABLE: 'editable',
  HIGHLIGHT: 'highlighted',
  LINKED: 'linked',
  BRANCH_HIGHLIGHT: 'branch-highlight',
  BRANCH_HIGHLIGHT_LINKED: 'branch-highlight-linked',
};

const GPON_STATUS_COLOR = {
  active: '#22c55e', off: '#94a3b8', block: '#ef4444', other: '#94a3b8',
};
const GPON_STATUS_CLASS = {
  active: 'stat-active', off: 'stat-off', block: 'stat-block', other: '',
};

/* ==========================================================================
   HELPERS
   ========================================================================== */
const $ = id => document.getElementById(id);
const esc = s => (s || '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const stripBOM = s => s.charCodeAt(0) === 0xFEFF ? s.slice(1) : s;

const patchRoute = (id, patch) =>
  fetch(`/route/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }).catch(err => console.error('PATCH failed', id, err));

const patchRoutesBulk = (ids, patch) => {
  if (!ids || !ids.length) return Promise.resolve();
  return fetch('/routes/bulk', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, patch }),
  }).catch(err => console.warn('bulk patch failed', err));
};

/* ==========================================================================
   BASEMAPS — онлайн и локальные MBTiles
   ========================================================================== */
function mapLayerForLocal(m){
  if (localLayers.has(m.file)) return localLayers.get(m.file);
  const layer = L.tileLayer(`/maps/${encodeURIComponent(m.file)}/tile/{z}/{x}/{y}.png`, {
    minZoom: Number.isFinite(m.minzoom) ? m.minzoom : 0,
    maxZoom: Number.isFinite(m.maxzoom) ? m.maxzoom : 19,
    tms: m.scheme !== 'xyz',
    attribution: `Локальная карта: ${m.name}`,
  });
  localLayers.set(m.file, layer);
  return layer;
}

function mapsMenuToggle(force){
  const menu = $('mapsMenu');
  const btn = $('btnMaps');
  if (!menu || !btn) return;
  const open = typeof force === 'boolean' ? force : menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !open);
  btn.classList.toggle('active', open);
}

function mapsRenderMenu(){
  const menu = $('mapsMenu');
  if (!menu) return;
  menu.innerHTML = '';

  const sel = K.baseSel || { kind: 'online', name: 'OSM DE' };

  const caption = (text) => {
    const d = document.createElement('div');
    d.className = 'menu-caption';
    d.textContent = text;
    menu.appendChild(d);
  };

  caption('Онлайн');
  for (const name of Object.keys(basemaps)){
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'menu-item' + (sel.kind === 'online' && sel.name === name ? ' active' : '');
    b.textContent = name;
    b.onclick = () => mapsSetBase('online', name);
    menu.appendChild(b);
  }

  caption('Локальные (офлайн)');
  if (!K.localMaps.length){
    const d = document.createElement('div');
    d.className = 'menu-hint';
    d.textContent = 'Файлы .mbtiles не найдены';
    menu.appendChild(d);
  }
  for (const m of K.localMaps){
    const row = document.createElement('div');
    row.className = 'menu-row';

    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'menu-item' + (sel.kind === 'local' && sel.name === m.file ? ' active' : '');
    b.textContent = '🗺 ' + m.name;
    const zmin = Number.isFinite(m.minzoom) ? m.minzoom : '?';
    const zmax = Number.isFinite(m.maxzoom) ? m.maxzoom : '?';
    b.title = `${m.file} · z${zmin}–${zmax} · ${(m.size / 1048576).toFixed(1)} МБ`;
    b.onclick = () => mapsSetBase('local', m.file);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'menu-del';
    del.textContent = '🗑';
    del.title = 'Удалить карту из приложения';
    del.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm(`Удалить карту «${m.name}»?`)) return;
      try {
        await fetch(`/maps/${encodeURIComponent(m.file)}`, { method: 'DELETE' });
      } catch (err){ console.warn('map delete failed', err); }
      if (K.baseSel?.kind === 'local' && K.baseSel.name === m.file){
        mapsSetBase('online', 'OSM DE');
      }
      await mapsLoad();
    };

    row.append(b, del);
    menu.appendChild(row);
  }

  const up = document.createElement('button');
  up.type = 'button';
  up.className = 'menu-item';
  up.textContent = '📤 Загрузить .mbtiles…';
  up.onclick = () => {
    mapsMenuToggle(false);
    $('mapFileInput').click();
  };
  menu.appendChild(up);
}

function mapsSetBase(kind, name){
  const layer = kind === 'online'
    ? basemaps[name]
    : (() => {
        const m = K.localMaps.find(x => x.file === name);
        return m ? mapLayerForLocal(m) : null;
      })();
  if (!layer) return;
  for (const l of Object.values(basemaps)) if (map.hasLayer(l)) map.removeLayer(l);
  for (const l of localLayers.values()) if (map.hasLayer(l)) map.removeLayer(l);
  layer.addTo(map);
  activeBase = layer;
  K.baseSel = { kind, name };
  try { localStorage.setItem('base-map', JSON.stringify(K.baseSel)); } catch(_) {}
  mapsRenderMenu();
  mapsMenuToggle(false);
}

async function mapsLoad(){
  try {
    const res = await fetch('/maps');
    K.localMaps = await res.json();
  } catch (e){
    K.localMaps = [];
  }
  mapsRenderMenu();

  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('base-map') || 'null'); } catch(_) {}
  if (saved?.kind === 'local' && K.localMaps.some(m => m.file === saved.name)){
    mapsSetBase('local', saved.name);
    return;
  }
  mapsSetBase('online', saved?.kind === 'online' && basemaps[saved.name] ? saved.name : 'OSM DE');
}

$('btnMaps').onclick = e => { e.stopPropagation(); mapsMenuToggle(); };
document.addEventListener('click', e => {
  if (!e.target.closest('#mapsWrap')) mapsMenuToggle(false);
});

$('mapFileInput').onchange = async () => {
  const file = $('mapFileInput').files?.[0];
  if (!file) return;

  const fd = new FormData();
  fd.append('file', file);

  let res;
  try {
    res = await fetch('/maps', { method: 'POST', body: fd });
  } catch (e){
    alert('Не удалось загрузить карту: ' + e.message);
    return;
  }
  const data = await res.json().catch(() => ({}));
  $('mapFileInput').value = '';
  if (!res.ok){
    alert('Не удалось загрузить карту: ' + (data.error || res.status));
    return;
  }
  await mapsLoad();
  mapsSetBase('local', data.file);
};


const createRoute = (payload) =>
  fetch('/routes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json()).catch(err => {
    console.warn('create route failed', err);
    return null;
  });

/* ==========================================================================
   STATE
   ========================================================================== */
const K = {
  routes: [],
  layers: new Map(),
  hiddenBranches: new Set(),
  mergeMarked: new Set(),
  customFolders: new Set(),
  dragPayload: null,
  search: '',
  linked: null,
  draw: null,
  editGeom: null,
  expandedFolders: new Set(),
  snapHint: null,
  geomUndoStack: [],
  snap: true,
  localMaps: [],
  baseSel: null,
  closures: [],
  closureFolders: new Set(),
  expandedClosureFolders: new Set(),
  closuresHidden: new Set(),
  closureMarkers: new Map(),
  closureMode: false,
};

const G = {
  geo: new Map(),
  onus: [],
  byBranch: new Map(),
  filters: { status: new Set(), branch: new Set() },
  branchLayers: {},
  allMarkers: [],
  searchMode: false,
  searchHighlightLayer: null,
  geoFiles: [],
  vgFile: '',
  editMode: null,
  markersVisible: true,
  expandedFolders: new Set(),
  expandedBeforeSearch: null,
};

/* ==========================================================================
   MAP
   ========================================================================== */
const map = L.map('map', { preferCanvas: true, zoomControl: true }).setView([45.3, 38.1], 6);

const basemaps = {
  'OSM DE': L.tileLayer('https://tile.openstreetmap.de/{z}/{x}/{y}.png', { maxZoom: 19 }),
  'Esri Улицы': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 }),
  'Esri Спутник': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 }),
  'Esri Топо': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 }),
  'Carto Светлая': L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', { maxZoom: 19, subdomains: 'abcd' }),
  'Carto Тёмная': L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { maxZoom: 19, subdomains: 'abcd' }),
};
let activeBase = null;
const localLayers = new Map();

let __curPopup = null;

/* ==========================================================================
   MODE SWITCH
   ========================================================================== */
function setMode(mode){
  if (mode !== 'kmz' && mode !== 'gpon') return;
  if (K.draw) kmzDrawStop();
  if (K.editGeom) kmzGeomCancel();

  document.querySelectorAll('.mode-switch button').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
  });

  const isKmz = mode === 'kmz';
  $('kmzControls').style.display = isKmz ? 'flex' : 'none';
  $('gponControls').style.display = isKmz ? 'none' : 'flex';
  $('sidebarKmz').classList.toggle('hidden', !isKmz);
  $('sidebarGpon').classList.toggle('hidden', isKmz);
  $('statKmz').classList.toggle('hidden', !isKmz);
  $('statGpon').classList.toggle('hidden', isKmz);
  $('search').placeholder = isKmz
    ? '🔎 Поиск по имени, ветке, папке…'
    : '🔎 Поиск: адрес, логин, ID, ветка…';

  if (isKmz){
    kmzRenderTree();
    kmzApplyFilters();
    $('s-routes').textContent = K.routes.length;
  } else {
    gponUpdateStats();
  }
  kmzUpdateLinkBadge();
}

document.querySelectorAll('.mode-switch button').forEach(b => {
  b.addEventListener('click', () => setMode(b.dataset.mode));
});

/* ==========================================================================
   KMZ — LAYERS
   ========================================================================== */
const _simplifiedCache = new WeakMap();

function kmzStringToColor(str){
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0;
  return `hsl(${Math.abs(h) % 360}, 70%, 45%)`;
}

// Видимость маршрута на карте: сохранённое r.visible + режим просмотра.
// Изоляция (K.linked) живёт только в памяти и на сервере не сохраняется.
function kmzIsRouteShown(r){
  if (!r.visible) return false;
  if (K.linked && K.linked.id !== r.id) return false;
  if (r.branch && K.hiddenBranches.has(r.branch)) return false;
  return true;
}

function kmzSimplify(points, minDist = 3){
  if (points.length < 3) return points;
  const out = [points[0]];
  const R = 6371000, toRad = Math.PI / 180;
  let lastLat = points[0][0], lastLon = points[0][1];
  for (let i = 1; i < points.length - 1; i++){
    const [lat, lon] = points[i];
    const dLat = (lat - lastLat) * toRad;
    const dLon = (lon - lastLon) * toRad;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(lastLat * toRad) * Math.cos(lat * toRad) * Math.sin(dLon / 2) ** 2;
    const d = 2 * R * Math.asin(Math.sqrt(a));
    if (d >= minDist){ out.push([lat, lon]); lastLat = lat; lastLon = lon; }
  }
  out.push(points[points.length - 1]);
  return out;
}

// Геометрия маршрута — сеть отрезков: [[[lat, lon], [lat, lon]], ...].
// Старые данные с points (одна полилиния) конвертируются в цепочку на лету.
function kmzRouteSegments(r){
  if (r && Array.isArray(r.segments) && r.segments.length) return r.segments;
  const pts = (r && r.points) || [];
  const out = [];
  for (let i = 0; i < pts.length - 1; i++) out.push([pts[i], pts[i + 1]]);
  return out;
}

function kmzSegmentsDistanceKm(segments){
  let total = 0;
  for (const seg of segments || []) total += kmzDistanceKm(seg);
  return total;
}

function kmzBuildLayer(r){
  const segments = kmzRouteSegments(r);
  const color = kmzStringToColor(r.branch || r.name);

  // каждая пара точек рисуется отдельным отрезком, без соединения «через»
  const halo = L.polyline(segments, {
    color: '#ffffff', weight: 7, opacity: .75,
    lineCap: 'round', lineJoin: 'round', interactive: false,
  });
  const line = L.polyline(segments, {
    color, weight: 3.5, opacity: .95,
    lineCap: 'round', lineJoin: 'round',
  });

  line.on('click', (e) => {
    if (K.draw || K.editGeom) return;   // в режимах рисования/правки клик не изолирует
    L.DomEvent.stopPropagation(e);
    if (K.linked && K.linked.id === r.id) kmzClearLink();
    else kmzLinkRoute(r.id);
  });
  line.bindTooltip(r.name, { sticky: true });

  return L.layerGroup([halo, line]);
}

function kmzUpdateTooltips(){
  for (const r of K.routes){
    const group = K.layers.get(r.id);
    if (!group || !group.getLayers) continue;
    for (const layer of group.getLayers()){
      if (layer.getTooltip && layer.getTooltip()) layer.setTooltipContent(r.name);
    }
  }
}

function kmzRemoveLayer(rid){
  const layer = K.layers.get(rid);
  if (!layer) return;
  if (map.hasLayer(layer)) map.removeLayer(layer);
  K.layers.delete(rid);
}

function kmzAddLayersFor(routes){
  let added = 0;
  for (const r of routes){
    if (K.layers.has(r.id)) continue;
    const layer = kmzBuildLayer(r);
    K.layers.set(r.id, layer);
    if (kmzIsRouteShown(r)) layer.addTo(map);
    added++;
  }
  return added;
}

function kmzFitAll(){
  if (K.linked) return;
  const bounds = [];
  for (const layer of K.layers.values()){
    try {
      const b = layer.getBounds?.();
      if (b && b.isValid()) bounds.push(b);
    } catch(_) {}
  }
  if (!bounds.length) return;
  const merged = bounds.reduce((acc, b) => acc.extend(b), bounds[0]);
  try { map.fitBounds(merged, { padding: [30, 30] }); } catch(_) {}
}

function kmzFitToLinkedRoute(){
  if (!K.linked) return;
  const pts = [];

  const r = K.routes.find(x => x.id === K.linked.id);
  for (const seg of kmzRouteSegments(r || {})) pts.push(...seg);

  const key = K.linked.key;
  const activeStatus = G.filters.status.size > 0;
  for (const o of G.onus){
    if (o.lat == null || o.lng == null) continue;
    if (activeStatus && !G.filters.status.has(o._statusKey)) continue;
    if (!gponBranchMatchesKmz(o.branch, key)) continue;
    pts.push([o.lat, o.lng]);
  }
  if (!pts.length) return;

  try { map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 17 }); }
  catch(_) {}
}

function kmzApplyFilters(){
  // в режимах правки/рисования поиск не прячет остальные маршруты:
  // нужен контекст соседних линий
  const q = (K.draw || K.editGeom) ? '' : K.search.toLowerCase();
  for (const r of K.routes){
    const layer = K.layers.get(r.id);
    if (!layer) continue;

    let visible = kmzIsRouteShown(r);
    if (q && !`${r.name} ${r.branch || ''} ${r.folder || ''}`.toLowerCase().includes(q)) visible = false;

    if (visible && !map.hasLayer(layer)) layer.addTo(map);
    if (!visible && map.hasLayer(layer)) map.removeLayer(layer);
  }
}

/* ==========================================================================
   KMZ — TREE
   ========================================================================== */
function kmzCollapseAllFolders(){
  const tree = $('folderTree');
  if (!tree) return;
  K.expandedFolders.clear();
  tree.querySelectorAll('.folder-children').forEach(ch => ch.classList.add('collapsed'));
  tree.querySelectorAll('.folder-toggle').forEach(t => t.textContent = '▸');
}

function kmzCollapseExceptRoute(routeId){
  const tree = $('folderTree');
  if (!tree) return;

  K.expandedFolders.clear();
  tree.querySelectorAll('.folder-children').forEach(ch => ch.classList.add('collapsed'));
  tree.querySelectorAll('.folder-toggle').forEach(t => t.textContent = '▸');
  tree.querySelectorAll('.route-row.linked').forEach(r => r.classList.remove(CLS.LINKED));

  const row = tree.querySelector(`.route-row[data-id="${routeId}"]`);
  if (!row) return;

  let p = row.parentElement;
  while (p && p !== tree){
    if (p.classList?.contains('folder-children')){
      p.classList.remove('collapsed');
      const prev = p.previousElementSibling;
      if (prev?.classList.contains('folder-row')){
        const toggle = prev.querySelector('.folder-toggle');
        if (toggle) toggle.textContent = '▾';
        if (prev.dataset.folderPath) K.expandedFolders.add(prev.dataset.folderPath);
      }
    }
    p = p.parentElement;
  }

  row.classList.add(CLS.LINKED);
  try { row.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch(_) {}
}

/* ==========================================================================
   KMZ — RENDER TREE
   ========================================================================== */
function kmzBuildTree(){
  const root = { name: '', children: new Map(), routes: [] };
  const ensure = (parts) => {
    let n = root;
    for (const p of parts){
      if (!p) continue;
      if (!n.children.has(p)) n.children.set(p, { name: p, children: new Map(), routes: [] });
      n = n.children.get(p);
    }
    return n;
  };
  for (const r of K.routes) ensure((r.folder || '').split('/').filter(Boolean)).routes.push(r);
  for (const p of K.customFolders) ensure(p.split('/').filter(Boolean));
  return root;
}

function kmzCollectRoutes(node){
  const out = [...node.routes];
  for (const c of node.children.values()) out.push(...kmzCollectRoutes(c));
  return out;
}

function kmzRenderTree(){
  const c = $('folderTree');
  if (!c) return;
  c.innerHTML = '';
  kmzRenderNode(kmzBuildTree(), c, [], true);
  renderClosureTree();

  if (K.linked?.type === 'route'){
    kmzCollapseExceptRoute(K.linked.id);
    setTimeout(() => {
      if (K.linked?.type === 'route') kmzCollapseExceptRoute(K.linked.id);
    }, 0);
  }
}

function kmzRenderNode(node, container, parts, isRoot){
  const path = isRoot ? [] : [...parts, node.name];
  const pathStr = path.join('/');

  if (!isRoot && node.name){
    const { row, wrap } = makeFolderRow(node, pathStr);
    container.appendChild(row);
    container.appendChild(wrap);
    container = wrap;
  }

  for (const child of node.children.values()) kmzRenderNode(child, container, path, false);
  for (const r of node.routes) container.appendChild(kmzBuildRouteRow(r));
}

function makeFolderRow(node, pathStr){
  const row = document.createElement('div');
  row.className = 'folder-row';
  row.dataset.folderPath = pathStr;
  row.draggable = true;

  const routes = kmzCollectRoutes(node);
  const total = routes.length;
  const visible = routes.filter(r => r.visible && !(r.branch && K.hiddenBranches.has(r.branch))).length;

  row.addEventListener('dragover', kmzFolderDragOver);
  row.addEventListener('dragleave', e => e.currentTarget.classList.remove('drop-target'));
  row.addEventListener('drop', kmzFolderDrop);
  row.addEventListener('dragstart', e => {
    K.dragPayload = { type: 'folder', path: pathStr };
    e.dataTransfer.effectAllowed = 'move';
  });
  row.addEventListener('dragend', () => { K.dragPayload = null; });

  const opened = K.expandedFolders.has(pathStr);
  const toggle = document.createElement('span');
  toggle.className = 'folder-toggle';
  toggle.textContent = opened ? '▾' : '▸';
  toggle.onclick = e => {
    e.stopPropagation();
    const ch = row.nextElementSibling;
    if (!ch?.classList.contains('folder-children')) return;
    const collapsed = ch.classList.toggle('collapsed');
    toggle.textContent = collapsed ? '▸' : '▾';
    if (collapsed) K.expandedFolders.delete(pathStr);
    else K.expandedFolders.add(pathStr);
  };

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'folder-cb';
  cb.checked = total > 0 && visible === total;
  cb.indeterminate = visible > 0 && visible < total;
  cb.disabled = total === 0;
  cb.title = `${visible} из ${total} видимо`;
  cb.addEventListener('mousedown', e => { e.stopPropagation(); row.draggable = false; });
  cb.addEventListener('mouseup', e => { e.stopPropagation(); row.draggable = true; });
  cb.addEventListener('pointerdown', e => e.stopPropagation());
  cb.addEventListener('click', e => {
    e.stopPropagation();
    cb.indeterminate = false;
    kmzSetFolderVisible(node, cb.checked);
  });

  const nameSpan = document.createElement('span');
  nameSpan.className = 'folder-name';
  nameSpan.textContent = node.name;
  nameSpan.title = `${node.name} — ${visible}/${total}`;
  nameSpan.onclick = e => { e.stopPropagation(); toggle.onclick(e); };

  const cnt = document.createElement('span');
  cnt.className = 'folder-count';
  cnt.textContent = total ? `${visible}/${total}` : '';

  const actions = document.createElement('span');
  actions.className = 'folder-actions';
  actions.innerHTML = `
    <button data-act="rename-folder" title="Переименовать">✏️</button>
    <button data-act="new-subfolder" title="Подпапка">📁+</button>
    <button data-act="delete-folder" title="Удалить">🗑</button>`;
  actions.querySelector('[data-act="rename-folder"]').onclick = e => { e.stopPropagation(); kmzRenameFolder(pathStr); };
  actions.querySelector('[data-act="new-subfolder"]').onclick = e => {
    e.stopPropagation();
    const sub = prompt('Имя подпапки:');
    if (!sub) return;
    const full = `${pathStr}/${sub.trim()}`;
    K.customFolders.add(full);
    kmzFolderAdd(full);
    kmzRenderTree();
  };
  actions.querySelector('[data-act="delete-folder"]').onclick = e => { e.stopPropagation(); kmzDeleteFolder(pathStr); };

  row.append(toggle, cb, nameSpan, cnt, actions);

  const wrap = document.createElement('div');
  wrap.className = 'folder-children';
  // дерево по умолчанию свернуто: раскрыто только то, что пользователь
  // открыл вручную в этой сессии
  if (!K.expandedFolders.has(pathStr)) wrap.classList.add('collapsed');

  return { row, wrap };
}

function kmzBuildRouteRow(r){
  const li = document.createElement('div');
  li.className = 'route-row';
  li.dataset.id = r.id;
  li.draggable = true;

  li.addEventListener('dragstart', e => {
    K.dragPayload = { type: 'route', id: r.id };
    e.dataTransfer.effectAllowed = 'move';
    li.classList.add('dragging');
  });
  li.addEventListener('dragend', () => {
    K.dragPayload = null;
    li.classList.remove('dragging');
    document.querySelectorAll('.folder-row.drop-target').forEach(el => el.classList.remove('drop-target'));
  });

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = r.visible;
  cb.addEventListener('mousedown', e => { e.stopPropagation(); li.draggable = false; });
  cb.addEventListener('mouseup', e => { e.stopPropagation(); li.draggable = true; });
  cb.addEventListener('click', e => e.stopPropagation());
  cb.addEventListener('pointerdown', e => e.stopPropagation());
  cb.onchange = async () => {
    r.visible = cb.checked;
    kmzApplyFilters();
    await patchRoute(r.id, { visible: r.visible });
  };

  const nameSpan = document.createElement('span');
  nameSpan.className = 'route-name';
  nameSpan.textContent = r.name;
  nameSpan.title = `${r.name}${r.branch ? ' — ' + r.branch : ''}`;

  const actions = document.createElement('span');
  actions.className = 'route-actions';
  actions.innerHTML = `
    <button data-act="${ACT.LINK}" title="Связать с GPON">🔗</button>
    <button data-act="geom" title="Редактировать точки на карте">📐</button>
    <button data-act="append" title="Продолжить с конца — или выберите любую вершину">➕</button>
    <button data-act="rename" title="Переименовать">✏️</button>
    <button data-act="merge" title="Отметить для объединения">${K.mergeMarked.has(r.id) ? '☑' : '◻'}</button>
    <button data-act="delete" title="Удалить">✕</button>`;
  actions.querySelector(`[data-act="${ACT.LINK}"]`).onclick = e => {
    e.stopPropagation();
    kmzLinkRoute(r.id);
  };
  actions.querySelector('[data-act="geom"]').onclick = e => {
    e.stopPropagation();
    // повторное нажатие на том же маршруте сохраняет правку
    if (K.editGeom && K.editGeom.id === r.id) kmzGeomSave();
    else kmzGeomBegin(r.id);
  };
  actions.querySelector('[data-act="append"]').onclick = e => {
    e.stopPropagation();
    kmzGeomAppendBegin(r.id);
  };
  actions.querySelector('[data-act="rename"]').onclick = async () => {
    const nn = prompt('Новое имя:', r.name);
    if (!nn) return;
    r.name = nn;
    await patchRoute(r.id, { name: nn });
    kmzRenderTree();
  };
  actions.querySelector('[data-act="merge"]').onclick = () => {
    K.mergeMarked.has(r.id) ? K.mergeMarked.delete(r.id) : K.mergeMarked.add(r.id);
    kmzRenderTree();
  };
  actions.querySelector('[data-act="delete"]').onclick = async () => {
    if (!confirm(`Удалить "${r.name}"?`)) return;
    if (K.editGeom?.id === r.id) kmzGeomCancel();
    if (K.linked?.id === r.id) kmzClearLink();
    await fetch(`/route/${r.id}`, { method: 'DELETE' });
    K.routes = K.routes.filter(x => x.id !== r.id);
    kmzRemoveLayer(r.id);
    kmzRenderTree();
    $('s-routes').textContent = K.routes.length;
  };

  li.append(cb, nameSpan, actions);
  if (K.mergeMarked.has(r.id)) li.classList.add('merge-marked');
  return li;
}

/* ==========================================================================
   KMZ — DnD
   ========================================================================== */
function kmzFolderDragOver(e){
  if (!K.dragPayload) return;
  e.preventDefault();
  e.stopPropagation();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drop-target');
}

async function kmzFolderDrop(e){
  e.preventDefault();
  e.stopPropagation();
  e.currentTarget.classList.remove('drop-target');
  const payload = K.dragPayload;
  if (!payload) return;
  const target = e.currentTarget.dataset.folderPath || '';
  if (payload.type === 'route') await kmzMoveRoutes([payload.id], target);
  else if (payload.type === 'folder') await kmzMoveFolder(payload.path, target);
  K.dragPayload = null;
}

async function kmzMoveRoutes(ids, folder){
  const affected = K.routes.filter(r => ids.includes(r.id));
  if (!affected.length) return;
  const oldFolders = new Set();
  for (const r of affected){
    if (r.folder) oldFolders.add(r.folder);
    r.folder = folder;
  }
  K.customFolders.delete(folder);
  kmzRenderTree();
  await patchRoutesBulk(affected.map(r => r.id), { folder });

  for (const old of oldFolders){
    const still = K.routes.some(r => r.folder === old || (r.folder || '').startsWith(old + '/'));
    if (!still && K.customFolders.has(old)){
      K.customFolders.delete(old);
      kmzFolderDelete(old);
    }
  }
}

async function kmzMoveFolder(src, dst){
  if (!src || dst === src || dst.startsWith(src + '/')) return;
  const base = src.split('/').pop();
  const np = dst ? `${dst}/${base}` : base;

  // один запрос на сервер вместо PATCH на каждый маршрут
  await kmzFolderMove(src, np);
  await kmzLoadRoutes();
  await kmzLoadFolders();
  kmzRenderTree();
}

async function kmzRenameFolder(oldPath){
  const cur = oldPath.split('/').pop();
  const nn = prompt('Новое имя папки:', cur);
  if (!nn || nn === cur) return;
  const parent = oldPath.split('/').slice(0, -1).join('/');
  const np = parent ? `${parent}/${nn.trim()}` : nn.trim();

  await kmzFolderMove(oldPath, np);
  await kmzLoadRoutes();
  await kmzLoadFolders();
  kmzRenderTree();
}

async function kmzDeleteFolder(path){
  const affected = K.routes.filter(r => r.folder === path || (r.folder || '').startsWith(path + '/'));
  if (affected.length && !confirm(`В папке "${path}" ${affected.length} маршрут(ов). Переместить в корень?`)) return;

  for (const r of affected) r.folder = '';
  K.customFolders.delete(path);
  for (const f of [...K.customFolders]) if (f.startsWith(path + '/')) K.customFolders.delete(f);

  kmzRenderTree();
  await patchRoutesBulk(affected.map(r => r.id), { folder: '' });
  await kmzFolderDelete(path);
}

function kmzSetFolderVisible(node, visible){
  const routes = kmzCollectRoutes(node);
  if (!routes.length) return;
  for (const r of routes){
    r.visible = visible;
    if (r.branch){
      visible ? K.hiddenBranches.delete(r.branch) : K.hiddenBranches.add(r.branch);
    }
  }
  kmzApplyFilters();
  kmzRenderTree();
  patchRoutesBulk(routes.map(r => r.id), { visible });
}

/* ==========================================================================
   KMZ — API HELPERS + LOADING
   ========================================================================== */
const kmzFolderAdd = (path) =>
  fetch('/folders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) })
    .catch(e => console.warn('folder add failed', e));

const kmzFolderDelete = (path) =>
  fetch('/folders', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) })
    .catch(e => console.warn('folder delete failed', e));

const kmzFolderRename = (oldPath, newPath) =>
  fetch('/folders/rename', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ old: oldPath, new: newPath }) })
    .catch(e => console.warn('folder rename failed', e));

const kmzFolderMove = (oldPath, newPath) =>
  fetch('/folders/move', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ old: oldPath, new: newPath }) })
    .catch(e => console.warn('folder move failed', e));

async function kmzLoadRoutes({ fit = true } = {}){
  const res = await fetch('/routes');
  const fresh = await res.json();

  const wasEmpty = K.routes.length === 0;
  const oldIds = new Set(K.routes.map(r => r.id));
  const newIds = new Set(fresh.map(r => r.id));

  for (const id of oldIds){
    if (!newIds.has(id)) kmzRemoveLayer(id);
  }

  const oldById = new Map(K.routes.map(r => [r.id, r]));
  K.routes = fresh.map(r => {
    const prev = oldById.get(r.id);
    if (prev && prev.points === r.points) return prev;
    return r;
  });

  kmzRenderTree();

  const brandNew = K.routes.filter(r => !oldIds.has(r.id));
  const addedCount = kmzAddLayersFor(brandNew);

  if (fit && (wasEmpty || addedCount > 0)) kmzFitAll();
  kmzApplyFilters();
  kmzUpdateTooltips();
  $('s-routes').textContent = K.routes.length;
}

async function kmzLoadFolders(){
  try {
    const res = await fetch('/folders');
    K.customFolders = new Set(await res.json());
  } catch(e){ console.warn('folders load failed', e); }
}

async function kmzUpload(files){
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files){
    fd.append('files[]', f, f.name);
    fd.append('paths[]', f.webkitRelativePath || f.name);
  }
  await fetch('/upload', { method: 'POST', body: fd });
  await kmzLoadRoutes();
}

/* ==========================================================================
   KMZ — РИСОВАНИЕ МАРШРУТА (отрезками)
   ========================================================================== */
const DRAW_LINE_STYLE = { color: '#38bdf8', weight: 3, dashArray: '6 6', opacity: .95 };
const DRAFT_DOT_STYLE = { radius: 4, color: '#38bdf8', weight: 2, fillColor: '#fff', fillOpacity: 1 };

function kmzDistanceKm(points){
  if (!points || points.length < 2) return 0;
  const R = 6371, toRad = Math.PI / 180;
  let total = 0;
  for (let i = 1; i < points.length; i++){
    const [lat1, lon1] = points[i - 1];
    const [lat2, lon2] = points[i];
    const dLat = (lat2 - lat1) * toRad;
    const dLon = (lon2 - lon1) * toRad;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) ** 2;
    total += 2 * R * Math.asin(Math.sqrt(a));
  }
  return total;
}

function kmzDrawHint(text){
  const el = $('editHint');
  if (!el) return;
  if (!text){
    const n = K.draw ? K.draw.segments.length : 0;
    const km = K.draw ? kmzSegmentsDistanceKm(K.draw.segments) : 0;
    const tail = K.draw && K.draw.pending ? 'клик задаёт конец отрезка' : 'клик задаёт начало отрезка';
    text = `✏️ Отрезков: ${n} · ${km.toFixed(2)} км — ${tail} · Ctrl+Z — убрать отрезок, правый клик — меню, Enter — сохранить, Esc — завершить`;
  }
  el.textContent = text;
  el.classList.remove('hidden');
}

function kmzDrawRender(){
  if (!K.draw) return;
  if (!K.draw.line){
    K.draw.line = L.polyline(K.draw.segments, DRAW_LINE_STYLE).addTo(map);
    K.draw.dots = L.layerGroup().addTo(map);
  }
  K.draw.line.setLatLngs(K.draw.segments);
  K.draw.dots.clearLayers();

  const seen = new Set();
  const addDot = (p) => {
    const key = `${p[0].toFixed(7)},${p[1].toFixed(7)}`;
    if (seen.has(key)) return;
    seen.add(key);
    L.circleMarker(p, DRAFT_DOT_STYLE).addTo(K.draw.dots);
  };
  for (const seg of K.draw.segments){ addDot(seg[0]); addDot(seg[1]); }
  if (K.draw.pending) addDot(K.draw.pending);

  kmzDrawHint();
}

function kmzDrawStart(){
  if (K.draw) return;
  if (K.editGeom) kmzGeomCancel();
  if (K.closureMode) closureToggle(false);
  if (K.linked) kmzClearLink(false);
  K.draw = { segments: [], pending: null, line: null, dots: null };
  map.doubleClickZoom.disable();
  const btn = $('btnDraw');
  if (btn){ btn.classList.add('active'); btn.textContent = '✏️ Рисую…'; }
  kmzDrawRender();
  kmzApplyFilters();
}

function kmzDrawStop(){
  if (!K.draw) return;
  for (const layer of [K.draw.line, K.draw.dots]){
    if (layer && map.hasLayer(layer)) map.removeLayer(layer);
  }
  K.draw = null;
  map.doubleClickZoom.enable();
  const btn = $('btnDraw');
  if (btn){ btn.classList.remove('active'); btn.textContent = '✏️ Рисовать'; }
  kmzSnapHintShow(null);
  kmzApplyFilters();
  const el = $('editHint');
  if (el) el.classList.add('hidden');
}

function kmzDrawAddPoint(latlng){
  if (!K.draw) return;
  const snap = kmzSnapFind(latlng, null);
  let ll = snap ? snap.latlng : latlng;
  // привязка к собственной «висящей» точке не должна блокировать рисование
  if (snap && K.draw.pending && kmzGeomKey([ll.lat, ll.lng]) === kmzGeomKey(K.draw.pending)){
    ll = latlng;
  }
  const point = [ll.lat, ll.lng];

  if (!K.draw.pending){
    K.draw.pending = point;         // первый клик — начало отрезка
    kmzDrawRender();
    return;
  }

  const [plat, plng] = K.draw.pending;
  if (Math.abs(plat - point[0]) < 1e-9 && Math.abs(plng - point[1]) < 1e-9) return;

  K.draw.segments.push([[plat, plng], point]);
  K.draw.pending = point;           // следующий отрезок продолжается с конца
  kmzDrawRender();
}

function kmzDrawUndoPoint(){
  if (!K.draw) return;
  if (K.draw.segments.length){
    const last = K.draw.segments.pop();
    K.draw.pending = last[0];
  } else {
    K.draw.pending = null;
  }
  kmzDrawRender();
}

async function kmzDrawSave(){
  if (!K.draw) return;
  if (!K.draw.segments.length){
    kmzDrawHint('Нужен хотя бы один отрезок');
    return;
  }
  const name = prompt('Название маршрута:', 'Новый маршрут');
  if (name === null) return;
  const segments = K.draw.segments.map(seg => seg.map(p => [p[0], p[1]]));
  const route = await createRoute({ name: (name || '').trim() || 'Новый маршрут', segments });
  if (!route || route.error){
    alert('Не удалось сохранить маршрут: ' + ((route && route.error) || 'ошибка сети'));
    return;
  }
  kmzDrawStop();
  await kmzLoadRoutes({ fit: false });
}
/* ==========================================================================
   KMZ — МУФТЫ (точки на карте)
   ========================================================================== */
const closuresLayer = L.layerGroup().addTo(map);

function closureIcon(){
  return L.divIcon({
    className: 'closure-marker',
    html: '<div class="closure-dot"></div>',
    iconSize: [16, 16], iconAnchor: [8, 8],
  });
}

function closurePopupHtml(c){
  return `<div class="onu-popup closure-popup">
    <div><b>${esc(c.name)}</b></div>
    <div style="margin-top:6px">
      <button type="button" data-act="rename">✏️ Имя</button>
      <button type="button" data-act="delete">🗑 Удалить</button>
    </div>
  </div>`;
}

function closuresRender(){
  closuresLayer.clearLayers();
  K.closureMarkers.clear();
  for (const c of K.closures){
    if (K.closuresHidden.has(c.id)) continue;
    closuresAddMarker(c);
  }
}

function closureBuildTree(){
  const root = { name: '', children: new Map(), closures: [] };
  const ensure = (parts) => {
    let node = root;
    for (const part of parts){
      if (!part) continue;
      if (!node.children.has(part)){
        node.children.set(part, { name: part, children: new Map(), closures: [] });
      }
      node = node.children.get(part);
    }
    return node;
  };
  for (const c of K.closures){
    ensure((c.folder || 'Муфты').split('/').filter(Boolean)).closures.push(c);
  }
  for (const f of K.closureFolders){
    ensure(f.split('/').filter(Boolean));
  }
  return root;
}

function closureExpandFolder(pathStr){
  const parts = String(pathStr || 'Муфты').split('/').filter(Boolean);
  for (let i = 1; i <= parts.length; i++){
    K.expandedClosureFolders.add(parts.slice(0, i).join('/'));
  }
}

function closureCollectClosures(node){
  const out = [...node.closures];
  for (const child of node.children.values()) out.push(...closureCollectClosures(child));
  return out;
}

function renderClosureTree(){
  const c = $('closureTree');
  if (!c) return;
  c.innerHTML = '';
  kmzRenderClosures(c);
}

function kmzRenderClosures(container){
  if (!K.closures.length && !K.closureFolders.size) return;
  const root = closureBuildTree();
  for (const child of root.children.values()) closureRenderNode(child, container, [], false);
}

function closureRenderNode(node, container, parts, isRoot){
  const path = isRoot ? [] : [...parts, node.name];
  const pathStr = path.join('/');
  let inner = container;
  if (!isRoot && node.name){
    const { row, wrap } = closureMakeFolderRow(node, pathStr);
    container.append(row, wrap);
    inner = wrap;
  }
  for (const child of node.children.values()) closureRenderNode(child, inner, path, false);
  for (const c of node.closures) inner.appendChild(kmzBuildClosureRow(c));
}

function closureMakeFolderRow(node, pathStr){
  const closures = closureCollectClosures(node);
  const total = closures.length;
  const visible = closures.filter(c => !K.closuresHidden.has(c.id)).length;

  const row = document.createElement('div');
  row.className = 'folder-row';
  row.dataset.closureFolder = pathStr;
  row.draggable = true;

  row.addEventListener('dragover', closureFolderDragOver);
  row.addEventListener('dragleave', e => e.currentTarget.classList.remove('drop-target'));
  row.addEventListener('drop', closureFolderDrop);
  row.addEventListener('dragstart', e => {
    K.dragPayload = { type: 'closure-folder', path: pathStr };
    e.dataTransfer.effectAllowed = 'move';
  });
  row.addEventListener('dragend', () => { K.dragPayload = null; });

  const opened = K.expandedClosureFolders.has(pathStr);
  const toggle = document.createElement('span');
  toggle.className = 'folder-toggle';
  toggle.textContent = opened ? '▾' : '▸';
  toggle.onclick = e => {
    e.stopPropagation();
    const ch = row.nextElementSibling;
    if (ch?.classList.contains('folder-children')){
      const collapsed = ch.classList.toggle('collapsed');
      toggle.textContent = collapsed ? '▸' : '▾';
      if (collapsed) K.expandedClosureFolders.delete(pathStr);
      else K.expandedClosureFolders.add(pathStr);
    }
  };

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'folder-cb';
  cb.checked = total > 0 && visible === total;
  cb.indeterminate = visible > 0 && visible < total;
  cb.disabled = total === 0;
  cb.title = `${visible} из ${total} видимо`;
  cb.addEventListener('mousedown', e => { e.stopPropagation(); row.draggable = false; });
  cb.addEventListener('mouseup', e => { e.stopPropagation(); row.draggable = true; });
  cb.onclick = e => {
    e.stopPropagation();
    for (const c of closures){
      if (cb.checked) K.closuresHidden.delete(c.id);
      else K.closuresHidden.add(c.id);
    }
    closuresRender();
    kmzRenderTree();
  };

  const name = document.createElement('span');
  name.className = 'folder-name';
  name.textContent = node.name;
  name.title = `${node.name} — ${visible}/${total}`;
  name.onclick = e => { e.stopPropagation(); toggle.onclick(e); };

  const cnt = document.createElement('span');
  cnt.className = 'folder-count';
  cnt.textContent = total ? `${visible}/${total}` : '';

  const actions = document.createElement('span');
  actions.className = 'folder-actions';
  actions.innerHTML = `
    <button data-act="closure-rename-folder" title="Переименовать">✏️</button>
    <button data-act="closure-new-subfolder" title="Подпапка">📁+</button>
    <button data-act="closure-delete-folder" title="Удалить">🗑</button>`;

  actions.querySelector('[data-act="closure-rename-folder"]').onclick = async e => {
    e.stopPropagation();
    const cur = pathStr.split('/').pop();
    const nn = prompt('Новое имя папки муфт:', cur);
    if (!nn || !nn.trim() || nn.trim() === cur) return;
    const parent = pathStr.split('/').slice(0, -1).join('/');
    const np = parent ? `${parent}/${nn.trim()}` : nn.trim();
    await closureFolderRename(pathStr, np);
  };

  actions.querySelector('[data-act="closure-new-subfolder"]').onclick = async e => {
    e.stopPropagation();
    const sub = prompt('Имя подпапки муфт:');
    if (!sub || !sub.trim()) return;
    try {
      await fetch('/closures/folders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: `${pathStr}/${sub.trim()}` }),
      });
    } catch (err){ console.warn('closure folder add failed', err); }
    closureExpandFolder(`${pathStr}/${sub.trim()}`);
    await closuresLoad();
  };

  actions.querySelector('[data-act="closure-delete-folder"]').onclick = async e => {
    e.stopPropagation();
    if (!confirm(`Удалить папку "${pathStr}"? Муфты переместятся в родительскую.`)) return;
    try {
      await fetch('/closures/folders', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: pathStr }),
      });
    } catch (err){ console.warn('closure folder delete failed', err); }
    await closuresLoad();
  };

  row.append(toggle, cb, name, cnt, actions);

  const wrap = document.createElement('div');
  wrap.className = 'folder-children';
  if (!K.expandedClosureFolders.has(pathStr)) wrap.classList.add('collapsed');

  return { row, wrap };
}

function kmzBuildClosureRow(c){
  const row = document.createElement('div');
  row.className = 'route-row closure-row';
  row.draggable = true;

  row.addEventListener('dragstart', e => {
    K.dragPayload = { type: 'closure', id: c.id };
    e.dataTransfer.effectAllowed = 'move';
    row.classList.add('dragging');
  });
  row.addEventListener('dragend', () => {
    K.dragPayload = null;
    row.classList.remove('dragging');
    document.querySelectorAll('.folder-row.drop-target')
      .forEach(el => el.classList.remove('drop-target'));
  });

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'folder-cb';
  cb.checked = !K.closuresHidden.has(c.id);
  cb.addEventListener('mousedown', e => { e.stopPropagation(); row.draggable = false; });
  cb.addEventListener('mouseup', e => { e.stopPropagation(); row.draggable = true; });
  cb.onclick = e => {
    e.stopPropagation();
    if (cb.checked) K.closuresHidden.delete(c.id);
    else K.closuresHidden.add(c.id);
    closuresRender();
    kmzRenderTree();
  };

  const name = document.createElement('span');
  name.className = 'route-name';
  name.textContent = c.name;
  name.title = `${c.name} · ${Number(c.lat).toFixed(6)}, ${Number(c.lng).toFixed(6)}`;
  name.onclick = () => {
    closureExpandFolder(c.folder);
    renderClosureTree();
    map.setView([c.lat, c.lng], Math.max(map.getZoom(), 17), { animate: true });
    const marker = K.closureMarkers.get(c.id);
    if (marker) marker.openPopup();
  };

  const actions = document.createElement('span');
  actions.className = 'route-actions';
  actions.innerHTML = `<button data-act="rename" title="Переименовать">✏️</button>` +
    `<button data-act="delete" title="Удалить">✕</button>`;

  actions.querySelector('[data-act="rename"]').onclick = async e => {
    e.stopPropagation();
    const nn = prompt('Название муфты:', c.name);
    if (!nn || !nn.trim()) return;
    c.name = nn.trim();
    try {
      await fetch(`/closures/${c.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: c.name }),
      });
    } catch (err){ console.warn('closure rename failed', err); }
    closuresRender();
    kmzRenderTree();
  };

  actions.querySelector('[data-act="delete"]').onclick = async e => {
    e.stopPropagation();
    if (!confirm(`Удалить муфту "${c.name}"?`)) return;
    try { await fetch(`/closures/${c.id}`, { method: 'DELETE' }); }
    catch (err){ console.warn('closure delete failed', err); }
    K.closures = K.closures.filter(x => x.id !== c.id);
    closuresRender();
    kmzRenderTree();
  };

  row.append(cb, name, actions);
  return row;
}

async function closureFolderRename(oldPath, newPath){
  try {
    await fetch('/closures/folders/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old: oldPath, new: newPath }),
    });
  } catch (e){ console.warn('closure folder rename failed', e); }
  await closuresLoad();
}

function closureFolderDragOver(e){
  const payload = K.dragPayload;
  if (!payload) return;
  if (payload.type !== 'closure' && payload.type !== 'closure-folder') return;
  e.preventDefault();
  e.stopPropagation();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drop-target');
}

async function closureFolderDrop(e){
  e.preventDefault();
  e.stopPropagation();
  e.currentTarget.classList.remove('drop-target');
  const payload = K.dragPayload;
  K.dragPayload = null;
  if (!payload) return;

  const target = e.currentTarget.dataset.closureFolder || '';
  if (payload.type === 'closure'){
    const c = K.closures.find(x => x.id === payload.id);
    if (!c) return;
    try {
      await fetch(`/closures/${c.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: target }),
      });
    } catch (err){ console.warn('closure move failed', err); }
    if (target) closureExpandFolder(target);
    await closuresLoad();
  } else if (payload.type === 'closure-folder'){
    const src = payload.path;
    if (!src || src === target || target.startsWith(src + '/')) return;
    const base = src.split('/').pop();
    const np = target ? `${target}/${base}` : base;
    await closureFolderRename(src, np);
  }
}
function closuresAddMarker(c){
  const marker = L.marker([c.lat, c.lng], {
    icon: closureIcon(), draggable: true, zIndexOffset: 500, keyboard: false,
  }).addTo(closuresLayer);

  K.closureMarkers.set(c.id, marker);
  marker.bindTooltip(c.name, { sticky: true });
  marker.bindPopup(closurePopupHtml(c), { closeButton: false, offset: [0, -6] });

  marker.on('dragend', async () => {
    const ll = marker.getLatLng();
    c.lat = ll.lat;
    c.lng = ll.lng;
    try {
      await fetch(`/closures/${c.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: c.lat, lng: c.lng }),
      });
    } catch (e){ console.warn('closure move failed', e); }
    closuresRender();
  });

  marker.on('popupopen', () => {
    const el = marker.getPopup().getElement();
    if (!el) return;
    el.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button');
      if (!btn) return;
      ev.stopPropagation();
      if (btn.dataset.act === 'rename'){
        const nn = prompt('Название муфты:', c.name);
        if (!nn || !nn.trim()) return;
        c.name = nn.trim();
        try {
          await fetch(`/closures/${c.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: c.name }),
          });
        } catch (e){ console.warn('closure rename failed', e); }
        marker.closePopup();
        closuresRender();
        kmzRenderTree();
      } else if (btn.dataset.act === 'delete'){
        if (!confirm(`Удалить муфту "${c.name}"?`)) return;
        try { await fetch(`/closures/${c.id}`, { method: 'DELETE' }); }
        catch (e){ console.warn('closure delete failed', e); }
        K.closures = K.closures.filter(x => x.id !== c.id);
        marker.closePopup();
        closuresRender();
        kmzRenderTree();
      }
    });
  });
}

async function closuresLoad(){
  try {
    const res = await fetch('/closures');
    K.closures = await res.json();
    try {
      const folders = await (await fetch('/closures/folders')).json();
      K.closureFolders = new Set(folders);
    } catch (_){ K.closureFolders = new Set(); }
    closuresRender();
    kmzRenderTree();
  } catch (e){ console.warn('closures load failed', e); }
}

function closureToggle(on){
  K.closureMode = typeof on === 'boolean' ? on : !K.closureMode;
  if (K.closureMode){
    if (K.draw) kmzDrawStop();
    if (K.editGeom) kmzGeomCancel();
  }
  const btn = $('btnAddClosure');
  if (btn){
    btn.classList.toggle('active', K.closureMode);
    btn.title = K.closureMode
      ? 'Кликните по карте, чтобы поставить муфту (Esc — отмена)'
      : 'Поставить муфту на карту';
  }
  map.getContainer().style.cursor = K.closureMode ? 'crosshair' : '';
}

async function closureAddAt(latlng){
  const def = `Муфта ${K.closures.length + 1}`;
  const name = prompt('Название муфты:', def);
  if (name === null) return;

  let res;
  try {
    res = await fetch('/closures', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: (name || '').trim() || def,
        lat: latlng.lat,
        lng: latlng.lng,
      }),
    });
  } catch (e){
    alert('Не удалось добавить муфту: ' + e.message);
    return;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok){
    alert('Не удалось добавить муфту: ' + (data.error || res.status));
    return;
  }
  K.closures.push(data);
  closureExpandFolder(data.folder);
  closuresRender();
  kmzRenderTree();
  closureToggle(false);
}

/* ==========================================================================
   KMZ — РЕДАКТИРОВАНИЕ ГЕОМЕТРИИ (сеть отрезков)
   ========================================================================== */
const GEOM_LINE_STYLE = { color: '#fbbf24', weight: 4, opacity: .95 };

function kmzPointSegmentDistance(lat, lng, a, b){
  // приближение в плоских координатах: достаточно для выбора ближайшего отрезка
  const toRad = Math.PI / 180;
  const kx = Math.cos(lat * toRad);
  const px = lng * kx, py = lat;
  const ax = a[1] * kx, ay = a[0];
  const bx = b[1] * kx, by = b[0];
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function kmzGeomKey(p){
  return `${Number(p[0]).toFixed(7)},${Number(p[1]).toFixed(7)}`;
}

function kmzGeomVertices(segments){
  const found = new Map();
  for (const seg of segments){
    for (const p of seg){
      const key = kmzGeomKey(p);
      if (!found.has(key)) found.set(key, [p[0], p[1]]);
    }
  }
  return [...found.entries()].map(([key, pt]) => ({ key, pt }));
}

function kmzMergeVertexSegments(segments, keep, drop){
  // все концы, совпадающие с drop, переносим в keep; вырожденные отрезки убираем
  const k = p => `${Number(p[0]).toFixed(7)},${Number(p[1]).toFixed(7)}`;
  const dropKey = k(drop);
  const out = [];
  for (const seg of segments){
    const a = k(seg[0]) === dropKey ? [keep[0], keep[1]] : [seg[0][0], seg[0][1]];
    const b = k(seg[1]) === dropKey ? [keep[0], keep[1]] : [seg[1][0], seg[1][1]];
    if (k(a) === k(b)) continue;
    out.push([a, b]);
  }
  return out;
}

function kmzGeomVertexByKey(key){
  const ed = K.editGeom;
  if (!ed) return null;
  for (const seg of ed.segments){
    for (const p of seg){
      if (kmzGeomKey(p) === key) return [p[0], p[1]];
    }
  }
  return null;
}

function kmzGeomMergeVertices(keepKey, dropKey){
  const ed = K.editGeom;
  if (!ed || keepKey === dropKey) return;
  const keep = kmzGeomVertexByKey(keepKey);
  const drop = kmzGeomVertexByKey(dropKey);
  if (!keep || !drop) return;
  kmzGeomPushHistory();
  ed.segments = kmzMergeVertexSegments(ed.segments, keep, drop);
  ed.selected = keepKey;
  ed.pending = null;
  ed.line.setLatLngs(ed.segments);
  kmzGeomRenderMarkers();
  kmzGeomHint();
}

function kmzGeomDegree(segments, key){
  let n = 0;
  for (const seg of segments){
    if (kmzGeomKey(seg[0]) === key) n++;
    if (kmzGeomKey(seg[1]) === key) n++;
  }
  return n;
}

function kmzGeomHint(text){
  const el = $('editHint');
  if (!el) return;
  if (!text){
    const ed = K.editGeom;
    if (!ed) return;
    const km = kmzSegmentsDistanceKm(ed.segments).toFixed(2);
    const route = K.routes.find(x => x.id === ed.id);
    const verts = kmzGeomVertices(ed.segments).length;
    const state = ed.pending ? ' · клик — конец отрезка' : ' · клик — начало отрезка';
    text = `${ed.append ? '➕' : '📐'} ${route ? route.name : 'маршрут'}: отрезков ${ed.segments.length} · вершин ${verts} · ${km} км${state} — ` +
      'клик по вершине — выбрать, Shift+клик — совместить точки, по линии — вставить, правый клик — удалить, Ctrl+Z — шаг назад, Enter — сохранить, Esc — отмена';
  }
  el.textContent = text;
  el.classList.remove('hidden');
}

function kmzGeomIcon(selected){
  return L.divIcon({
    className: 'geom-marker',
    html: `<div class="geom-dot${selected ? ' sel' : ''}"></div>`,
    iconSize: [12, 12], iconAnchor: [6, 6],
  });
}

function kmzGeomRenderMarkers(){
  const ed = K.editGeom;
  if (!ed) return;
  ed.markers.clearLayers();

  for (const { key, pt } of kmzGeomVertices(ed.segments)){
    const marker = L.marker(pt, {
      draggable: true, icon: kmzGeomIcon(ed.selected === key), zIndexOffset: 1000, keyboard: false,
    }).addTo(ed.markers);

    marker.on('click', (e) => {
      L.DomEvent.stopPropagation(e);
      // Shift+клик по второй вершине — слить её с выбранной
      if (e.originalEvent && e.originalEvent.shiftKey && ed.selected && ed.selected !== key){
        kmzGeomMergeVertices(ed.selected, key);
        return;
      }
      if (ed.pending && kmzGeomKey(ed.pending) !== key){
        kmzGeomConnect(ed.pending, pt);
        return;
      }
      ed.selected = key;
      ed.pending = [pt[0], pt[1]];
      kmzGeomRenderMarkers();
      kmzGeomHint();
    });

    marker.on('dragstart', () => { ed.selected = key; kmzGeomPushHistory(); });
    marker.on('drag', () => {
      const ll = marker.getLatLng();
      for (const seg of ed.segments){
        if (kmzGeomKey(seg[0]) === key) seg[0] = [ll.lat, ll.lng];
        if (kmzGeomKey(seg[1]) === key) seg[1] = [ll.lat, ll.lng];
      }
      ed.line.setLatLngs(ed.segments);
      kmzGeomHint();
    });
    marker.on('dragend', () => {
      const ll = marker.getLatLng();
      const snap = kmzSnapFind(ll, ed.id, { includeOwn: true, skipKey: key });
      const final = snap ? [snap.latlng.lat, snap.latlng.lng] : [ll.lat, ll.lng];
      for (const seg of ed.segments){
        if (kmzGeomKey(seg[0]) === key) seg[0] = [final[0], final[1]];
        if (kmzGeomKey(seg[1]) === key) seg[1] = [final[0], final[1]];
      }
      ed.selected = kmzGeomKey(final);
      ed.line.setLatLngs(ed.segments);
      kmzGeomRenderMarkers();
      kmzGeomHint();
    });
    marker.on('contextmenu', (e) => {
      L.DomEvent.stopPropagation(e);
      L.DomEvent.preventDefault(e);
      kmzGeomRemove(key);
    });
  }

  if (ed.pending && !kmzGeomVertices(ed.segments).some(v => v.key === kmzGeomKey(ed.pending))){
    L.circleMarker(ed.pending, DRAFT_DOT_STYLE).addTo(ed.markers);
  }
}

function kmzGeomBegin(routeId, { append = false } = {}){
  const r = K.routes.find(x => x.id === routeId);
  if (!r) return;
  if (K.draw) kmzDrawStop();
  if (K.editGeom) kmzGeomCancel();
  if (K.closureMode) closureToggle(false);
  if (K.linked) kmzClearLink(false);

  const segments = kmzRouteSegments(r).map(seg => seg.map(p => [p[0], p[1]]));
  K.editGeom = {
    id: r.id,
    segments,
    original: segments.map(seg => seg.map(p => [p[0], p[1]])),
    history: [],
    pending: null,
    selected: null,
    append: !!append,
    line: null,
    markers: L.layerGroup().addTo(map),
  };
  K.editGeom.line = L.polyline(K.editGeom.segments, GEOM_LINE_STYLE).addTo(map);
  K.editGeom.line.on('click', (e) => {
    L.DomEvent.stopPropagation(e);
    kmzGeomInsert(e.latlng);
  });
  kmzGeomRenderMarkers();

  if (append && segments.length){
    const leaf = kmzGeomVertices(segments).find(v => kmzGeomDegree(segments, v.key) === 1);
    if (leaf){
      K.editGeom.selected = leaf.key;
      K.editGeom.pending = [leaf.pt[0], leaf.pt[1]];
      map.setView(leaf.pt, Math.max(map.getZoom(), 16), { animate: true });
      kmzGeomRenderMarkers();
    }
  }
  kmzGeomHint();
  kmzApplyFilters();
}

function kmzGeomAppendBegin(routeId){
  kmzGeomBegin(routeId, { append: true });
}

function kmzGeomConnect(a, b){
  const ed = K.editGeom;
  if (!ed) return;
  if (kmzGeomKey(a) === kmzGeomKey(b)) return;
  kmzGeomPushHistory();
  ed.segments.push([[a[0], a[1]], [b[0], b[1]]]);
  ed.pending = [b[0], b[1]];
  ed.selected = kmzGeomKey(b);
  ed.line.setLatLngs(ed.segments);
  kmzGeomRenderMarkers();
  kmzGeomHint();
}

function kmzGeomAddPoint(latlng){
  const ed = K.editGeom;
  if (!ed) return;
  const snap = kmzSnapFind(latlng, ed.id);
  const ll = snap ? snap.latlng : latlng;
  const pt = [ll.lat, ll.lng];

  if (!ed.pending){
    ed.pending = pt;
    ed.selected = kmzGeomKey(pt);
    kmzGeomRenderMarkers();
    kmzGeomHint();
    return;
  }
  kmzGeomConnect(ed.pending, pt);
}

function kmzGeomInsert(latlng){
  const ed = K.editGeom;
  if (!ed) return;
  const snap = kmzSnapFind(latlng, ed.id);
  const pt = snap ? [snap.latlng.lat, snap.latlng.lng] : [latlng.lat, latlng.lng];

  let best = -1, bestD = Infinity;
  for (let i = 0; i < ed.segments.length; i++){
    const [a, b] = ed.segments[i];
    const d = kmzPointSegmentDistance(pt[0], pt[1], a, b);
    if (d < bestD){ bestD = d; best = i; }
  }
  if (best < 0) return;

  kmzGeomPushHistory();
  const [a, b] = ed.segments[best];
  ed.segments.splice(best, 1, [[a[0], a[1]], [pt[0], pt[1]]], [[pt[0], pt[1]], [b[0], b[1]]]);
  ed.selected = kmzGeomKey(pt);
  ed.pending = null;
  ed.line.setLatLngs(ed.segments);
  kmzGeomRenderMarkers();
  kmzGeomHint();
}

function kmzGeomRemove(key){
  const ed = K.editGeom;
  if (!ed) return;
  const incident = [];
  ed.segments.forEach((seg, i) => {
    if (kmzGeomKey(seg[0]) === key || kmzGeomKey(seg[1]) === key) incident.push(i);
  });
  if (!incident.length) return;
  if (incident.length === 1 && ed.segments.length === 1){
    kmzGeomHint('Нельзя удалить единственный отрезок');
    return;
  }
  if (incident.length > 2){
    kmzGeomHint('Вершина соединяет больше двух отрезков — удалить нельзя');
    return;
  }

  kmzGeomPushHistory();
  if (incident.length === 2){
    const [i, j] = incident;
    const segA = ed.segments[i], segB = ed.segments[j];
    const a = kmzGeomKey(segA[0]) === key ? segA[1] : segA[0];
    const b = kmzGeomKey(segB[0]) === key ? segB[1] : segB[0];
    const keep = Math.min(i, j), drop = Math.max(i, j);
    ed.segments[keep] = [[a[0], a[1]], [b[0], b[1]]];
    ed.segments.splice(drop, 1);
  } else {
    ed.segments.splice(incident[0], 1);
  }
  ed.selected = null;
  ed.pending = null;
  ed.line.setLatLngs(ed.segments);
  kmzGeomRenderMarkers();
  kmzGeomHint();
}

function kmzGeomCancel(){
  const ed = K.editGeom;
  if (!ed) return;
  for (const layer of [ed.line, ed.markers]){
    if (layer && map.hasLayer(layer)) map.removeLayer(layer);
  }
  K.editGeom = null;
  kmzSnapHintShow(null);
  kmzApplyFilters();
  const el = $('editHint');
  if (el) el.classList.add('hidden');
}

async function kmzGeomSave(){
  const ed = K.editGeom;
  if (!ed) return;
  if (!ed.segments.length){
    kmzGeomHint('Нужен хотя бы один отрезок');
    return;
  }
  const r = K.routes.find(x => x.id === ed.id);
  if (!r){ kmzGeomCancel(); return; }
  const segments = ed.segments.map(seg => seg.map(p => [p[0], p[1]]));

  try {
    const res = await fetch(`/route/${ed.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segments }),
    });
    if (!res.ok){
      const err = await res.json().catch(() => ({}));
      alert('Не удалось сохранить маршрут: ' + (err.error || res.status));
      return;
    }
  } catch (e){
    alert('Не удалось сохранить маршрут: ' + e.message);
    return;
  }

  K.geomUndoStack.push({ id: ed.id, segments: ed.original.map(seg => seg.map(p => [p[0], p[1]])) });
  if (K.geomUndoStack.length > 20) K.geomUndoStack.shift();
  r.segments = segments;
  r.points = undefined;
  kmzGeomCancel();
  kmzRemoveLayer(r.id);
  kmzAddLayersFor([r]);
  kmzApplyFilters();
}
/* ==========================================================================
   KMZ — ПРИВЯЗКА К ВЕРШИНАМ И СЕГМЕНТАМ
   ========================================================================== */
const SNAP_PIXELS = 12;
let __lastSnapTs = 0;

function kmzClosestOnSegment(px, py, ax, ay, bx, by){
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * dx, cy = ay + t * dy;
  return { x: cx, y: cy, dist: Math.hypot(px - cx, py - cy) };
}

function kmzSnapTargets(excludeRouteId, opts = {}){
  const verts = [], segs = [];
  for (const r of K.routes){
    if (excludeRouteId != null && r.id === excludeRouteId) continue;
    for (const seg of kmzRouteSegments(r)){
      segs.push(seg);
      verts.push(seg[0], seg[1]);
    }
  }
  // при рисовании можно привязаться к уже нарисованным вершинам (ветвление)
  if (K.draw){
    for (const seg of K.draw.segments){ verts.push(seg[0], seg[1]); }
    if (K.draw.pending) verts.push(K.draw.pending);
  }
  // при перетаскивании вершины — привязка к другим вершинам того же маршрута,
  // чтобы две точки можно было точно совместить
  if (K.editGeom && opts.includeOwn){
    for (const seg of K.editGeom.segments){
      for (const p of seg){
        if (opts.skipKey && kmzGeomKey(p) === opts.skipKey) continue;
        verts.push(p);
      }
    }
  }
  return { verts, segs };
}

function kmzSnapFind(latlng, excludeRouteId, opts = {}){
  if (!K.snap) return null;
  const p = map.latLngToContainerPoint(latlng);
  const { verts, segs } = kmzSnapTargets(excludeRouteId, opts);
  let best = null, bestD = SNAP_PIXELS;

  // сначала вершины: привязка к существующей точке важнее, чем к линии
  for (const v of verts){
    const q = map.latLngToContainerPoint(v);
    const d = Math.hypot(p.x - q.x, p.y - q.y);
    if (d < bestD){ bestD = d; best = { latlng: L.latLng(v[0], v[1]), kind: 'vertex' }; }
  }

  for (const [a, b] of segs){
    const qa = map.latLngToContainerPoint(a);
    const qb = map.latLngToContainerPoint(b);
    const c = kmzClosestOnSegment(p.x, p.y, qa.x, qa.y, qb.x, qb.y);
    if (c.dist < bestD){
      bestD = c.dist;
      best = { latlng: map.containerPointToLatLng(L.point(c.x, c.y)), kind: 'segment' };
    }
  }
  return best;
}

function kmzSnapHintShow(snap){
  if (!K.snapHint){
    K.snapHint = L.marker([0, 0], {
      interactive: false, keyboard: false, zIndexOffset: 2000,
      icon: L.divIcon({ className: 'snap-ring', iconSize: [14, 14], iconAnchor: [7, 7], html: '' }),
    });
  }
  if (!snap){
    if (map.hasLayer(K.snapHint)) map.removeLayer(K.snapHint);
    return;
  }
  K.snapHint.setLatLng(snap.latlng);
  if (!map.hasLayer(K.snapHint)) K.snapHint.addTo(map);
}

map.on('mousemove', (e) => {
  if (!K.draw && !K.editGeom) return;
  const now = Date.now();
  if (now - __lastSnapTs < 50) return;
  __lastSnapTs = now;
  kmzSnapHintShow(kmzSnapFind(e.latlng, K.editGeom ? K.editGeom.id : null));
});

function kmzGeomPushHistory(){
  const ed = K.editGeom;
  if (!ed) return;
  if (!ed.history) ed.history = [];
  ed.history.push(ed.segments.map(seg => seg.map(p => [p[0], p[1]])));
  if (ed.history.length > 50) ed.history.shift();
}

function kmzGeomUndo(){
  const ed = K.editGeom;
  if (!ed) return;
  if (!ed.history || !ed.history.length){
    kmzGeomHint('Отменять нечего');
    return;
  }
  ed.segments = ed.history.pop();
  ed.selected = null;
  ed.pending = null;
  ed.line.setLatLngs(ed.segments);
  kmzGeomRenderMarkers();
  kmzGeomHint();
}

async function kmzGeomUndoLastSave(){
  // идём по стеку отмен, пропуская удалённые маршруты
  let u = null, r = null;
  while (K.geomUndoStack.length){
    u = K.geomUndoStack.pop();
    r = K.routes.find(x => x.id === u.id);
    if (r) break;
    u = null;
  }
  if (!u || !r) return;
  try {
    const res = await fetch(`/route/${u.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segments: u.segments }),
    });
    if (!res.ok){
      alert('Не удалось отменить: ' + res.status);
      return;
    }
  } catch (e){
    alert('Не удалось отменить: ' + e.message);
    return;
  }
  r.segments = u.segments.map(seg => seg.map(p => [p[0], p[1]]));
  r.points = undefined;
  kmzRemoveLayer(r.id);
  kmzAddLayersFor([r]);
  kmzApplyFilters();
  kmzGeomHint('↩ Геометрия маршрута восстановлена');
  setTimeout(() => {
    const el = $('editHint');
    if (el && !K.editGeom && !K.draw) el.classList.add('hidden');
  }, 2000);
}

function kmzSnapPaint(){
  const btn = $('btnSnap');
  if (!btn) return;
  btn.classList.toggle('active', !!K.snap);
  btn.title = K.snap
    ? 'Привязка включена: точки липнут к вершинам и линиям (нажмите, чтобы выключить)'
    : 'Привязка выключена (нажмите, чтобы включить)';
}

function kmzSnapToggle(value){
  K.snap = typeof value === 'boolean' ? value : !K.snap;
  if (!K.snap && K.snapHint && map.hasLayer(K.snapHint)) map.removeLayer(K.snapHint);
  try { localStorage.setItem('kmz-snap', K.snap ? '1' : '0'); } catch(_) {}
  kmzSnapPaint();
}

try { K.snap = localStorage.getItem('kmz-snap') !== '0'; } catch(_) {}

/* ==========================================================================
   KMZ ↔ GPON LINK
   ========================================================================== */
function gponBranchMatchesKmz(gponBranch, kmzKey){
  if (!gponBranch || !kmzKey) return false;
  const gb = String(gponBranch).toLowerCase();
  const kk = String(kmzKey).toLowerCase();

  if (gb === kk) return true;
  if (gb.endsWith('/' + kk)) return true;

  const gbParts = gb.split('/');
  const kkParts = kk.split('/');
  if (kkParts.length <= gbParts.length){
    for (let i = 0; i < kkParts.length; i++){
      if (gbParts[gbParts.length - kkParts.length + i] !== kkParts[i]) return false;
    }
    return true;
  }
  return false;
}

function gponApplyKmzLinkFilter(){
  if (!G.markersVisible){
    for (const k in G.branchLayers){
      const c = G.branchLayers[k];
      if (map.hasLayer(c)) map.removeLayer(c);
    }
    return;
  }
  if (!K.linked){
    gponApplyFilters();
    return;
  }

  const key = K.linked.key;
  const activeStatus = G.filters.status.size > 0;
  const wanted = new Map();

  for (const m of G.allMarkers){
    const o = m.__onu;
    if (o.lat == null) continue;
    if (activeStatus && !G.filters.status.has(o._statusKey)) continue;
    if (!gponBranchMatchesKmz(o.branch, key)) continue;
    (wanted.get(o.branch) ?? wanted.set(o.branch, []).get(o.branch)).push(m);
  }

  for (const branch in G.branchLayers){
    const cluster = G.branchLayers[branch];
    const arr = wanted.get(branch) || [];
    const has = arr.length > 0;
    const on = map.hasLayer(cluster);

    if (has && !on && !G.searchMode) cluster.addTo(map);
    if (!has && on) map.removeLayer(cluster);
    if (!has) continue;

    const cur = _clusterMembers.get(branch);
    if (cur && cur.size === arr.length){
      let same = true;
      for (const m of arr){ if (!cur.has(m)){ same = false; break; } }
      if (same) continue;
    }
    cluster.clearLayers();
    cluster.addLayers(arr);
    _clusterMembers.set(branch, new Set(arr));
  }
}

function gponHighlightLinkedBranches(){
  const root = $('filter-branch-list');
  if (!root) return;

  root.querySelectorAll(`.filter-item.${CLS.BRANCH_HIGHLIGHT_LINKED}`)
      .forEach(el => el.classList.remove(CLS.BRANCH_HIGHLIGHT_LINKED));

  if (!K.linked) return;

  const key = K.linked.key;
  for (const el of root.querySelectorAll('.filter-item')){
    const cb = el.querySelector('input[data-filter="branch"]');
    if (cb && gponBranchMatchesKmz(cb.value, key)){
      el.classList.add(CLS.BRANCH_HIGHLIGHT_LINKED);
    }
  }
}

function kmzLinkRoute(routeId){
  const r = K.routes.find(x => x.id === routeId);
  if (!r) return;

  if (K.linked?.id === routeId){ kmzClearLink(); return; }
  if (K.linked) kmzClearLink();

  const key = r.branch || r.name;
  K.linked = { type: 'route', id: r.id, key, folder: r.folder || '' };

  // Изоляция — режим просмотра: r.visible на сервере не трогаем, иначе
  // клик по линии переписывает сохранённые данные всех маршрутов.
  kmzApplyFilters();

  kmzUpdateLinkBadge();
  gponApplyKmzLinkFilter();
  gponHighlightLinkedBranches();

  kmzFitToLinkedRoute();
  setTimeout(() => { if (K.linked?.id === routeId) kmzFitToLinkedRoute(); }, 150);

  kmzCollapseExceptRoute(routeId);
  setTimeout(() => { if (K.linked?.id === routeId) kmzCollapseExceptRoute(routeId); }, 0);

  document.querySelectorAll(
    '#filter-branch-list .branch-group, #filter-branch-list .branch-subgroup, #filter-branch-list .branch-subsub'
  ).forEach(p => {
    p.classList.remove('collapsed');
    const tt = p.querySelector(':scope > .branch-group-head .toggle-g, '
                            + ':scope > .branch-subgroup-head .toggle-s, '
                            + ':scope > .branch-subsub-head .toggle-ss');
    if (tt) tt.textContent = '▾';
    if (p.dataset.key) G.expandedFolders.add(p.dataset.key);
  });
}

function kmzClearLink(fit = true){
  if (!K.linked) return;
  K.linked = null;
  kmzApplyFilters();

  const tree = $('folderTree');
  if (tree){
    tree.querySelectorAll('.route-row.linked').forEach(r => r.classList.remove(CLS.LINKED));
  }

  kmzUpdateLinkBadge();
  gponApplyKmzLinkFilter();
  gponHighlightLinkedBranches();
  if (fit) setTimeout(() => kmzFitAll(), 100);
}

function kmzUpdateLinkBadge(){
  const b = $('linkBadge');
  const t = $('linkBadgeText');
  if (!b || !t) return;
  if (!K.linked){ b.classList.add('hidden'); return; }
  b.classList.remove('hidden');
  t.textContent = `Связка: ${K.linked.key}`;
}

$('linkBadgeClear').onclick = () => kmzClearLink();

/* ==========================================================================
   GPON — PARSERS
   ========================================================================== */
function gponDecodeBytes(buf){
  for (const enc of ['utf-8', 'windows-1251', 'koi8-r', 'iso-8859-5']){
    try {
      const t = new TextDecoder(enc, { fatal: true }).decode(buf);
      if (/[А-Яа-яЁё]/.test(t) && !t.includes('\uFFFD')) return stripBOM(t);
    } catch(_) {}
  }
  return stripBOM(new TextDecoder('windows-1251').decode(buf));
}

function gponDetectDelim(line){
  const c = {
    ';': (line.match(/;/g) || []).length,
    '\t': (line.match(/\t/g) || []).length,
    ',': (line.match(/,/g) || []).length,
  };
  let best = ';', max = 0;
  for (const [d, n] of Object.entries(c)) if (n > max){ max = n; best = d; }
  return best;
}

function gponParseCSV(text, delim){
  const rows = [];
  let i = 0, len = text.length, row = [], field = '', inQ = false;
  while (i < len){
    const ch = text[i];
    if (inQ){
      if (ch === '"'){
        if (text[i + 1] === '"'){ field += '"'; i += 2; continue; }
        inQ = false; i++; continue;
      }
      const next = text.indexOf('"', i);
      if (next < 0){ field += text.slice(i); i = len; break; }
      field += text.slice(i, next); i = next; continue;
    }
    if (ch === '"'){ inQ = true; i++; continue; }
    if (ch === delim){ row.push(field); field = ''; i++; continue; }
    if (ch === '\n' || ch === '\r'){
      row.push(field); rows.push(row); row = []; field = '';
      i += (ch === '\r' && text[i + 1] === '\n') ? 2 : 1;
      continue;
    }
    const nd = text.indexOf(delim, i), nn = text.indexOf('\n', i), nr = text.indexOf('\r', i);
    let next = -1;
    for (const c of [nd, nn, nr]) if (c >= 0 && (next < 0 || c < next)) next = c;
    if (next < 0){ field += text.slice(i); i = len; break; }
    field += text.slice(i, next); i = next;
  }
  if (field.length || row.length){ row.push(field); rows.push(row); }
  return rows;
}

const GPON_RE_ADDR = [
  [/ё/g, 'е'], [/[«»"'`]/g, ''], [/\s+/g, ' '], [/[(){}\[\]]/g, ''],
  [/\bдом\b/g, 'д'], [/\bд\.\s*/g, 'д '], [/\bкв\.?\s*/g, 'кв '],
  [/\bкорп\.?\s*/g, 'корп '], [/\bул\.\s*/g, 'ул '], [/\bпер\.\s*/g, 'пер '],
  [/\bснт\.?\s*/g, 'снт '], [/\bтер\.?\s*/g, 'тер '], [/,\s*,/g, ','],
];
const gponNormAddr = (a) => {
  if (!a) return '';
  let s = String(a).toLowerCase();
  for (const [re, to] of GPON_RE_ADDR) s = s.replace(re, to);
  return s.trim();
};

const gponBranch = (login) => {
  if (!login) return 'Прочее';
  const head = login.split(':')[0];
  const p = head.split('/');
  if (p.length < 5) return head;
  return `${p[0]}/${p[3]}/${p[4]}`;
};

function gponStatusKey(s){
  // Статусы приходят из CSV и могут быть null — приводим к строке
  const t = String(s == null ? '' : s).toLowerCase();
  if (!t) return 'other';
  // Русские формулировки: «Активна», «Отключена», «Заблокирована …»
  if (/активн/.test(t)) return 'active';
  if (/отключ|неактивн|отсутств|нет сигнала/.test(t)) return 'off';
  if (/заблокир|блок|недостат|приостанов|заморож|не ?оплач|долг/.test(t)) return 'block';
  // Английские статусы как запасной вариант
  if (/active|online|up\b|вкл/.test(t)) return 'active';
  if (/off|disabled|down\b|откл/.test(t)) return 'off';
  if (/block|suspend|freeze|неоплач/.test(t)) return 'block';
  return 'other';
}

/* ==========================================================================
   GPON — SAVE / LOAD
   ========================================================================== */
const gponSaveGeo = () =>
  fetch('/gpon/geo', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ geo: Object.fromEntries(G.geo), geoFiles: G.geoFiles }),
  }).catch(e => console.warn('gpon geo save failed', e));

const gponSaveOnus = () =>
  fetch('/gpon/onus', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ onus: G.onus, vgFile: G.vgFile }),
  }).catch(e => console.warn('gpon onus save failed', e));

async function gponLoadState(){
  try {
    const data = await (await fetch('/gpon/state')).json();
    if (!(data.geo && Object.keys(data.geo).length) && !data.onus?.length) return false;

    G.geo = new Map(Object.entries(data.geo || {}));
    G.onus = (data.onus || []).map(o => {
      const g = G.geo.get(gponNormAddr(o.address));
      return { ...o, lat: g?.lat ?? null, lng: g?.lng ?? null, _statusKey: gponStatusKey(o.status || '') };
    });

    G.byBranch = new Map();
    for (const o of G.onus){
      if (!G.byBranch.has(o.branch)) G.byBranch.set(o.branch, []);
      G.byBranch.get(o.branch).push(o);
    }
    G.geoFiles = data.geoFiles || [];
    G.vgFile = data.vgFile || '';

    if (G.geoFiles.length){
      $('fileGeoBtn').classList.add('active');
      $('fileGeoBtn').childNodes[0].nodeValue = `📂 Excel × ${G.geoFiles.length} `;
    }
    if (G.vgFile){
      $('fileVgBtn').classList.add('active');
      $('fileVgBtn').childNodes[0].nodeValue = `📂 ${G.vgFile} `;
    }

    const matched = G.onus.filter(o => o.lat != null).length;
    $('diag').textContent =
      `geo: ${G.geo.size} адресов` +
      (G.geoFiles.length ? ` (файлов: ${G.geoFiles.length})` : '') + '\n' +
      `ONU: ${G.onus.length}, на карте ${matched}` +
      (G.vgFile ? ` (${G.vgFile})` : '');

    if (G.onus.length){
      gponBuildStatusFilter();
      gponBuildBranchFilter();
      gponBuildLayers();
      gponUpdateStats();
    }
    return true;
  } catch(e){
    console.warn('gpon state load failed', e);
    return false;
  }
}

async function gponClearState(){
  if (!confirm('Удалить сохранённые geo и vgroups? Это действие необратимо.')) return;

  for (const k in G.branchLayers){ map.removeLayer(G.branchLayers[k]); delete G.branchLayers[k]; }
  G.allMarkers.length = 0;
  _markersByBranch.clear();
  _clusterMembers.clear();

  G.geo = new Map();
  G.onus = [];
  G.byBranch = new Map();
  G.geoFiles = [];
  G.vgFile = '';
  G.searchMode = false;

  if (G.searchHighlightLayer){ map.removeLayer(G.searchHighlightLayer); G.searchHighlightLayer = null; }
  G.editMode = null;
  gponHideEditHint();

  $('fileGeoBtn').classList.remove('active');
  $('fileGeoBtn').childNodes[0].nodeValue = '📂 Excel (geo*.xlsx) ';
  $('fileVgBtn').classList.remove('active');
  $('fileVgBtn').childNodes[0].nodeValue = '📂 vgroups.csv ';
  $('filter-status').innerHTML = '';
  $('filter-branch-list').innerHTML = '';
  const nl = $('gpon-nogeo-list'); if (nl) nl.innerHTML = '';
  const nc = $('nogeo-count'); if (nc) nc.textContent = '';
  $('diag').textContent = 'Загрузите Excel (geo*.xlsx) и vgroups.csv.';
  gponUpdateStats();

  try { await fetch('/gpon/state', { method: 'DELETE' }); }
  catch(e){ console.warn('gpon state delete failed', e); }
}

$('clear-gpon-data').onclick = gponClearState;

/* ==========================================================================
   GPON — MARKERS + POPUPS
   ========================================================================== */
const GPON_ICON_CACHE = {};
let _markersByBranch = new Map();
let _clusterMembers = new Map();

function gponGetIcon(color){
  if (GPON_ICON_CACHE[color]) return GPON_ICON_CACHE[color];
  return GPON_ICON_CACHE[color] = L.divIcon({
    className: 'onu-marker',
    html: `<div style="background:${color};width:14px;height:14px;border-radius:50%;
      border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.6)"></div>`,
    iconSize: [14, 14], iconAnchor: [7, 7],
  });
}

function gponBindPopup(layer){
  layer.bindPopup(gponBuildPopup(layer.__onu), {
    closeButton: false, offset: [0, -5], autoClose: false, closeOnClick: false, autoPan: false,
  });

  layer.on('mouseover', function(){
    if (layer._editModeActive) return;
    if (__curPopup && __curPopup !== this) __curPopup.closePopup();
    __curPopup = this;
    this.openPopup();
  });

  layer.on('click', function(e){
    L.DomEvent.stopPropagation(e);
    if (layer._editModeActive) return;
    gponHighlightBranchInTree(layer.__onu.branch);
  });

  layer.on('popupopen', () => {
    const el = layer.getPopup().getElement();
    if (!el) return;
    el.addEventListener('mousedown', ev => ev.stopPropagation());
    if (el.__clickBound) return;
    el.__clickBound = true;
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const btn = ev.target.closest('button');
      if (!btn) return;
      const act = btn.dataset.action;
      if (act === ACT.SAVE){ ev.preventDefault(); gponSaveEditedMarker(layer); return; }
      if (act === ACT.CANCEL){ ev.preventDefault(); gponCancelEditMarker(layer); return; }
      if (btn.id?.startsWith('onu-edit-')){
        if (layer._editModeActive) return;
        ev.preventDefault();
        gponStartEditMarker(layer);
      }
    });
  });
}

function gponBuildPopup(o){
  const hasCoords = (o.lat != null && o.lng != null);
  return `<div class="onu-popup">
    <div><b>Ветка:</b> ${esc(o.branch)}</div>
    <div><b>ONU ID:</b> ${esc(o.id)}</div>
    <div><b>Логин:</b> ${esc(o.login)}</div>
    <div><b>Статус:</b> <span class="${GPON_STATUS_CLASS[o._statusKey] || ''}">${esc(o.status)}</span></div>
    <hr style="margin:5px 0;border:none;border-top:1px solid #ddd">
    <div><b>Адрес:</b> ${esc(o.address)}</div>
    <div style="margin-top:6px">
      <button type="button" id="onu-edit-${esc(o.id)}" class="warn">📌 ${hasCoords ? 'Перетащить' : 'Указать место'}</button>
    </div>
  </div>`;
}

function gponBuildLayers(){
  for (const k in G.branchLayers){ map.removeLayer(G.branchLayers[k]); delete G.branchLayers[k]; }
  G.allMarkers.length = 0;
  _markersByBranch = new Map();
  _clusterMembers = new Map();

  G.byBranch.forEach((onus, branch) => {
    const cluster = L.markerClusterGroup({
      maxClusterRadius: 45, disableClusteringAtZoom: 17,
      showCoverageOnHover: false, chunkedLoading: true,
      chunkInterval: 100, chunkDelay: 30,
    });
    const arr = [];
    for (const p of onus){
      if (p.lat == null) continue;
      const m = L.marker([p.lat, p.lng], {
        icon: gponGetIcon(GPON_STATUS_COLOR[p._statusKey]),
        draggable: false,
      });
      m.__onu = p;
      gponBindPopup(m);
      arr.push(m);
      G.allMarkers.push(m);
    }
    cluster.addLayers(arr);
    G.branchLayers[branch] = cluster;
    _markersByBranch.set(branch, arr);
    _clusterMembers.set(branch, new Set(arr));
    if (G.markersVisible) cluster.addTo(map);
  });

  gponApplyKmzLinkFilter();
  if (K.linked && G.markersVisible) setTimeout(() => kmzFitToLinkedRoute(), 150);
  gponRebuildNogeoList();
}

function gponApplyFilters(){
  if (!G.markersVisible){
    for (const k in G.branchLayers){
      const c = G.branchLayers[k];
      if (map.hasLayer(c)) map.removeLayer(c);
    }
    return;
  }

  const { status: fs, branch: fb } = G.filters;
  const activeStatus = fs.size > 0;
  const activeBranch = fb.size > 0;

  const wanted = new Map();
  for (const m of G.allMarkers){
    const o = m.__onu;
    if (o.lat == null) continue;
    if (activeStatus && !fs.has(o._statusKey)) continue;
    if (activeBranch && !fb.has(o.branch)) continue;
    (wanted.get(o.branch) ?? wanted.set(o.branch, []).get(o.branch)).push(m);
  }

  for (const branch in G.branchLayers){
    const cluster = G.branchLayers[branch];
    const arr = wanted.get(branch) || [];
    const has = arr.length > 0;
    const on = map.hasLayer(cluster);

    if (has && !on && !G.searchMode) cluster.addTo(map);
    if (!has && on) map.removeLayer(cluster);
    if (!has) continue;

    const cur = _clusterMembers.get(branch);
    if (cur && cur.size === arr.length){
      let same = true;
      for (const m of arr){ if (!cur.has(m)){ same = false; break; } }
      if (same) continue;
    }
    cluster.clearLayers();
    cluster.addLayers(arr);
    _clusterMembers.set(branch, new Set(arr));
  }
}

/* ==========================================================================
   GPON — EDIT COORDS
   ========================================================================== */
const gponGeoKeyForOnu = (o) => gponNormAddr(o.address);

function gponStartEditMarker(marker){
  const o = marker.__onu;

  G.editMode = {
    type: 'move', onuId: o.id, marker,
    originalLat: o.lat, originalLng: o.lng,
    originalGeoKey: gponGeoKeyForOnu(o),
  };

  marker.dragging?.enable();
  marker.setZIndexOffset(9999);
  marker.getElement()?.classList.add(CLS.EDITABLE);
  marker._editModeActive = true;
  marker.closePopup();

  const curLat = o.lat != null ? o.lat.toFixed(7) : '';
  const curLng = o.lng != null ? o.lng.toFixed(7) : '';

  marker.setPopupContent(`
    <div class="onu-popup onu-edit-popup">
      <div><b>Ветка:</b> ${esc(o.branch)}</div>
      <div><b>ONU ID:</b> ${esc(o.id)}</div>
      <div><b>Логин:</b> ${esc(o.login)}</div>
      <div style="margin-top:6px;display:grid;grid-template-columns:1fr 1fr;gap:6px">
        <label>Широта
          <input type="text" id="onu-lat-${esc(o.id)}" value="${curLat}" inputmode="decimal" autocomplete="off" />
        </label>
        <label>Долгота
          <input type="text" id="onu-lng-${esc(o.id)}" value="${curLng}" inputmode="decimal" autocomplete="off" />
        </label>
      </div>
      <div style="margin-top:8px">
        <button type="button" class="primary" data-action="${ACT.SAVE}">✓ Сохранить</button>
        <button type="button" data-action="${ACT.CANCEL}">↺ Отменить</button>
      </div>
      <div style="color:var(--muted);font-size:11px;margin-top:4px">
        Введите координаты или перетащите метку.
      </div>
    </div>
  `);

  marker.openPopup();

  setTimeout(() => {
    const latEl = document.getElementById(`onu-lat-${o.id}`);
    const lngEl = document.getElementById(`onu-lng-${o.id}`);
    if (!latEl || !lngEl) return;

    const sync = () => {
      const ll = marker.getLatLng();
      latEl.value = ll.lat.toFixed(7);
      lngEl.value = ll.lng.toFixed(7);
    };
    marker.on('drag', sync);
    marker.on('dragend', () => {
      sync();
      if (!marker.isPopupOpen()) marker.openPopup();
    });
  }, 0);

  gponShowEditHint('Введите координаты вручную или перетащите метку, затем «✓ Сохранить».');
}

async function gponSaveEditedMarker(marker){
  const o = marker.__onu;
  const latEl = document.getElementById(`onu-lat-${o.id}`);
  const lngEl = document.getElementById(`onu-lng-${o.id}`);

  let newLat, newLng, manual = false;

  if (latEl && lngEl){
    const latV = String(latEl.value).trim().replace(',', '.');
    const lngV = String(lngEl.value).trim().replace(',', '.');
    if (latV && lngV){
      const lat = parseFloat(latV);
      const lng = parseFloat(lngV);
      if (!isFinite(lat) || !isFinite(lng) || Math.abs(lat) > 90 || Math.abs(lng) > 180){
        alert('Некорректные координаты. Широта: -90..90, долгота: -180..180.');
        return;
      }
      newLat = lat; newLng = lng; manual = true;
    }
  }
  if (!manual){
    const ll = marker.getLatLng();
    newLat = ll.lat; newLng = ll.lng;
  }

  o.lat = newLat;
  o.lng = newLng;
  marker.setLatLng([newLat, newLng]);

  const key = gponGeoKeyForOnu(o);
  G.geo.set(key, { lat: newLat, lng: newLng, address: o.address });

  try {
    const res = await fetch('/gpon/geo/point', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ addr: key, lat: newLat, lng: newLng, address: o.address }),
    });
    if (!res.ok) console.warn('server returned', res.status);
  } catch(e){ console.warn('save geo point failed', e); }

  marker.dragging?.disable();
  marker.setZIndexOffset(0);
  marker._editModeActive = false;
  marker.off('drag');
  marker.off('dragend');
  marker.getElement()?.classList.remove(CLS.EDITABLE);
  G.editMode = null;
  gponHideEditHint();

  marker.closePopup();
  gponBindPopup(marker);
  gponUpdateStats();
}

function gponCancelEditMarker(marker){
  if (!G.editMode) return;
  const o = marker.__onu;
  const { originalLat, originalLng } = G.editMode;

  o.lat = originalLat; o.lng = originalLng;
  marker.setLatLng([originalLat, originalLng]);
  marker.dragging?.disable();
  marker.setZIndexOffset(0);
  marker._editModeActive = false;
  marker.off('drag');
  marker.off('dragend');
  marker.getElement()?.classList.remove(CLS.EDITABLE);
  G.editMode = null;
  gponHideEditHint();

  marker.closePopup();
  gponBindPopup(marker);
}

const gponShowEditHint = (text) => { const el = $('editHint'); if (el){ el.textContent = text; el.classList.remove('hidden'); } };
const gponHideEditHint = () => { $('editHint')?.classList.add('hidden'); };

/* ---------- Установка координат с нуля ---------- */
function gponStartSetCoord(onuId){
  const o = G.onus.find(x => x.id === onuId);
  if (!o) return;
  G.editMode = { type: 'set', onuId };
  gponShowEditHint('Кликните по карте, чтобы указать координаты для ONU ' + (o.login || o.id));
}

function gponHandleMapClickForSet(latlng){
  if (!G.editMode || G.editMode.type !== 'set') return;
  const o = G.onus.find(x => x.id === G.editMode.onuId);
  if (!o) return;

  o.lat = latlng.lat;
  o.lng = latlng.lng;
  const key = gponGeoKeyForOnu(o);
  G.geo.set(key, { lat: latlng.lat, lng: latlng.lng, address: o.address });

  fetch('/gpon/geo/point', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ addr: key, lat: latlng.lat, lng: latlng.lng, address: o.address }),
  }).catch(e => console.warn('save geo point failed', e));

  G.editMode = null;
  gponHideEditHint();
  gponBuildLayers();
  gponUpdateStats();
}

/* ---------- «Без координат» ---------- */
function gponRebuildNogeoList(){
  const el = $('gpon-nogeo-list');
  const cnt = $('nogeo-count');
  if (!el) return;

  const noGeo = G.onus.filter(o => o.lat == null || o.lng == null);
  if (cnt) cnt.textContent = noGeo.length ? `(${noGeo.length})` : '';

  if (!noGeo.length){
    el.innerHTML = '<div style="padding:6px 8px;color:var(--muted);font-size:12px">Все ONU имеют координаты 🎉</div>';
    return;
  }

  const shown = noGeo.slice(0, 200);
  const frag = document.createDocumentFragment();
  for (const o of shown){
    const row = document.createElement('div');
    row.className = 'gpon-nogeo-item';
    row.innerHTML = `
      <span class="addr" title="${esc(o.address)} — ${esc(o.login)}">${esc(o.address)}</span>
      <button data-onu="${esc(o.id)}">📍</button>`;
    row.querySelector('button').onclick = () => gponStartSetCoord(o.id);
    frag.appendChild(row);
  }
  if (noGeo.length > 200){
    const more = document.createElement('div');
    more.className = 'gpon-nogeo-item';
    more.innerHTML = `<span class="addr" style="color:var(--muted)">… и ещё ${noGeo.length - 200}</span>`;
    frag.appendChild(more);
  }
  el.replaceChildren(frag);
}

/* ==========================================================================
   GPON — HIGHLIGHT BRANCH IN TREE
   ========================================================================== */
function gponClearBranchHighlight(){
  $('filter-branch-list')?.querySelectorAll(`.filter-item.${CLS.BRANCH_HIGHLIGHT}`)
    .forEach(el => el.classList.remove(CLS.BRANCH_HIGHLIGHT));
}

function gponHighlightBranchInTree(branch){
  const root = $('filter-branch-list');
  if (!root) return;
  gponClearBranchHighlight();
  if (!branch) return;

  let target = null;
  for (const el of root.querySelectorAll('.filter-item')){
    if (el.querySelector('input[data-filter="branch"]')?.value === branch){ target = el; break; }
  }
  if (!target) return;

  target.classList.add(CLS.BRANCH_HIGHLIGHT);

  let p = target.parentElement;
  while (p && p !== root){
    p.classList.remove('collapsed');
    const tt = p.querySelector(':scope > .branch-group-head .toggle-g, '
                            + ':scope > .branch-subgroup-head .toggle-s, '
                            + ':scope > .branch-subsub-head .toggle-ss');
    if (tt) tt.textContent = '▾';
    if (p.dataset.key) G.expandedFolders.add(p.dataset.key);
    p = p.parentElement;
  }
  try { target.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch(_) {}
}

/* ==========================================================================
   GPON — PARSERS (WORKBOOK + CSV)
   ========================================================================== */
function gponParseGeoWorkbook(buf, fname, out){
  const wb = XLSX.read(buf, { type: 'array' });
  const rows = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { header: 1, raw: true });

  let h = -1;
  for (let i = 0; i < Math.min(rows.length, 30); i++){
    if (rows[i]?.some(c => String(c).trim() === 'Широта')){ h = i; break; }
  }
  if (h < 0) throw new Error(`«${fname}»: нет столбца «Широта»`);

  const header = rows[h];
  const cLat = header.findIndex(x => String(x).trim() === 'Широта');
  const cLng = header.findIndex(x => String(x).trim() === 'Долгота');
  const cAddr = header.findIndex(x => String(x).trim() === 'Описание');
  if (cLat < 0 || cLng < 0) throw new Error('Нет «Широта»/«Долгота»');
  if (cAddr < 0) throw new Error('Нет «Описание»');

  let added = 0, skipped = 0;
  for (let i = h + 1; i < rows.length; i++){
    const r = rows[i];
    if (!r) continue;
    const lat = parseFloat(String(r[cLat]).replace(',', '.'));
    const lng = parseFloat(String(r[cLng]).replace(',', '.'));
    const addr = r[cAddr];
    if (!addr || isNaN(lat) || isNaN(lng)){ skipped++; continue; }
    out.set(gponNormAddr(addr), { lat, lng, address: addr });
    added++;
  }
  return { added, skipped };
}

function gponParseVgroups(text){
  const first = text.split(/\r?\n/)[0] || '';
  const delim = gponDetectDelim(first);
  const rows = gponParseCSV(text, delim);
  if (!rows.length) return { matched: 0, unmatched: 0, total: 0 };

  const header = rows[0].map(h => (h || '').replace(/^"|"$/g, '').trim());
  let iLogin = header.indexOf('Логин');
  if (iLogin < 0){
    const probe = rows[1] || [];
    for (let i = 0; i < probe.length; i++){
      if ((probe[i] || '').includes('/')){ iLogin = i; break; }
    }
  }
  if (iLogin < 0) iLogin = 1;
  const iID = header.indexOf('ID') >= 0 ? header.indexOf('ID') : 0;
  const iAddr = header.indexOf('Адрес') >= 0 ? header.indexOf('Адрес') : 8;
  const iStat = header.findIndex(h => h.startsWith('Блокировка'));

  const onus = [], byBranch = new Map();
  let matched = 0, unmatched = 0;

  for (let i = 1; i < rows.length; i++){
    const r = rows[i];
    if (!r) continue;
    const login = (r[iLogin] || '').trim();
    const addr = (r[iAddr] || '').trim();
    if (!login || !addr || login === 'null' || addr === 'null') continue;

    const g = G.geo.get(gponNormAddr(addr));
    const status = iStat >= 0 ? (r[iStat] || '').trim() : '';
    const o = {
      id: (r[iID] || '').trim(),
      login, address: addr, status,
      branch: gponBranch(login),
      lat: g?.lat ?? null,
      lng: g?.lng ?? null,
      _statusKey: gponStatusKey(status),
    };
    onus.push(o);
    if (!byBranch.has(o.branch)) byBranch.set(o.branch, []);
    byBranch.get(o.branch).push(o);
    if (g) matched++; else unmatched++;
  }
  G.onus = onus;
  G.byBranch = byBranch;
  return { matched, unmatched, total: onus.length };
}

/* ==========================================================================
   GPON — STATS + FILTERS
   ========================================================================== */
function gponUpdateStats(){
  let matched = 0;
  for (const o of G.onus) if (o.lat != null) matched++;
  $('s-branches').textContent = G.byBranch.size;
  $('s-onus').textContent = G.onus.length;
  $('s-matched').textContent = matched;
}

function gponBuildStatusFilter(){
  const items = [
    { key: 'active', label: '🟢 Активна', cls: 'stat-active' },
    { key: 'off', label: '⚪ Отключена', cls: 'stat-off' },
    { key: 'block', label: '🔴 Заблокирована', cls: 'stat-block' },
    { key: 'other', label: '⚪ Другое' },
  ];
  const frag = document.createDocumentFragment();
  for (const s of items){
    const l = document.createElement('label');
    l.className = 'filter-item';
    l.innerHTML = `<input type="checkbox" data-filter="status" value="${s.key}">
      <span class="${s.cls || ''}">${s.label}</span>`;
    frag.appendChild(l);
  }
  $('filter-status').replaceChildren(frag);
}

function gponBuildBranchFilter(){
  const el = $('filter-branch-list');

  const tree = new Map();
  for (const [branch, onus] of G.byBranch){
    const g = gponBranchGroup(branch);
    const s = gponBranchSubgroup(branch);
    const ss = gponBranchSubsub(branch);
    if (!tree.has(g)) tree.set(g, new Map());
    const sg = tree.get(g);
    if (!sg.has(s)) sg.set(s, new Map());
    const ssg = sg.get(s);
    if (!ssg.has(ss)) ssg.set(ss, []);
    ssg.get(ss).push({ branch, cnt: onus.length });
  }

  const RANK = { 'slav*': 0, 'bdcom-*': 1, '— Прочие': 2 };
  const sorted = [...tree.keys()].sort((a, b) =>
    (RANK[a] ?? 3) - (RANK[b] ?? 3) || a.localeCompare(b, 'ru'));

  const frag = document.createDocumentFragment();
  for (const gname of sorted) frag.appendChild(renderBranchGroup(gname, tree.get(gname)));
  el.replaceChildren(frag);

  if (!el.__bound){
    el.__bound = true;
    el.addEventListener('click', gponFilterClick);
    el.addEventListener('change', gponFilterChange);
  }

  gponSyncChecks();
  gponHighlightLinkedBranches();
}

function renderBranchGroup(gname, subs){
  let gB = 0, gO = 0;
  subs.forEach(ssMap => ssMap.forEach(arr => {
    gB += arr.length;
    arr.forEach(({ cnt }) => gO += cnt);
  }));

  const gEl = document.createElement('div');
  gEl.className = 'branch-group';
  gEl.dataset.key = 'g:' + gname;
  const gOpen = G.expandedFolders.has(gEl.dataset.key);
  if (!gOpen) gEl.classList.add('collapsed');
  gEl.innerHTML = `<label class="branch-group-head">
    <input type="checkbox" class="group-check" data-group="${esc(gname)}">
    <span class="toggle-g">${gOpen ? '▾' : '▸'}</span>
    <span class="g-name">${esc(gname)}</span>
    <span class="g-cnt">${gB} / ${gO}</span></label>`;
  const gList = document.createElement('div');
  gList.className = 'branch-group-list';

  const sortedSubs = [...subs.keys()].sort((a, b) => a.localeCompare(b, 'ru'));
  for (const sname of sortedSubs){
    gList.appendChild(renderBranchSubgroup(sname, subs.get(sname), gname));
  }
  gEl.appendChild(gList);
  return gEl;
}

function renderBranchSubgroup(sname, ssMap, gname){
  let sB = 0, sO = 0;
  ssMap.forEach(arr => {
    sB += arr.length;
    arr.forEach(({ cnt }) => sO += cnt);
  });

  const sEl = document.createElement('div');
  sEl.className = 'branch-subgroup';
  sEl.dataset.key = `s:${gname}/${sname}`;
  const sOpen = G.expandedFolders.has(sEl.dataset.key);
  if (!sOpen) sEl.classList.add('collapsed');
  sEl.innerHTML = `<label class="branch-subgroup-head">
    <input type="checkbox" class="subgroup-check" data-subgroup="${esc(sname)}">
    <span class="toggle-s">${sOpen ? '▾' : '▸'}</span>
    <span class="s-name">${esc(sname)}</span>
    <span class="s-cnt">${sB} / ${sO}</span></label>`;
  const sList = document.createElement('div');
  sList.className = 'branch-subgroup-list';

  const sortedSS = [...ssMap.keys()].sort((a, b) => a.localeCompare(b, 'ru'));
  for (const ssname of sortedSS){
    if (ssname === ''){
      for (const { branch, cnt } of ssMap.get(ssname)) sList.appendChild(renderBranchLeaf(branch, cnt));
      continue;
    }
    sList.appendChild(renderBranchSubsub(ssname, ssMap.get(ssname), sEl.dataset.key));
  }
  sEl.appendChild(sList);
  return sEl;
}

function renderBranchSubsub(ssname, items, sKey){
  let ssO = 0;
  for (const { cnt } of items) ssO += cnt;

  const ss = document.createElement('div');
  ss.className = 'branch-subsub';
  ss.dataset.key = `ss:${sKey}/${ssname}`;
  const ssOpen = G.expandedFolders.has(ss.dataset.key);
  if (!ssOpen) ss.classList.add('collapsed');
  ss.innerHTML = `<label class="branch-subsub-head">
    <input type="checkbox" class="subsub-check" data-subsub="${esc(ssname)}">
    <span class="toggle-ss">${ssOpen ? '▾' : '▸'}</span>
    <span class="ss-name">${esc(ssname)}</span>
    <span class="ss-cnt">${items.length} / ${ssO}</span></label>`;
  const ssList = document.createElement('div');
  ssList.className = 'branch-subsub-list';
  for (const { branch, cnt } of items) ssList.appendChild(renderBranchLeaf(branch, cnt));
  ss.appendChild(ssList);
  return ss;
}

function renderBranchLeaf(branch, cnt){
  const l = document.createElement('label');
  l.className = 'filter-item';
  l.dataset.branch = branch.toLowerCase();
  l.innerHTML = `<input type="checkbox" data-filter="branch" value="${esc(branch)}">
    <span class="name">${esc(branch)}</span><span class="cnt">${cnt}</span>`;
  return l;
}

const gponBranchGroup = (b) => {
  if (!b || b.charCodeAt(0) === 45) return '— Прочие';
  const l = b.toLowerCase();
  if (l.startsWith('bdcom')) return 'bdcom-*';
  if (l.startsWith('slav')) return 'slav*';
  return '— Прочие';
};
const gponBranchSubgroup = (b) => {
  if (!b) return '—';
  const i = b.indexOf('/');
  return i <= 0 ? b : b.slice(0, i);
};
const gponBranchSubsub = (b) => {
  if (!b) return '';
  const f = b.indexOf('/'), l = b.lastIndexOf('/');
  return (f <= 0 || l <= f) ? '' : b.slice(0, l);
};

function gponFilterClick(e){
  const head = e.target.closest('.branch-group-head, .branch-subgroup-head, .branch-subsub-head');
  if (!head) return;
  const t = e.target;
  if (!t.classList.contains('toggle-g') && !t.classList.contains('toggle-s') && !t.classList.contains('toggle-ss')) return;
  e.preventDefault(); e.stopPropagation();
  const block = head.parentElement;
  block.classList.toggle('collapsed');
  const collapsed = block.classList.contains('collapsed');
  const tt = head.querySelector('.toggle-g, .toggle-s, .toggle-ss');
  if (tt) tt.textContent = collapsed ? '▸' : '▾';
  if (block.dataset.key){
    if (collapsed) G.expandedFolders.delete(block.dataset.key);
    else G.expandedFolders.add(block.dataset.key);
  }
}

function gponFilterChange(e){
  const t = e.target;
  if (!(t instanceof HTMLInputElement)) return;

  gponClearBranchHighlight();

  if (t.matches('input[data-filter="branch"]')){
    const set = G.filters.branch;
    t.checked ? set.add(t.value) : set.delete(t.value);
    gponApplyKmzLinkFilter(); gponSyncChecks();
    return;
  }
  if (t.matches('input[data-filter="status"]')){
    const set = G.filters.status;
    t.checked ? set.add(t.value) : set.delete(t.value);
    gponApplyKmzLinkFilter();
    return;
  }
  const box = t.closest('.group-check, .subgroup-check, .subsub-check');
  if (!box) return;
  const container = box.closest('.branch-group, .branch-subgroup, .branch-subsub');
  if (!container) return;
  const set = G.filters.branch;
  const checks = container.querySelectorAll('input[data-filter="branch"]');
  for (const c of checks){
    c.checked = box.checked;
    box.checked ? set.add(c.value) : set.delete(c.value);
  }
  gponApplyKmzLinkFilter(); gponSyncChecks();
}

function gponSyncChecks(){
  const root = $('filter-branch-list');
  const syncPair = (box, checks) => {
    if (!box) return;
    let c = 0;
    for (const x of checks) if (x.checked) c++;
    box.checked = c === checks.length && checks.length > 0;
    box.indeterminate = c > 0 && c < checks.length;
  };
  for (const group of root.querySelectorAll('.branch-group')){
    syncPair(group.querySelector('.group-check'), group.querySelectorAll('input[data-filter="branch"]'));
    for (const sub of group.querySelectorAll('.branch-subgroup')){
      syncPair(sub.querySelector('.subgroup-check'), sub.querySelectorAll('input[data-filter="branch"]'));
      for (const ss of sub.querySelectorAll('.branch-subsub')){
        syncPair(ss.querySelector('.subsub-check'), ss.querySelectorAll('input[data-filter="branch"]'));
      }
    }
  }
}

/* ==========================================================================
   GPON — SEARCH
   ========================================================================== */
function gponSearch(q){
  const root = $('filter-branch-list');
  for (const group of root.querySelectorAll('.branch-group')){
    let any = false;
    for (const sub of group.querySelectorAll('.branch-subgroup')){
      let subAny = false;
      for (const ss of sub.querySelectorAll('.branch-subsub')){
        let ssAny = false;
        for (const el of ss.querySelectorAll('.filter-item')){
          const show = !q || el.dataset.branch.includes(q);
          el.style.display = show ? '' : 'none';
          if (show) any = subAny = ssAny = true;
        }
        ss.style.display = ssAny ? '' : 'none';
      }
      for (const el of sub.querySelectorAll(':scope > .branch-subgroup-list > .filter-item')){
        const show = !q || el.dataset.branch.includes(q);
        el.style.display = show ? '' : 'none';
        if (show) any = subAny = true;
      }
      sub.style.display = subAny ? '' : 'none';
    }
    group.style.display = any ? '' : 'none';
  }

  if (q){
    if (!G.expandedBeforeSearch){
      G.expandedBeforeSearch = new Set(G.expandedFolders);
    }
    root.querySelectorAll('.branch-group, .branch-subgroup, .branch-subsub').forEach(p => {
      p.classList.remove('collapsed');
      const tt = p.querySelector('.toggle-g, .toggle-s, .toggle-ss');
      if (tt) tt.textContent = '▾';
    });
  } else if (G.expandedBeforeSearch){
    G.expandedFolders = G.expandedBeforeSearch;
    G.expandedBeforeSearch = null;
    root.querySelectorAll('.branch-group, .branch-subgroup, .branch-subsub').forEach(p => {
      const collapsed = p.dataset.key ? !G.expandedFolders.has(p.dataset.key) : false;
      p.classList.toggle('collapsed', collapsed);
      const tt = p.querySelector('.toggle-g, .toggle-s, .toggle-ss');
      if (tt) tt.textContent = collapsed ? '▸' : '▾';
    });
  }

  if (G.searchHighlightLayer){ map.removeLayer(G.searchHighlightLayer); G.searchHighlightLayer = null; }

  if (!q){
    G.searchMode = false;
    gponApplyKmzLinkFilter();
    $('s-found').textContent = '—';
    return;
  }

  if (!G.markersVisible){
    $('s-found').textContent = '—';
    return;
  }

  const hits = G.onus.filter(o => o.lat != null &&
    (o.id + ' ' + o.login + ' ' + o.address + ' ' + o.status + ' ' + o.branch).toLowerCase().includes(q));

  G.searchMode = true;
  for (const k in G.branchLayers) if (map.hasLayer(G.branchLayers[k])) map.removeLayer(G.branchLayers[k]);

  const layer = L.layerGroup();
  for (const o of hits){
    const c = L.circleMarker([o.lat, o.lng], {
      radius: 14, color: '#38bdf8', weight: 3, fillColor: '#38bdf8', fillOpacity: .15,
    });
    c.__onu = o;
    gponBindPopup(c);
    c.addTo(layer);
  }
  layer.addTo(map);
  G.searchHighlightLayer = layer;
  if (hits.length) map.setView([hits[0].lat, hits[0].lng], Math.max(map.getZoom(), 15));
  $('s-found').textContent = hits.length;
}

/* ==========================================================================
   GPON — SHOW / HIDE MARKERS
   ========================================================================== */
function gponSetMarkersVisible(visible){
  G.markersVisible = !!visible;

  if (G.markersVisible){
    const q = ($('search') && $('search').value ? $('search').value : '').trim().toLowerCase();
    const active = document.querySelector('.mode-switch button.active');
    const inGpon = active && active.dataset.mode === 'gpon';
    if (inGpon && q){
      gponSearch(q);            // восстановить подсветку поиска
    } else if (K.linked){
      gponApplyKmzLinkFilter();
    } else {
      gponApplyFilters();
    }
  } else {
    for (const k in G.branchLayers){
      const c = G.branchLayers[k];
      if (map.hasLayer(c)) map.removeLayer(c);
    }
    if (G.searchHighlightLayer){
      map.removeLayer(G.searchHighlightLayer);
      G.searchHighlightLayer = null;
    }
    G.searchMode = false;       // иначе после включения слои не вернутся
  }

  const btn = $('btnMarkersVis');
  if (btn){
    btn.textContent = G.markersVisible ? '👁️' : '🙈';
    btn.title = G.markersVisible ? 'Скрыть GPON-метки' : 'Показать GPON-метки';
  }

  try { localStorage.setItem('gpon-markers-visible', G.markersVisible ? '1' : '0'); } catch(_) {}
}

/* ==========================================================================
   MAP — click handler
   ========================================================================== */
map.on('click', (e) => {
  if (K.closureMode){
    L.DomEvent.stopPropagation(e);
    closureAddAt(e.latlng);
    return;
  }
  if (K.draw){
    kmzDrawAddPoint(e.latlng);
    return;
  }
  if (K.editGeom){
    kmzGeomAddPoint(e.latlng);
    return;
  }
  if (G.editMode?.type === 'set'){
    L.DomEvent.stopPropagation(e);
    gponHandleMapClickForSet(e.latlng);
    return;
  }
  if (__curPopup){ __curPopup.closePopup(); __curPopup = null; }

  const stopped = e.originalEvent?._stopped;
  if (stopped) return;

  if (K.linked) kmzClearLink();
  gponClearBranchHighlight();
});

function mapMenuClose(){
  const el = $('mapMenu');
  if (el) el.classList.add('hidden');
}

function mapMenuShow(latlng, containerPoint, items){
  const el = $('mapMenu');
  if (!el) return;
  el.innerHTML = '';
  for (const item of items){
    if (item === '-'){
      const sep = document.createElement('div');
      sep.className = 'menu-sep';
      el.appendChild(sep);
      continue;
    }
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'menu-item';
    b.textContent = item.label;
    b.onclick = ev => { ev.stopPropagation(); mapMenuClose(); item.action(); };
    el.appendChild(b);
  }
  el.classList.remove('hidden');

  const rect = map.getContainer().getBoundingClientRect();
  el.style.left = (rect.left + containerPoint.x) + 'px';
  el.style.top = (rect.top + containerPoint.y) + 'px';
  requestAnimationFrame(() => {
    const r = el.getBoundingClientRect();
    const maxX = window.innerWidth - r.width - 8;
    const maxY = window.innerHeight - r.height - 8;
    if (r.left > maxX) el.style.left = Math.max(8, maxX) + 'px';
    if (r.top > maxY) el.style.top = Math.max(8, maxY) + 'px';
  });
}

function kmzFindNearestRoute(latlng, maxPx = 24){
  const p = map.latLngToContainerPoint(latlng);
  let best = null, bestD = maxPx;
  for (const r of K.routes){
    for (const seg of kmzRouteSegments(r)){
      const a = map.latLngToContainerPoint(seg[0]);
      const b = map.latLngToContainerPoint(seg[1]);
      const dx = b.x - a.x, dy = b.y - a.y;
      const len2 = dx * dx + dy * dy;
      let t = len2 ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2 : 0;
      t = Math.max(0, Math.min(1, t));
      const d = Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
      if (d < bestD){ bestD = d; best = r; }
    }
  }
  return best;
}

map.on('click', () => mapMenuClose());
map.on('zoomstart movestart', () => mapMenuClose());
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') mapMenuClose();
});

map.on('contextmenu', (e) => {
  L.DomEvent.preventDefault(e);
  L.DomEvent.stopPropagation(e);

  const items = [];
  if (K.editGeom){
    items.push({ label: '➕ Добавить точку здесь', action: () => kmzGeomAddPoint(e.latlng) });
    items.push({ label: '↩ Отменить шаг', action: () => kmzGeomUndo() });
    items.push('-');
  }
  if (K.draw){
    items.push({ label: '↩ Убрать последний отрезок', action: () => kmzDrawUndoPoint() });
    items.push('-');
  }
  items.push({ label: '📍 Поставить муфту здесь', action: () => closureAddAt(e.latlng) });
  items.push({ label: '✏️ Начать маршрут здесь', action: () => {
    kmzDrawStart();
    kmzDrawAddPoint(e.latlng);
  } });
  const near = kmzFindNearestRoute(e.latlng);
  if (near){
    items.push({ label: `📐 Редактировать «${near.name}»`, action: () => kmzGeomBegin(near.id) });
  }
  mapMenuShow(e.latlng, e.containerPoint, items);
});

document.addEventListener('keydown', (e) => {
  if (!K.draw) return;
  const tag = (e.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z'){ e.preventDefault(); kmzDrawUndoPoint(); }
  else if (e.key === 'Escape'){
    e.preventDefault();
    if (K.draw.pending){ K.draw.pending = null; kmzDrawRender(); }
    else kmzDrawStop();
  }
  else if (e.key === 'Enter'){ e.preventDefault(); kmzDrawSave(); }
  else if (e.key === 'Backspace'){ e.preventDefault(); kmzDrawUndoPoint(); }
});

document.addEventListener('keydown', (e) => {
  if (!K.editGeom) return;
  const tag = (e.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z'){ e.preventDefault(); kmzGeomUndo(); }
  else if (e.key === 'Escape'){
    e.preventDefault();
    if (K.editGeom.pending){ K.editGeom.pending = null; kmzGeomRenderMarkers(); kmzGeomHint(); }
    else kmzGeomCancel();
  }
  else if (e.key === 'Enter'){ e.preventDefault(); kmzGeomSave(); }
});

// вне режимов Ctrl+Z откатывает последнее сохранение геометрии
document.addEventListener('keydown', (e) => {
  if (!K.closureMode) return;
  if (e.key === 'Escape'){ e.preventDefault(); closureToggle(false); }
});

document.addEventListener('keydown', (e) => {
  if (K.draw || K.editGeom) return;
  if (!K.geomUndoStack.length) return;
  const tag = (e.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z'){
    e.preventDefault();
    kmzGeomUndoLastSave();
  }
});

/* ==========================================================================
   KMZ EVENTS
   ========================================================================== */
$('btnUploadFiles').onclick = () => $('fileInput').click();
$('btnDraw').onclick = () => { if (K.draw) kmzDrawSave(); else kmzDrawStart(); };
$('btnSnap').onclick = () => kmzSnapToggle();
kmzSnapPaint();

$('btnAddClosure').onclick = () => closureToggle();

$('btnMerge').onclick = async () => {
  const ids = [...K.mergeMarked];
  if (ids.length < 2){
    alert('Отметьте 2 и более маршрута кнопкой ◻ в дереве и нажмите «Объединить».');
    return;
  }
  const first = K.routes.find(r => r.id === ids[0]);
  const name = prompt('Название объединённого маршрута:', first ? first.name : 'Объединённый маршрут');
  if (name === null) return;

  let res;
  try {
    res = await fetch('/merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, name: (name || '').trim() || 'Объединённый маршрут' }),
    });
  } catch (e){
    alert('Не удалось объединить: ' + e.message);
    return;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok){
    alert('Не удалось объединить: ' + (data.error || res.status));
    return;
  }
  K.mergeMarked.clear();
  await kmzLoadRoutes({ fit: false });
};

async function moveRoutesToFolder(){
  let ids = [...K.mergeMarked];
  if (!ids.length){
    const needle = prompt('Перенести маршруты, в имени которых есть:', 'Муфта');
    if (needle === null) return;
    const q = needle.trim().toLowerCase();
    if (!q) return;
    ids = K.routes.filter(r => (r.name || '').toLowerCase().includes(q)).map(r => r.id);
    if (!ids.length){
      alert('Маршруты с таким текстом не найдены.');
      return;
    }
  }

  const folder = prompt(`Перенести ${ids.length} маршрут(ов) в папку:`, 'Муфта');
  if (folder === null) return;
  const clean = folder.trim().replace(/^\/+|\/+$/g, '');
  if (!clean) return;

  for (const r of K.routes){
    if (ids.includes(r.id)) r.folder = clean;
  }
  K.customFolders.add(clean);
  K.expandedFolders.add(clean);
  kmzRenderTree();

  await patchRoutesBulk(ids, { folder: clean });
  await kmzFolderAdd(clean);
  await kmzLoadFolders();
  K.mergeMarked.clear();
  kmzRenderTree();
}

$('btnMoveToFolder').onclick = async () => {
  const tab = document.querySelector('.side-tab.active')?.dataset.tab;
  if (tab === 'closures'){
    await moveClosuresToFolder();
    return;
  }
  await moveRoutesToFolder();
};
$('btnRenameMask').onclick = async () => {
  let ids = [...K.mergeMarked];
  if (!ids.length){
    const needle = prompt('Переименовать маршруты, в имени которых есть:', 'Измерение круга');
    if (needle === null) return;
    const q = needle.trim().toLowerCase();
    if (!q) return;
    ids = K.routes.filter(r => (r.name || '').toLowerCase().includes(q)).map(r => r.id);
    if (!ids.length){
      alert('Маршруты с таким текстом не найдены.');
      return;
    }
  }

  const tpl = prompt('Новое имя (символ # заменится на номер по порядку):', 'Муфта #');
  if (tpl === null) return;
  const template = tpl.trim();
  if (!template) return;

  // стабильная нумерация: сначала по папке, затем по старому имени
  const ordered = ids
    .map(id => K.routes.find(r => r.id === id))
    .filter(Boolean)
    .sort((a, b) => `${a.folder || ''}|${a.name || ''}`.localeCompare(`${b.folder || ''}|${b.name || ''}`, 'ru'))
    .map(r => r.id);

  let res;
  try {
    res = await fetch('/routes/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ordered, template }),
    });
  } catch (e){
    alert('Не удалось переименовать: ' + e.message);
    return;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok){
    alert('Не удалось переименовать: ' + (data.error || res.status));
    return;
  }
  K.mergeMarked.clear();
  await kmzLoadRoutes({ fit: false });
};
$('btnUploadFolder').onclick = () => $('folderInput').click();
$('fileInput').onchange = e => kmzUpload([...e.target.files]);
$('folderInput').onchange = e => kmzUpload([...e.target.files]);

$('btnNewFolder').onclick = () => {
  const n = prompt('Название новой папки (можно / для вложенности):');
  if (!n) return;
  const clean = n.trim().replace(/^\/+|\/+$/g, '');
  if (!clean) return;
  K.customFolders.add(clean);
  kmzFolderAdd(clean);
  kmzRenderTree();
};

$('btnExport').onclick = async () => {
  const ids = [];
  for (const r of K.routes){
    const l = K.layers.get(r.id);
    if (l && map.hasLayer(l)) ids.push(r.id);
  }
  if (!ids.length){ alert('Нет видимых маршрутов'); return; }
  const grp = confirm('Группировать по ветвям? OK — да, Отмена — нет.');
  const res = await fetch('/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, group_by_branch: grp }),
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'routes.kmz'; a.click();
  URL.revokeObjectURL(url);
};

$('btnExportTree').onclick = async () => {
  if (!K.routes.length){ alert('Дерево пустое — нечего выгружать.'); return; }

  const visibleCount = K.routes.filter(r => r.visible).length;
  const onlyVisible = confirm(
    'Выгружать только видимые маршруты?\n\n' +
    'OK — только видимые (' + visibleCount + ' шт.)\n' +
    'Отмена — все (' + K.routes.length + ' шт.)'
  );

  const btn = $('btnExportTree');
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Собираю…';

  try {
    const res = await fetch('/export_tree', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ only_visible: onlyVisible }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'routes_tree.zip'; a.click();
    URL.revokeObjectURL(url);
  } catch(e){
    console.error('export_tree failed', e);
    alert('Не удалось выгрузить дерево: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
};

let dragCounter = 0;
window.addEventListener('dragenter', e => {
  if (K.linked) return;
  if (!e.dataTransfer.types.includes('Files')) return;
  dragCounter++;
});
window.addEventListener('dragleave', () => {
  dragCounter = Math.max(0, dragCounter - 1);
});
window.addEventListener('dragover', e => {
  if (e.dataTransfer.types.includes('Files')) e.preventDefault();
});
window.addEventListener('drop', async e => {
  if (K.dragPayload) return;
  e.preventDefault();
  dragCounter = 0;
  const items = [...(e.dataTransfer.items || [])];
  if (!items.length){ kmzUpload([...e.dataTransfer.files]); return; }
  const files = [];
  async function walk(entry, path){
    if (entry.isFile){
      const f = await new Promise((res, rej) => entry.file(res, rej));
      Object.defineProperty(f, 'webkitRelativePath', { value: path + f.name });
      files.push(f);
    } else if (entry.isDirectory){
      const reader = entry.createReader();
      const entries = await new Promise(res => {
        const all = [];
        const read = () => reader.readEntries(b => {
          if (!b.length) res(all); else { all.push(...b); read(); }
        });
        read();
      });
      for (const c of entries) await walk(c, path + entry.name + '/');
    }
  }
  for (const it of items){
    const entry = it.webkitGetAsEntry?.();
    if (entry) await walk(entry, '');
  }
  if (files.length) kmzUpload(files);
});

/* ==========================================================================
   GPON EVENTS
   ========================================================================== */
$('fileGeo').onchange = async e => {
  const files = [...(e.target.files || [])];
  if (!files.length) return;

  $('diag').textContent = `Чтение ${files.length} файлов…`;
  const out = new Map();
  const results = await Promise.all(files.map(async f => {
    try {
      const buf = await f.arrayBuffer();
      const r = gponParseGeoWorkbook(buf, f.name, out);
      return { name: f.name, ...r, ok: true };
    } catch(err){ return { name: f.name, ok: false, error: err.message }; }
  }));

  G.geo = out;
  G.geoFiles = files.map(f => f.name);
  $('fileGeoBtn').classList.add('active');
  $('fileGeoBtn').childNodes[0].nodeValue = `📂 Excel × ${files.length} `;

  const lines = results.map(r => r.ok ? `  ✓ ${r.name}: ${r.added}` : `  ✗ ${r.name}: ${r.error}`);
  const total = results.reduce((s, r) => s + (r.ok ? r.added : 0), 0);
  $('diag').textContent = `Excel: ${files.length} файл., адресов: ${total}\n` + lines.join('\n');

  if (G.onus.length) await gponReindex();
  await gponSaveGeo();
  e.target.value = '';
};

$('fileVg').onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;

  try {
    const text = gponDecodeBytes(await f.arrayBuffer());
    const r = gponParseVgroups(text);
    G.vgFile = f.name;
    $('fileVgBtn').classList.add('active');
    $('fileVgBtn').childNodes[0].nodeValue = `📂 ${f.name} `;
    $('diag').textContent = `${f.name}: ${r.total} ONU, на карте ${r.matched}, без координат ${r.unmatched}`;
    gponBuildStatusFilter();
    gponBuildBranchFilter();
    gponBuildLayers();
    gponUpdateStats();
    await gponSaveOnus();
  } catch(err){ alert('CSV: ' + err.message); }
  e.target.value = '';
};

async function gponReindex(){
  let m = 0, u = 0;
  for (const o of G.onus){
    const g = G.geo.get(gponNormAddr(o.address));
    o.lat = g?.lat ?? null;
    o.lng = g?.lng ?? null;
    if (g) m++; else u++;
  }
  gponBuildStatusFilter();
  gponBuildBranchFilter();
  gponBuildLayers();
  gponUpdateStats();
  $('diag').textContent = `geo: ${G.geo.size} адресов · ONU: ${G.onus.length} · на карте ${m} · без координат ${u}`;
  await gponSaveGeo();
  await gponSaveOnus();
}

$('reset-filters').onclick = () => {
  G.filters.status.clear();
  G.filters.branch.clear();
  document.querySelectorAll('#sidebarGpon input[data-filter]').forEach(i => i.checked = false);
  document.querySelectorAll('#sidebarGpon .group-check, #sidebarGpon .subgroup-check, #sidebarGpon .subsub-check')
    .forEach(i => { i.checked = false; i.indeterminate = false; });
  $('filter-branch').value = '';
  document.querySelectorAll('#filter-branch-list .filter-item, #filter-branch-list .branch-subsub, #filter-branch-list .branch-subgroup, #filter-branch-list .branch-group')
    .forEach(el => el.style.display = '');
  gponClearBranchHighlight();
  gponApplyKmzLinkFilter();
};

/* ==========================================================================
   SEARCH / THEME / MARKERS VISIBILITY / INIT
   ========================================================================== */
$('search').addEventListener('input', debounce(() => {
  const q = $('search').value.trim().toLowerCase();
  if (document.querySelector('.mode-switch button.active').dataset.mode === 'kmz'){
    K.search = q;
    kmzApplyFilters();
  } else {
    gponSearch(q);
  }
}, 180));

function applyTheme(t){
  document.documentElement.dataset.theme = t;
  $('theme').textContent = t === 'light' ? '☀️' : '🌙';
  try { localStorage.setItem('viewer-theme', t); } catch(_) {}
}
applyTheme(localStorage.getItem('viewer-theme') || 'dark');
$('theme').onclick = () =>
  applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');

$('btnMarkersVis').onclick = () => gponSetMarkersVisible(!G.markersVisible);

/* ==========================================================================
   UI — меню редактирования, сворачивание панели, вкладки
   ========================================================================== */
function editMenuToggle(force){
  const panel = $('editMenuPanel');
  const btn = $('btnEditMenu');
  if (!panel || !btn) return;
  const open = typeof force === 'boolean' ? force : panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !open);
  btn.classList.toggle('active', open);
}
$('btnEditMenu').onclick = e => { e.stopPropagation(); editMenuToggle(); };
document.addEventListener('click', e => {
  if (!e.target.closest('#editMenu')) editMenuToggle(false);
});
document.querySelectorAll('#editMenuPanel .menu-item').forEach(b => {
  b.addEventListener('click', () => editMenuToggle(false));
});

function setCompact(on){
  const bar = document.querySelector('.topbar');
  if (!bar) return;
  bar.classList.toggle('compact', on);
  const btn = $('btnCollapse');
  if (btn) btn.textContent = on ? '▾' : '▴';
  try { localStorage.setItem('ui-compact', on ? '1' : '0'); } catch(_) {}
}
$('btnCollapse').onclick = () =>
  setCompact(!document.querySelector('.topbar').classList.contains('compact'));

function setSideTab(tab){
  document.querySelectorAll('.side-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.side-panel').forEach(p =>
    p.classList.toggle('hidden', p.dataset.panel !== tab));
  try { localStorage.setItem('side-tab', tab); } catch(_) {}
}
document.querySelectorAll('.side-tab').forEach(b => {
  b.onclick = () => setSideTab(b.dataset.tab);
});

const TOOL_BUTTONS = {
  btnToolsDraw: 'btnDraw',
  btnToolsClosure: 'btnAddClosure',
  btnToolsMerge: 'btnMerge',
  btnToolsRename: 'btnRenameMask',
  btnToolsMove: 'btnMoveToFolder',
};
for (const [toolId, targetId] of Object.entries(TOOL_BUTTONS)){
  const el = $(toolId);
  if (el) el.onclick = () => $(targetId).click();
}

async function moveClosuresToFolder(){
  if (!K.closures.length){
    alert('Муфт пока нет.');
    return;
  }
  const needle = prompt('Перенести муфты, в имени которых есть (например, bdcom-sla2/GPON0/3):', '');
  if (needle === null) return;
  const q = needle.trim().toLowerCase();
  if (!q) return;

  const matches = K.closures.filter(c => (c.name || '').toLowerCase().includes(q));
  if (!matches.length){
    alert('Муфты с таким текстом не найдены.');
    return;
  }

  const folder = prompt(
    `Найдено ${matches.length}. Папка назначения (например, Муфты/bdcom-sla2/3):`, '');
  if (folder === null) return;
  const target = folder.trim().replace(/^\/+|\/+$/g, '');
  if (!target) return;

  let res;
  try {
    res = await fetch('/closures/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: matches.map(c => c.id), folder: target }),
    });
  } catch (e){
    alert('Не удалось перенести: ' + e.message);
    return;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok){
    alert('Не удалось перенести: ' + (data.error || res.status));
    return;
  }
  closureExpandFolder(target);
  await closuresLoad();
  alert(`Перенесено муфт: ${data.moved}.`);
}

(async function init(){
  // восстанавливаем видимость меток ДО того, как загрузятся данные
  const savedVis = localStorage.getItem('gpon-markers-visible');
  G.markersVisible = savedVis === null ? true : savedVis === '1';

  await kmzLoadRoutes();
  await kmzLoadFolders();
  await closuresLoad();
  await mapsLoad();
  await gponLoadState();
  setMode('kmz');
  setCompact(localStorage.getItem('ui-compact') === '1');
  setSideTab(localStorage.getItem('side-tab') || 'routes');
  $('s-found').textContent = '—';
  $('btnMarkersVis').textContent = G.markersVisible ? '👁️' : '🙈';
  $('btnMarkersVis').title = G.markersVisible ? 'Скрыть GPON-метки' : 'Показать GPON-метки';
})();