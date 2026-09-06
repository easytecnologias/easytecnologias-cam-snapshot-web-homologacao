// Inventario Cameras IP -- estado (movido de dashboard.js, onde tinha ficado
// esquecido desde uma divisao anterior deste arquivo; nada em dashboard.js
// depende disso, confirmado por grep antes da mudanca).
const _invCam   = { basico: [], olt: [], switch: [] };
let _invOltView   = (() => {
  try { return sessionStorage.getItem('so_cam_view') || 'olt'; } catch { return 'olt'; }
})();
let _invOltActive = null;
let _pingInterval = null;
let _pendingOpenCamIp = null;

function _invOltAll_get() { return _invCam[_invOltView] || []; }

function _camSessionSave(mode, rows) {
  try { sessionStorage.setItem(`so_cam_${mode}`, JSON.stringify(rows)); } catch {}
}
function _camSessionLoad() {
  ['basico','olt','switch'].forEach(m => {
    try {
      const d = JSON.parse(sessionStorage.getItem(`so_cam_${m}`) || 'null');
      if (Array.isArray(d)) _invCam[m] = d;
    } catch {}
  });
}
function _camSessionClear() {
  ['basico','olt','switch'].forEach(m => {
    try { sessionStorage.removeItem(`so_cam_${m}`); } catch {}
    _invCam[m] = [];
  });
}

function _camKey(camOrIp) {
  if (typeof camOrIp === 'string') return `IP:${camOrIp.trim()}`;
  const cam = camOrIp || {};
  const existingKey = String(cam.inventory_key || cam.key || '').trim();
  if (existingKey) return existingKey;
  const ip = String(cam.ip || cam.IP || '').trim();
  const connector = String(cam.remote_connector_id || cam.connector_id || '').trim();
  const site = String(cam.site || cam.site_name || cam.local || '').trim().toLowerCase();
  if (connector && ip) return `REMOTE:${connector}:IP:${ip}`;
  if ((cam.remote === true || cam.remote === 'true' || cam.remote === 1) && site && ip) return `REMOTE_SITE:${site}:IP:${ip}`;
  return `IP:${ip}`;
}

function _camRemoveIpsLocally(ips) {
  const doomed = new Set((ips || []).map(ip => String(ip || '').trim()).filter(Boolean));
  if (!doomed.size) return;
  ['basico','olt','switch'].forEach(mode => {
    _invCam[mode] = (_invCam[mode] || []).filter(cam => !doomed.has(String(cam.ip || '').trim()) && !doomed.has(_camKey(cam)));
    _camSessionSave(mode, _invCam[mode]);
  });
  try {
    const imgbb = _imgbbGet();
    doomed.forEach(ip => {
      delete imgbb[ip];
      delete imgbb[`IP:${ip}`];
    });
    sessionStorage.setItem('so_imgbb', JSON.stringify(imgbb));
  } catch {}
}

function updateCamTabs() {
  const fixedViews = new Set(['basico', 'olt', 'switch']);
  document.querySelectorAll('.inv-view-tab[data-view]').forEach(t => {
    const view = t.dataset.view;
    const hasData = _invCam[view]?.length > 0;
    t.style.display = (fixedViews.has(view) || hasData) ? '' : 'none';
  });
  if (!['basico', 'olt', 'switch'].includes(_invOltView)) {
    _invOltView = 'basico';
  }
  document.querySelectorAll('.inv-view-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.view === _invOltView)
  );
}

function cameraImgbbUrl(c) {
  if (!c) return '';
  const candidates = [
    c.imgbb_url,
    c.imgbb_thumb_url,
    c.thumbnail_url,
    c.thumb_url,
    c.display_url,
    c.url,
    c.snapshot_url,
  ];
  return candidates.map(v => String(v || '').trim()).find(isImgbbUrl) || '';
}

// Celulas base compartilhadas entre as 3 visoes
function _camCell(c) {
  const imgbbUrl = cameraImgbbUrl(c);
  const key = _camKey(c);
  return {
    chk:       `<input type="checkbox" class="chk-olt" value="${esc(c.ip)}" data-key="${esc(key)}">`,
    ip:        `<span class="monospace text-primary" title="${esc(c.ip)}">${esc(c.ip)}</span>`,
    mac:       `<span class="monospace" title="${esc(c.mac||'')}" style="font-size:11px">${esc(c.mac||'')}</span>`,
    fab:       `<span class="text-muted" title="${esc(c.fabricante||'')}">${esc(c.fabricante||'')}</span>`,
    modelo:    `<span title="${esc(c.modelo || c.model || '')}">${esc(c.modelo || c.model || '')}</span>`,
    titulo:    `<strong title="${esc(c.titulo||'')}">${esc(c.titulo||'')}</strong>`,
    status:    invStatusBadge(c.status),
    imgbb:     imgbbUrl ? `<a href="${esc(imgbbUrl)}" target="_blank" onclick="event.stopPropagation()" style="color:var(--primary);font-weight:700;font-size:12px;text-decoration:none"> up</a>` : `<span style="color:var(--danger);font-weight:700;font-size:12px"> down</span>`,
    local:     `<span class="text-muted" title="${esc(c.local||'')}">${esc(c.local||'')}</span>`,
    pon:       `<span style="text-align:center;display:block;font-weight:500">${esc(c.pon||'')}</span>`,
    onu_id:    `<span style="text-align:center;display:block;font-weight:500">${esc(c.onu_id||'')}</span>`,
    onu_name:  `<span class="text-muted" title="${esc(c.onu_name||'')}" style="font-size:11px">${esc(c.onu_name||'')}</span>`,
    onu_ser:   `<span class="monospace text-muted" title="${esc(c.onu_serial||'')}" style="font-size:11px">${esc(c.onu_serial||'')}</span>`,
    sw_name:   `<span class="text-muted" title="${esc(c.switch_name||'')}">${esc(c.switch_name||'')}</span>`,
    sw_ip:     `<span class="monospace text-muted" title="${esc(c.switch_ip||'')}">${esc(c.switch_ip||'')}</span>`,
    sw_port:   `<span class="text-muted" title="${esc(c.switch_port||'')}">${esc(c.switch_port||'')}</span>`,
    sw_vlan:   `<span class="text-muted" title="${esc(c.switch_vlan||'')}">${esc(c.switch_vlan||'')}</span>`,
    sw_combo:  (() => {
      const name = c.switch_name || '';
      const ip = c.switch_ip || '';
      // So o nome aparece na celula (curto, cabe); IP completo vai so no
      // tooltip -- "nome (ip)" inteiro nunca coube em nenhuma largura
      // razoavel de coluna com nomes reais tipo "SWITCH GALPAO".
      const shown = name || ip;
      const full = name && ip ? `${name} (${ip})` : shown;
      return `<span class="text-muted" title="${esc(full)}">${esc(shown)}</span>`;
    })(),
  };
}

// Larguras em % (nao em px): garantem matematicamente que a tabela nunca
// ultrapassa 100% do container, ou seja, nunca gera scroll horizontal,
// independente de resolucao/zoom/escala do Windows. Identificadores
// (IP/MAC/PON/ONU ID/Serial/Porta) recebem a maior fatia para minimizar
// truncamento; colunas descritivas (Fabricante/Modelo/Titulo/Local/ONU Name)
// truncam com reticencias + tooltip (title=) quando o espaco aperta.
const INV_COLS = {
  // Basico: IP, MAC, Fabricante, Modelo, Titulo, Status, ImgBB, Local
  basico: {
    minWidth: '980px',
    cols:  ['3%','11%','13%','9%','14%','22%','7%','6%','15%'],
    heads: ['',    'IP', 'MAC','Fabricante','Modelo','Titulo','Status','ImgBB','Local'],
    row: c => { const v = _camCell(c); return [v.chk, v.ip, v.mac, v.fab, v.modelo, v.titulo, v.status, v.imgbb, v.local]; },
  },
  // OLT: base enxuta + dados OLT
  olt: {
    minWidth: '1180px',
    cols:  ['3%','10%','12%','8%','12%','18%','6%','5%','8%','5%','6%','7%','8%'],
    heads: ['',    'IP','MAC','Fabricante','Modelo','Titulo','Status','ImgBB','Local','PON','ONU ID','ONU Name','ONU Serial'],
    row: c => { const v = _camCell(c); return [v.chk, v.ip, v.mac, v.fab, v.modelo, v.titulo, v.status, v.imgbb, v.local, v.pon, v.onu_id, v.onu_name, v.onu_ser]; },
  },
  // Switch: base enxuta + dados Switch
  switch: {
    // Larguras calculadas a partir do conteudo real max esperado por coluna
    // (chars x largura media do char no font-size/peso da celula + os 20px
    // de padding fixo de .data-table td), nao estimativa visual:
    //   chk ~20px | IP "172.16.100.204" mono | MAC "50:e5:38:e8:e1:df" mono 11px
    //   Fabricante "Intelbras/Hikvision" | Modelo ate ~16 chars | Titulo ate ~28 chars (bold)
    //   Status "auth_failed" (bold) | ImgBB "up/down" (bold) | Local ate ~14 chars
    //   Switch (nome) ate ~18 chars | Porta "Eth10/Ge10" mono | VLAN "default"
    minWidth: '1080px',
    cols:  ['3%','10%','12%','8%','13%','13%','6%','5%','8%','12%','5%','5%'],
    heads: ['',    'IP','MAC','Fabricante','Modelo','Titulo','Status','ImgBB','Local','Switch','Porta','VLAN'],
    row: c => { const v = _camCell(c); return [v.chk, v.ip, v.mac, v.fab, v.modelo, v.titulo, v.status, v.imgbb, v.local, v.sw_combo, v.sw_port, v.sw_vlan]; },
  },
};

async function setInvOltView(view) {
  _invOltView = view;
  try { sessionStorage.setItem('so_cam_view', view); } catch {}
  document.querySelectorAll('.inv-view-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.view === view)
  );
  try {
    await _loadCamForMode(view);
    updateCamTabs();
    populateCamSiteFilter();
  } catch (e) {
    console.warn('Falha ao carregar visao', view, e);
  }
  applyInvOltFilters();
}

// Persiste mapeamento camera->imgbb_url no sessionStorage.
function _imgbbSave(camOrIp, url) {
  try {
    const m = JSON.parse(sessionStorage.getItem('so_imgbb') || '{}');
    const key = _camKey(camOrIp);
    if (key) m[key] = url;
    if (typeof camOrIp === 'string') m[`IP:${camOrIp.trim()}`] = url;
    sessionStorage.setItem('so_imgbb', JSON.stringify(m));
  } catch {}
}
function _imgbbGet() {
  try { return JSON.parse(sessionStorage.getItem('so_imgbb') || '{}'); } catch { return {}; }
}
function _imgbbClear() {
  try { sessionStorage.removeItem('so_imgbb'); } catch {}
}

//  Mapa de cameras (Leaflet)
let _map            = null;
let _mapFeatures    = [];
let _mapLayers      = [];
let _mapLayerGroups = {}; // id  { group, active, features }
let _mapCameraIndex = { byName: {}, byIp: {} };

// Modo de edicao de pontos: uma camada por vez. _mapEditPendingCam guarda a
// camera escolhida na lista "sem ponto no mapa" enquanto se espera o clique
// no mapa que vai criar o ponto dela.
let _mapEditLayerId     = null;
let _mapEditDef         = null;
let _mapEditPendingCam  = null;
let _mapEditClickHandler = null;

function mapEditIsActive(id) {
  return !!_mapEditLayerId && _mapEditLayerId === id;
}

// Definicao das camadas disponiveis
const MAP_LAYER_DEFS = [
  { id: 'cameras',  get label() { return sessionStorage.getItem('so_kmz_generated_name') || 'Cameras do Inventario'; },
    color: '#16a34a', endpoint: '/api/kmz/generated/geojson', source: 'generated' },
];

function mapExtractIp(text) {
  const m = String(text || '').match(/\b(?:\d{1,3}\.){3}\d{1,3}\b/);
  return m ? m[0] : '';
}

function mapFeatureName(feature) {
  return String(feature?.properties?.name || '').trim();
}

function mapFeatureLocal(feature, cam) {
  if (cam?.local) return cam.local;
  const desc = String(feature?.properties?.description || '');
  const m = desc.match(/LOCAL.*?>(.*?)</i);
  return m ? m[1].trim() : '';
}

function mapFindCamera(feature, index = _mapCameraIndex) {
  const name = mapFeatureName(feature);
  const desc = String(feature?.properties?.description || '');
  const ip = mapExtractIp(desc) || mapExtractIp(name);
  return index.byName[name.toLowerCase()] || index.byIp[ip] || null;
}

function mapFeatureType(feature, cam) {
  const name = mapFeatureName(feature);
  const desc = String(feature?.properties?.description || '');
  const geomType = String(feature?.geometry?.type || '').toLowerCase();
  if (/^fibra\b|\bfibra\b|fiber|cabo optico|cabo óptico/i.test(name) || geomType === 'linestring') return 'fiber';
  if (/^pt[-\s]?\d+\b|\bposte\b|\bpostes\b|\bpole\b/i.test(name)) return 'pole';
  if (/caixa\s*herm[eé]tica|caixa\s*cftv|hermetica|hermética/i.test(name)) return 'box';
  if (cam || (!feature?._source && geomType === 'point') || feature?._source === 'generated') return 'camera';
  if (/\bcto\b|^cto/i.test(name)) return 'cto';
  if (/\bcdo\b|^cdo|emenda|splice/i.test(name)) return 'cdo';
  if (/cam|camera|vip-|vipc|vip\s|\bcam\s/i.test(name)) return 'camera';
  if (/\bSTATUS\b|\bLOCAL\b|\bMODELO\b|\bFABRICANTE\b|vip-|vipc|hikvision|intelbras/i.test(desc)) return 'camera';
  return 'other';
}

function mapFeatureStatus(feature, cam) {
  const desc = String(feature?.properties?.description || '').toUpperCase();
  const status = String(cam?.status || '').toLowerCase();
  if (status === 'online') return 'online';
  if (status === 'offline') return 'offline';
  if (desc.includes('ONLINE')) return 'online';
  if (desc.includes('OFFLINE')) return 'offline';
  return 'outros';
}

function mapTypeLabel(type) {
  return ({
    camera: 'Camera',
    fiber: 'Fibra',
    pole: 'Poste',
    box: 'Caixa CFTV',
    cto: 'CTO',
    cdo: 'Emenda/CDO',
    other: 'Outros',
  })[type] || 'Outros';
}

function mapFeatureKey(feature, cam = null) {
  const ip = cam?.ip || mapExtractIp(feature?.properties?.description) || mapExtractIp(mapFeatureName(feature));
  const name = mapFeatureName(feature);
  const coords = feature?.geometry?.coordinates || [];
  const first = Array.isArray(coords?.[0]) ? coords[0] : coords;
  return [ip || name, first?.[1], first?.[0]].map(v => String(v ?? '').trim()).join('|');
}

function mapInventoryMode() {
  const selected = String(document.getElementById('mapInventoryMode')?.value || '').trim().toLowerCase();
  if (['basico', 'olt', 'switch'].includes(selected)) return selected;
  const current = String(_invOltView || '').trim().toLowerCase();
  return ['basico', 'olt', 'switch'].includes(current) ? current : 'olt';
}

function mapLayerStats(features, index = _mapCameraIndex) {
  return (features || []).reduce((acc, f) => {
    const cam = mapFindCamera(f, index);
    const type = mapFeatureType(f, cam);
    acc.total += 1;
    if (type === 'camera') {
      acc.cameras += 1;
      const status = mapFeatureStatus(f, cam);
      if (status === 'online') acc.online += 1;
      else if (status === 'offline') acc.offline += 1;
      else acc.outros += 1;
    } else if (type === 'fiber') {
      acc.fiber += 1;
    } else if (type === 'pole') {
      acc.poles += 1;
    } else if (type === 'box') {
      acc.boxes += 1;
    } else if (type === 'cto') {
      acc.cto += 1;
    } else if (type === 'cdo') {
      acc.cdo += 1;
    } else {
      acc.outros += 1;
    }
    return acc;
  }, { total: 0, cameras: 0, online: 0, offline: 0, outros: 0, fiber: 0, poles: 0, boxes: 0, cto: 0, cdo: 0 });
}

function mapLayerColor(features, fallback = '#d97706') {
  const stats = mapLayerStats(features, _mapCameraIndex);
  if (stats.cameras > 0) return '#16a34a';
  if (stats.fiber > 0) return '#0ea5e9';
  if (stats.boxes > 0) return '#f59e0b';
  if (stats.poles > 0) return '#64748b';
  const names = (features || []).map(f => mapFeatureName(f)).join(' ');
  if (/\bcto\b|^cto/i.test(names)) return '#1971c2';
  if (/\bcdo\b|^cdo|emenda|splice/i.test(names)) return '#7950f2';
  return fallback;
}

function mapLayerSignature(features) {
  return (features || [])
    .filter(f => f?.geometry?.type === 'Point')
    .map(f => {
      const coords = f.geometry?.coordinates || [];
      const name = mapFeatureName(f).toLowerCase();
      const lat = Number(coords[1] || 0).toFixed(6);
      const lng = Number(coords[0] || 0).toFixed(6);
      return `${name}|${lat}|${lng}`;
    })
    .sort()
    .slice(0, 80)
    .join('||');
}

function mapLayerPointKeys(features, options = {}) {
  const includeName = options.includeName !== false;
  return new Set((features || [])
    .filter(f => f?.geometry?.type === 'Point')
    .map(f => {
      const coords = f.geometry?.coordinates || [];
      const name = mapFeatureName(f).toLowerCase();
      const lat = Number(coords[1] || 0).toFixed(6);
      const lng = Number(coords[0] || 0).toFixed(6);
      return includeName ? `${name}|${lat}|${lng}` : `${lat}|${lng}`;
    })
    .filter(Boolean));
}

function mapLayerOverlapsImported(layer, importedPointSets) {
  const keys = mapLayerPointKeys(layer.features, { includeName: false });
  if (!keys.size) return false;
  for (const importedKeys of importedPointSets) {
    if (!importedKeys?.size) continue;
    let common = 0;
    keys.forEach(key => { if (importedKeys.has(key)) common += 1; });
    const overlap = common / Math.min(keys.size, importedKeys.size);
    if (overlap >= 0.8) return true;
  }
  return false;
}

async function mapLoadCameraIndex() {
  const mode = mapInventoryMode();
  const camData = await apiJson(`/api/cameras?mode=${encodeURIComponent(mode)}&_=${Date.now()}`);
  const cams = camData?.cameras || [];
  const byName = {};
  const byIp = {};
  cams.forEach(c => {
    if (c.titulo) byName[String(c.titulo).toLowerCase()] = c;
    if (c.ip) byIp[String(c.ip)] = c;
  });
  _mapCameraIndex = { byName, byIp };
  return _mapCameraIndex;
}

async function refreshMapLiveStatus() {
  try {
    showToast('Sincronizando status real das cameras...');
    const res = await api('/api/scripts/zabbix/status-sync', {
      method: 'POST',
      body: JSON.stringify({ source: 'ip', mode: mapInventoryMode(), site: '', validate_offline: true }),
    });
    const body = await res?.json().catch(() => ({}));
    if (!res?.ok || body?.ok === false) {
      showToast(body?.detail || body?.error || 'Nao foi possivel sincronizar status agora.', true);
      return;
    }
    const extra = body.validated_online ? `, ${body.validated_online} validadas por TCP` : '';
    showToast(`Status atualizado: ${body.online || 0} online, ${body.offline || 0} offline${extra}.`);
  } catch (err) {
    showToast(`Falha ao atualizar status: ${err.message || err}`, true);
  }
}

async function loadKmz() {
  const container = document.getElementById('leafletMap');
  if (!container) return;

  if (!_map) {
    _map = L.map('leafletMap', { zoomControl: true }).setView([-9.76, -36.67], 14);
    // OpenStreetMap direto: a CARTO passou a exigir chave de API nos mapas
    // dark_all e as tiles voltavam carimbadas com "API KEY REQUIRED". O visual
    // escuro e recuperado por filtro CSS sobre o painel de tiles -- os
    // marcadores ficam em outro painel e nao sao afetados.
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap',
      subdomains: 'abc', maxZoom: 19,
    }).addTo(_map);
  }

  setTimeout(() => _map.invalidateSize(), 100);

  // Carrega e renderiza painel de camadas
  await loadMapLayers();

  // Nada mais a fazer aqui  as camadas sao gerenciadas pelo painel.
  // Sem texto: o painel de camadas ja diz o que fazer, e a frase antiga
  // ("Selecione camadas no painel") comia ~190px da barra e empurrava a legenda
  // para uma segunda linha. Assim que uma camada acende, aqui vira "N pontos".
  setText('mapCounter', '');

  // Popula filtro de sites (extraido das propriedades)
  const sites = [...new Set(_mapFeatures.map(f => {
    const m = (f.properties?.description || '').match(/LOCAL.*?>(.*?)</i);
    return m ? m[1].trim() : '';
  }).filter(Boolean))].sort();
  const selSite = document.getElementById('mapFilterSite');
  if (selSite) {
    const cur = selSite.value;
    selSite.innerHTML = '<option value="">Todos os sites</option>' +
      sites.map(s => `<option${s===cur?' selected':''}>${esc(s)}</option>`).join('');
  }

  setTimeout(() => _map.invalidateSize(), 200);
}

function mapLayerDownloadUrl(def) {
  const raw = String(def?.downloadUrl || '');
  if (!raw) return '';
  if (def?.source !== 'imported' || !raw.includes('/download-enriched')) return raw;
  const sep = raw.includes('?') ? '&' : '?';
  return `${raw}${sep}source=ip&mode=${encodeURIComponent(mapInventoryMode())}`;
}

let _mapLayerRenameTarget = null;

function openMapLayerRename(def) {
  if (!def?.updateUrl) return;
  _mapLayerRenameTarget = def;
  const modal = document.getElementById('modalMapLayerRename');
  const input = document.getElementById('mapLayerRenameInput');
  const hint = document.getElementById('mapLayerRenameHint');
  if (!modal || !input) return;
  input.value = String(def.label || '').trim();
  if (hint) hint.textContent = `${def.count || 0} ponto(s) nesta camada`;
  modal.classList.remove('hidden');
  setTimeout(() => {
    input.focus();
    input.select();
  }, 30);
  lucide.createIcons();
}

function closeMapLayerRename() {
  document.getElementById('modalMapLayerRename')?.classList.add('hidden');
  _mapLayerRenameTarget = null;
}

function handleMapLayerRenameKey(event) {
  if (event.key === 'Escape') {
    closeMapLayerRename();
    return;
  }
  if (event.key === 'Enter') {
    event.preventDefault();
    saveMapLayerRename();
  }
}

async function saveMapLayerRename() {
  const def = _mapLayerRenameTarget;
  const updateUrl = String(def?.updateUrl || '');
  if (!def || !updateUrl) return;
  const input = document.getElementById('mapLayerRenameInput');
  const label = String(input?.value || '').trim();
  if (!label) {
    showToast('Informe o nome da camada.', true);
    input?.focus();
    return;
  }
  const btn = document.getElementById('confirmMapLayerRename');
  if (btn) btn.disabled = true;
  const res = await api(updateUrl, {
    method: 'PATCH',
    body: JSON.stringify({ label }),
  });
  const body = await res?.json().catch(() => ({}));
  if (btn) btn.disabled = false;
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || body?.error || 'Nao foi possivel renomear a camada.', true);
    return;
  }
  closeMapLayerRename();
  showToast('Nome da camada atualizado.');
  await loadMapLayers();
}

// Uma camada so mostra o que foi desenhado no KMZ. Cameras que entraram no
// inventario depois ficam sem ponto -- e a contagem da camada passa seguranca
// falsa: no site RESERVA o mapa dizia "2 offline" enquanto 4 offline estavam
// fora dele. Os pontos nao trazem o nome do site, entao descobrimos o site pelo
// IP das cameras que a camada JA mostra.
// Mostra QUAIS cameras ficaram de fora. Reaproveita a gaveta do dashboard e o
// atalho que abre a camera no inventario -- e la que a correcao e feita (quase
// sempre preencher o titulo, que e o que liga a camera ao ponto do KMZ).
function abrirDrawerCamerasFora(site, cams) {
  if (typeof _openDashDrawer !== 'function') return;
  _openDashDrawer('Mapa', `Sem ponto no mapa${site ? ' - ' + site : ''}`);
  const body = document.getElementById('dashDrawerBody');
  if (!body) return;

  const semTitulo = cams.filter(c => !c.titulo).length;
  const nota = semTitulo
    ? `<div style="padding:10px 14px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--border)">`
      + `${semTitulo} dela(s) esta(o) <strong>sem titulo</strong> no inventario. O mapa liga o ponto do KMZ `
      + `a camera pelo IP ou pelo titulo -- preenchendo o titulo, elas passam a aparecer no mapa.</div>`
    : '';

  const linhas = cams.map(c => `
    <div data-cam-ip="${esc(c.ip)}" style="display:flex;align-items:center;gap:10px;padding:10px 14px;
         border-bottom:1px solid var(--border);cursor:pointer">
      ${typeof _drawerStatusDot === 'function' ? _drawerStatusDot(c.status) : ''}
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600">${esc(c.ip)}</div>
        <div style="font-size:12px;color:${c.titulo ? 'var(--muted)' : '#b45309'}">
          ${esc(c.titulo || 'sem titulo cadastrado')}</div>
      </div>
      <span style="font-size:11px;color:var(--muted);flex-shrink:0">${esc(c.status || '')}</span>
    </div>`).join('');

  body.innerHTML = nota + (linhas || '<div style="padding:24px;text-align:center;color:var(--muted)">Nada a listar.</div>');
  body.querySelectorAll('[data-cam-ip]').forEach(el => {
    el.addEventListener('click', () => {
      const modo = typeof mapInventoryMode === 'function' ? mapInventoryMode() : 'olt';
      if (typeof _drawerGoToInventory === 'function') {
        _drawerGoToInventory('inv-olt', el.dataset.camIp, modo);
      }
    });
  });
  if (window.lucide?.createIcons) lucide.createIcons();
}

function camerasForaDoMapa(layer) {
  try {
    const idx = _mapCameraIndex || { byName: {}, byIp: {} };
    const inventario = Object.values(idx.byIp || {});
    if (!inventario.length) return { total: 0, offline: 0, semInventario: true };

    // Casar pelo MESMO criterio da camada (IP ou titulo). Procurar so o IP no
    // texto do ponto contava como ausente toda camera que o KMZ identifica pelo
    // nome -- foi assim que uma camada com 30 de 30 pontos casados acusou 12
    // cameras "sem ponto no mapa".
    const noMapa = new Set();
    (layer.features || []).forEach(f => {
      const cam = mapFindCamera(f, idx);
      const ip = String(cam?.ip || '').trim();
      if (ip) noMapa.add(ip);
    });
    if (!noMapa.size) return { total: 0, offline: 0 };
    const siteDe = c => String(c.local || c.site || c.site_name || '').trim().toUpperCase();

    // site da camada = o mais frequente entre as cameras que ela contem
    const contagem = {};
    inventario.forEach(c => {
      const ip = String(c.ip || '').trim();
      if (ip && noMapa.has(ip)) {
        const s = siteDe(c);
        if (s) contagem[s] = (contagem[s] || 0) + 1;
      }
    });
    const site = Object.keys(contagem).sort((a, b) => contagem[b] - contagem[a])[0];
    if (!site) return { total: 0, offline: 0 };

    const doSite = inventario.filter(c => siteDe(c) === site && String(c.ip || '').trim());
    const fora = doSite.filter(c => !noMapa.has(String(c.ip).trim()));
    return {
      total: fora.length,
      offline: fora.filter(c => String(c.status || '').toLowerCase() !== 'online').length,
      site,
      // o numero sozinho nao conserta nada; a lista e o que permite agir
      lista: fora.map(c => ({
        ip: String(c.ip || '').trim(),
        titulo: String(c.titulo || c.title || c.nome || '').trim(),
        status: String(c.status || '').trim(),
      })).sort((a, b) => a.ip.localeCompare(b.ip, 'pt', { numeric: true })),
    };
  } catch {
    return { total: 0, offline: 0 };
  }
}

async function loadMapLayers() {
  const listEl = document.getElementById('mapLayersList');
  if (!listEl) return;

  listEl.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:8px;text-align:center">Carregando camadas</div>';

  const camIndex = await mapLoadCameraIndex();
  const previousActive = new Set(Object.entries(_mapLayerGroups || {}).filter(([, s]) => s?.active).map(([id]) => id));

  const importedData = await apiJson('/api/kmz/import/layers');
  const importedLayers = (importedData?.layers || []).map((layer, idx) => ({
    id: `imported:${layer.id}`,
    layerId: layer.id,
    label: layer.label || layer.original_name || `Mapa ${idx + 1}`,
    color: mapLayerColor((layer.features || []).map(f => ({ ...f, _source: 'imported', _layerId: layer.id }))),
    source: 'imported',
    features: (layer.features || []).map(f => ({ ...f, _source: 'imported', _layerId: layer.id })),
    count: Number(layer.features_count ?? layer.features?.length ?? 0),
    downloadUrl: layer.download_url || `/api/kmz/import/layers/${encodeURIComponent(layer.id)}/download-enriched`,
    rawDownloadUrl: layer.raw_download_url || `/api/kmz/import/layers/${encodeURIComponent(layer.id)}/download`,
    updateUrl: layer.update_url || `/api/kmz/import/layers/${encodeURIComponent(layer.id)}`,
    deleteUrl: `/api/kmz/import/layers/${encodeURIComponent(layer.id)}`,
  }));
  const importedSignatures = new Set(importedLayers.map(layer => mapLayerSignature(layer.features)).filter(Boolean));
  const importedLayerIds = new Set(importedLayers.map(layer => layer.layerId).filter(Boolean));
  const importedPointSets = importedLayers.map(layer => mapLayerPointKeys(layer.features, { includeName: false })).filter(set => set.size);

  const generatedData = await apiJson('/api/kmz/generated/layers').catch(() => ({ layers: [] }));
  const generatedLayers = (generatedData?.layers || []).map((layer, idx) => ({
    id: `generated:${layer.id}`,
    layerId: layer.id,
    label: layer.label || layer.original_name || `Mapa gerado ${idx + 1}`,
    color: '#16a34a',
    source: 'generated',
    sourceLayerId: layer.source_layer_id || '',
    generatedFrom: layer.generated_from || '',
    features: (layer.features || []).map(f => ({ ...f, _source: 'generated', _layerId: layer.id })),
    count: Number(layer.features_count ?? layer.features?.length ?? 0),
    downloadUrl: layer.download_url || `/api/kmz/generated/layers/${encodeURIComponent(layer.id)}/download`,
    updateUrl: layer.update_url || `/api/kmz/generated/layers/${encodeURIComponent(layer.id)}`,
    deleteUrl: `/api/kmz/generated/layers/${encodeURIComponent(layer.id)}`,
  })).filter(layer =>
    layer.generatedFrom !== 'imported-layer-download'
    && !importedLayerIds.has(layer.sourceLayerId)
    && !importedSignatures.has(mapLayerSignature(layer.features))
    && !mapLayerOverlapsImported(layer, importedPointSets)
  );

  const results = [
    ...importedLayers.filter(r => r.count > 0),
    ...generatedLayers.filter(r => r.count > 0),
  ];
  _mapFeatures = results.flatMap(layer => (layer.features || []).map(f => ({ ...f, _source: f._source || layer.source, _layerId: layer.layerId || layer.id })));

  // Inicializa grupos de camadas
  Object.values(_mapLayerGroups || {}).forEach(state => {
    if (state?.active && state?.group) {
      try { _map.removeLayer(state.group); } catch {}
    }
  });
  _mapLayerGroups = {};
  results.forEach(r => {
    _mapLayerGroups[r.id] = { features: r.features, group: L.layerGroup(), active: false, color: r.color, def: r };
  });

  listEl.innerHTML = '';

  if (results.every(r => r.count === 0)) {
    listEl.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:12px;text-align:center">Nenhuma camada disponivel.<br><small>Importe um KMZ ou gere o mapa.</small></div>';
    return;
  }

  const controls = document.createElement('div');
  controls.className = 'map-layer-bulk';
  controls.innerHTML = `
    <button type="button" class="map-layer-bulk-btn" data-map-layer-bulk="all"><i data-lucide="check-square"></i> Todas</button>
    <button type="button" class="map-layer-bulk-btn" data-map-layer-bulk="none"><i data-lucide="square"></i> Nenhuma</button>`;
  listEl.appendChild(controls);

  results.forEach(def => {
    const stats = mapLayerStats(def.features, camIndex);
    const btn = document.createElement('label');
    btn.className = 'map-layer-btn';
    btn.dataset.layerId = def.id;
    btn.title = def.label;
    btn.innerHTML = `
      <input type="checkbox" class="map-layer-check" data-layer-check="${esc(def.id)}">
      <span class="layer-dot" style="background:${def.color}"></span>
      <span class="map-layer-name">${esc(def.label)}</span>
      <span class="map-layer-badge">${def.count} pts</span>`;
    btn.querySelector('input')?.addEventListener('change', () => toggleMapLayer(def.id, def));

    // Botao excluir
    const delBtn = document.createElement('button');
    delBtn.title = 'Excluir camada';
    delBtn.className = 'map-layer-action danger';
    delBtn.innerHTML = '<i data-lucide="trash-2"></i>';
    delBtn.onclick = async (e) => {
      e.stopPropagation();
      const ok = await showConfirm({ title: 'Excluir camada', msg: `Remover "${def.label}" do mapa e do servidor?`, label: 'Excluir' });
      if (!ok) return;
      // Remove do mapa
      const state = _mapLayerGroups[def.id];
      if (state?.active) { _map.removeLayer(state.group); state.active = false; }
      delete _mapLayerGroups[def.id];
      if (def.deleteUrl) {
        await api(def.deleteUrl, { method: 'DELETE' }).catch(() => {});
      }
      btn.closest('.map-layer-card')?.remove();
      setText('mapCounter', 'Camada removida');
      showToast(`"${def.label}" removida do mapa.`);
    };

    const row = document.createElement('div');
    row.className = 'map-layer-card';
    row.dataset.layerCardId = def.id;
    row.appendChild(btn);

    const statsEl = document.createElement('div');
    statsEl.className = 'map-layer-stats';

    const infraStats = [
      stats.fiber ? `<span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#0ea5e9"></span><strong>${stats.fiber}</strong> fibra</span>` : '',
      stats.boxes ? `<span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#f59e0b"></span><strong>${stats.boxes}</strong> caixas</span>` : '',
      stats.poles ? `<span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#64748b"></span><strong>${stats.poles}</strong> postes</span>` : '',
      stats.cto ? `<span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#1971c2"></span><strong>${stats.cto}</strong> CTO</span>` : '',
      stats.cdo ? `<span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#7950f2"></span><strong>${stats.cdo}</strong> CDO</span>` : '',
    ].filter(Boolean).join('');
    statsEl.innerHTML = `
      <span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#16a34a"></span><strong>${stats.online}</strong> online</span>
      <span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#dc2626"></span><strong>${stats.offline}</strong> offline</span>
      <span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#64748b"></span><strong>${stats.cameras}</strong> cameras</span>
      ${infraStats}
      <span class="map-layer-stat"><span class="map-layer-stat-dot" style="background:#d97706"></span><strong>${stats.outros}</strong> outros</span>`;

    try {
      const fora = camerasForaDoMapa(def);
      if (fora.semInventario) {
        // Sem inventario neste modo, os numeros acima vieram do texto do KMZ --
        // uma foto do dia em que o mapa foi gerado, nao o estado de agora.
        const alerta = document.createElement('div');
        alerta.style.cssText = 'font-size:11px;margin-top:6px;padding:5px 8px;border-radius:6px;'
          + 'background:rgba(220,38,38,.10);color:#dc2626;font-weight:600';
        alerta.textContent = 'Sem inventario neste modo - os numeros acima vem do arquivo KMZ '
          + 'e podem estar desatualizados. Troque o modo no seletor do topo.';
        alerta.title = 'A camada compara cada ponto com o inventario do modo escolhido. '
          + 'Nesse modo nao ha camera cadastrada, entao nada foi conferido.';
        statsEl.appendChild(alerta);
      }
      if (fora.total) {
        const aviso = document.createElement('div');
        aviso.style.cssText = 'font-size:11px;margin-top:6px;padding:5px 8px;border-radius:6px;'
          + (fora.offline
              ? 'background:rgba(220,38,38,.10);color:#dc2626;font-weight:600'
              : 'background:rgba(217,119,6,.10);color:#b45309');
        aviso.textContent = fora.offline
          ? `${fora.total} camera(s) do inventario sem ponto no mapa - ${fora.offline} delas offline`
          : `${fora.total} camera(s) do inventario sem ponto no mapa`;
        aviso.title = 'Clique para ver quais sao. Elas existem no inventario mas nao foram '
          + 'desenhadas neste KMZ, entao nao entram na contagem acima.';
        if (fora.lista?.length) {
          aviso.style.cursor = 'pointer';
          aviso.style.textDecoration = 'underline';
          aviso.addEventListener('click', ev => {
            ev.preventDefault();
            ev.stopPropagation();   // nao ligar/desligar a camada ao clicar no aviso
            abrirDrawerCamerasFora(fora.site, fora.lista);
          });
        }
        statsEl.appendChild(aviso);
      }
    } catch (e) {
      console.warn('aviso de cameras fora do mapa falhou:', e);
    }
    row.appendChild(statsEl);

    // Menu unico por camada: com quatro ou mais acoes soltas o card ficava
    // poluido, e ainda vao entrar as de edicao de ponto.
    const menuWrap = document.createElement('div');
    menuWrap.className = 'map-layer-menu-wrap';

    const menuBtn = document.createElement('button');
    menuBtn.className = 'map-layer-action map-layer-menu-btn';
    menuBtn.title = 'Acoes da camada';
    menuBtn.innerHTML = '<i data-lucide="more-vertical"></i>';

    const menu = document.createElement('div');
    menu.className = 'map-layer-menu';
    menu.hidden = true;

    const item = (icone, rotulo, aoClicar, perigo = false) => {
      const b = document.createElement('button');
      b.className = 'map-layer-menu-item' + (perigo ? ' danger' : '');
      b.innerHTML = `<i data-lucide="${icone}"></i><span>${esc(rotulo)}</span>`;
      b.onclick = (e) => { e.stopPropagation(); menu.hidden = true; aoClicar(); };
      return b;
    };

    menu.appendChild(item('list', 'Detalhes', () => openMapLayerDetails(def)));
    if (def.updateUrl) {
      menu.appendChild(item('pencil', 'Renomear camada', () => openMapLayerRename(def)));
      menu.appendChild(item(
        mapEditIsActive(def.id) ? 'x' : 'move',
        mapEditIsActive(def.id) ? 'Sair do modo de edicao' : 'Editar pontos no mapa',
        () => mapEditIsActive(def.id) ? exitMapPointEditor() : openMapPointEditor(def),
      ));
    }
    const dlUrl = mapLayerDownloadUrl(def);
    if (dlUrl) {
      const rotulo = def.source === 'imported' ? 'Baixar KMZ enriquecido' : 'Baixar KMZ';
      menu.appendChild(item('download', rotulo, () => downloadWithAuth(dlUrl, `${def.label || 'mapa'}.kmz`)));
    }
    menu.appendChild(document.createElement('hr'));
    menu.appendChild(item('trash-2', 'Excluir camada', () => delBtn.onclick(new Event('click')), true));

    menuBtn.onclick = (e) => {
      e.stopPropagation();
      // fecha os menus das outras camadas: dois abertos ao mesmo tempo confunde
      document.querySelectorAll('.map-layer-menu').forEach(m => { if (m !== menu) m.hidden = true; });
      const abrir = menu.hidden;
      menu.hidden = !abrir;
      if (!abrir) return;
      // O card tem overflow:hidden e cortava o menu. Com position:fixed ele
      // escapa de qualquer recorte; a posicao vem do botao.
      const r = menuBtn.getBoundingClientRect();
      menu.style.top = `${Math.round(r.bottom + 4)}px`;
      menu.style.left = `${Math.round(Math.max(8, r.right - 220))}px`;
      lucide.createIcons();
    };

    menuWrap.appendChild(menuBtn);
    menuWrap.appendChild(menu);
    // Fora do fluxo, no canto do card: dentro do cabecalho ele encolhia o
    // nome da camada, que virava "JARDI...".
    row.appendChild(menuWrap);
    listEl.appendChild(row);
  });

  controls.querySelector('[data-map-layer-bulk="all"]')?.addEventListener('click', async () => {
    for (const def of results) {
      const state = _mapLayerGroups[def.id];
      if (state && !state.active) await toggleMapLayer(def.id, def, true);
    }
  });
  controls.querySelector('[data-map-layer-bulk="none"]')?.addEventListener('click', () => {
    results.forEach(def => {
      const state = _mapLayerGroups[def.id];
      if (state?.active) toggleMapLayer(def.id, def, true);
    });
  });

  lucide.createIcons();

  const idsToRestore = previousActive.size ? [...previousActive].filter(id => _mapLayerGroups[id]) : [results[0]?.id].filter(Boolean);
  for (const id of idsToRestore) {
    const def = results.find(r => r.id === id);
    if (def) await toggleMapLayer(id, def, true);
  }
}

function renderMapLayerGroup(id, def, skipFit = false) {
  const state = _mapLayerGroups[id];
  if (!state) return;

  // Renderiza os pontos neste grupo
  state.group = L.layerGroup();
  state.markers = {};
  const bounds = [];
  let drawnCount = 0;
  const editing = mapEditIsActive(id);
  state.features.forEach(f => {
      const geomType = String(f.geometry?.type || '');
      const name = f.properties?.name || '';
      const cam = mapFindCamera(f, _mapCameraIndex);
      const pointType = mapFeatureType(f, cam);
      const statusFilter = document.getElementById('mapFilterStatus')?.value || '';
      const typeFilter = document.getElementById('mapFilterType')?.value || '';
      const siteFilter = document.getElementById('mapFilterSite')?.value || '';
      const featureStatus = mapFeatureStatus(f, cam);
      const featureLocal = mapFeatureLocal(f, cam);
      if (typeFilter && pointType !== typeFilter) return;
      if (statusFilter && (pointType !== 'camera' || featureStatus !== statusFilter)) return;
      if (siteFilter && featureLocal !== siteFilter) return;

      if (geomType === 'LineString') {
        const coords = Array.isArray(f.geometry.coordinates) ? f.geometry.coordinates : [];
        const latlngs = coords
          .filter(c => Array.isArray(c) && c.length >= 2 && !isNaN(+c[0]) && !isNaN(+c[1]))
          .map(c => [+c[1], +c[0]]);
        if (latlngs.length < 2) return;
        const line = L.polyline(latlngs, {
          color: pointType === 'fiber' ? '#0ea5e9' : (def?.color || '#d97706'),
          weight: pointType === 'fiber' ? 5 : 3,
          opacity: 0.9,
        });
        line.bindPopup(`<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:10px;min-width:180px"><strong>${esc(name || 'Linha')}</strong><div style="font-size:12px;color:#64748b;margin-top:6px">${pointType === 'fiber' ? 'Fibra optica' : 'Linha do KMZ'}</div></div>`);
        const featureKey = mapFeatureKey(f, cam);
        if (!state.markers) state.markers = {};
        state.markers[featureKey] = line;
        state.group.addLayer(line);
        latlngs.forEach(ll => bounds.push(ll));
        drawnCount += 1;
        return;
      }

      if (geomType !== 'Point') return;
      const [lng, lat] = f.geometry.coordinates;
      if (lat == null || lng == null || isNaN(+lat) || isNaN(+lng)) return;

      const isOnlinePop = cam?.status
        ? String(cam.status).toLowerCase() === 'online'
        : String(f.properties?.description || '').toUpperCase().includes('ONLINE');
      const statusColor = isOnlinePop ? '#16a34a' : '#dc2626';

      const typeConfig = {
        camera: { bg: statusColor, label: '' },
        cto:    { bg: '#1971c2', label: 'CTO' },
        cdo:    { bg: '#7950f2', label: 'CDO' },
        pole:   { bg: '#64748b', label: 'PT' },
        box:    { bg: '#f59e0b', label: 'CX' },
        fiber:  { bg: '#0ea5e9', label: 'FO' },
        other:  { bg: def?.color || '#d97706', label: '' },
      };
      const tc = typeConfig[pointType] || typeConfig.other;
      // Em modo de edicao, os pontos de camera ganham draggable: true (ver
      // editar_ponto_no_kmz no backend, que casa o Placemark pelo nome do
      // proprio ponto e regrava a coordenada preservando estilo/descricao).
      const canDrag = editing && pointType === 'camera' && !!mapFeatureName(f) && !!def?.updateUrl;
      const icon = L.divIcon({
        html: `<div style="background:${tc.bg};color:white;border:2px solid white;border-radius:6px;padding:2px 5px;font-size:10px;font-weight:700;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.4);cursor:${canDrag ? 'move' : 'pointer'}${canDrag ? ';outline:2px dashed rgba(255,255,255,.8);outline-offset:2px' : ''}">${tc.label}</div>`,
        className: '', iconSize: [40, 22], iconAnchor: [20, 11], popupAnchor: [0, -14],
      });
      const marker = L.marker([+lat, +lng], { icon, draggable: canDrag });
      if (canDrag) {
        marker.on('dragend', () => handleMapPointDragEnd(def, mapFeatureName(f), marker));
      }
      const featureKey = mapFeatureKey(f, cam);
      if (!state.markers) state.markers = {};
      // Popup rico e moderno
      const [lng2, lat2] = f.geometry.coordinates;

      const row = (icon, label, value, mono = false) =>
        value ? `<div style="display:flex;align-items:baseline;gap:6px;padding:4px 0;border-bottom:1px solid #f1f3f5">
          <span style="color:#868e96;font-size:12px;flex-shrink:0">${icon}</span>
          <span style="color:#868e96;font-size:11px;min-width:56px;flex-shrink:0">${label}</span>
          <span style="font-size:12px;font-weight:500;word-break:break-all;${mono?'font-family:monospace;font-size:11px;':''}">${value}</span>
        </div>` : '';

      const snapHtml = cam?.snapshot_url
        ? `<div style="position:relative;overflow:hidden;border-radius:8px 8px 0 0;margin-bottom:12px">
            <img src="${API_BASE}${esc(cam.snapshot_url)}" style="width:100%;height:160px;object-fit:cover;display:block">
            <div style="position:absolute;bottom:0;left:0;right:0;padding:10px 14px;background:linear-gradient(transparent,rgba(0,0,0,.8))">
              <div style="color:white;font-size:14px;font-weight:700">${esc(name)}</div>
              <div style="color:${isOnlinePop?'#69db7c':'#ff8787'};font-size:12px;font-weight:600">${isOnlinePop?' ONLINE':' OFFLINE'}</div>
            </div>
          </div>`
        : `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;padding-bottom:8px;border-bottom:2px solid #f1f3f5">
            <strong style="font-size:14px">${esc(name)}</strong>
            <span style="background:${statusColor}22;color:${statusColor};font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px">${isOnlinePop?'ONLINE':'OFFLINE'}</span>
          </div>`;

      marker.bindPopup(`
        <div style="width:280px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:14px;overflow:hidden">
          ${snapHtml}
          ${row('','Camera', cam?.model || cam?.fabricante)}
          ${row('','Local',  cam?.local)}
          ${row('','IP', cam?.ip ? `<a href="http://${esc(cam.ip)}" target="_blank" style="color:#1971c2">${esc(cam.ip)}</a>` : '')}
          ${row('','MAC',    cam?.mac, true)}
          ${row('','PON/ONU', cam?.pon || cam?.onu_id ? [cam?.pon,cam?.onu_id].filter(Boolean).join(' / ') : '')}
          ${row('','ONU Serial', cam?.onu_serial, true)}
          <div style="margin-top:8px;padding-top:6px;font-size:10px;color:#adb5bd;font-family:monospace;word-break:break-all">
             ${(+lat2).toFixed(7)}, ${(+lng2).toFixed(7)}
          </div>
          ${cam?.ip ? `<div style="margin-top:10px;display:flex;gap:6px">
            <a href="http://${esc(cam.ip)}" target="_blank" style="flex:1;text-align:center;padding:6px;background:#e7f5ff;color:#1971c2;border-radius:6px;font-size:11px;font-weight:600;text-decoration:none;border:1px solid #a5d8ff">Abrir camera</a>
          </div>` : ''}
          ${canDrag ? `<div style="margin-top:8px;display:flex;gap:6px">
            <button type="button" data-map-point-rename style="flex:1;text-align:center;padding:6px;background:#fff3bf;color:#997404;border-radius:6px;font-size:11px;font-weight:600;border:1px solid #ffe066;cursor:pointer">Renomear ponto</button>
            <button type="button" data-map-point-delete style="flex:1;text-align:center;padding:6px;background:#ffe3e3;color:#c92a2a;border-radius:6px;font-size:11px;font-weight:600;border:1px solid #ffc9c9;cursor:pointer">Excluir ponto</button>
          </div>` : ''}
        </div>`, { maxWidth: 320, className: 'sightops-popup' });
      if (canDrag) {
        marker.on('popupopen', () => {
          const el = marker.getPopup()?.getElement();
          const nomePonto = mapFeatureName(f);
          el?.querySelector('[data-map-point-rename]')?.addEventListener('click', () => openMapPointRename(def, nomePonto));
          el?.querySelector('[data-map-point-delete]')?.addEventListener('click', () => handleMapPointDelete(def, nomePonto));
        });
      }
      state.markers[featureKey] = marker;
      state.group.addLayer(marker);
      bounds.push([+lat, +lng]);
      drawnCount += 1;
  });
  _map.addLayer(state.group);
  state.active = true;
  state.drawnCount = drawnCount;
  state.def = def;

  if (bounds.length > 0 && !skipFit) {
    try { _map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 }); } catch {}
  }
}

// Atualiza botao/contador visual de uma camada sem mexer nos marcadores --
// usado tanto por toggleMapLayer quanto por reRenderMapLayerGroup (que
// reconstroi os marcadores sem ligar/desligar a camada).
function updateMapLayerUiState(id) {
  const state = _mapLayerGroups[id];
  if (!state) return;
  const btn = document.querySelector(`[data-layer-id="${id}"]`);
  if (btn) btn.classList.toggle('active', state.active);
  const chk = document.querySelector(`[data-layer-check="${CSS.escape(id)}"]`);
  if (chk) chk.checked = !!state.active;
  const card = document.querySelector(`[data-layer-card-id="${id}"]`);
  if (card) card.classList.toggle('active', state.active);

  const totalShown = Object.values(_mapLayerGroups).filter(s => s.active).reduce((a, s) => a + (s.drawnCount ?? s.group?.getLayers?.().length ?? 0), 0);
  setText('mapCounter', `${totalShown} ponto${totalShown !== 1 ? 's' : ''} visiveis`);
}

async function toggleMapLayer(id, def, skipFit = false) {
  if (!_map) return;
  const state = _mapLayerGroups[id];
  if (!state) return;

  if (state.active) {
    _map.removeLayer(state.group);
    state.group.clearLayers();
    state.active = false;
    state.drawnCount = 0;
  } else {
    renderMapLayerGroup(id, def, skipFit);
  }

  updateMapLayerUiState(id);
}

// Reconstroi os marcadores de uma camada ja ativa (usado ao entrar/sair do
// modo de edicao, onde so muda se os pontos de camera sao draggable, e apos
// salvar um ponto, quando o layerGroup precisa refletir a coordenada nova).
function reRenderMapLayerGroup(id, def, skipFit = true) {
  const state = _mapLayerGroups[id];
  if (!state) return;
  if (state.active && state.group) {
    try { _map.removeLayer(state.group); } catch {}
  }
  renderMapLayerGroup(id, def, skipFit);
  updateMapLayerUiState(id);
}

// --- Modo de edicao de pontos no mapa ------------------------------------
// Liga o modo de edicao para uma camada: os pontos de camera ja existentes
// ganham draggable (soltar = mover, PATCH com o mesmo nome), e a lista de
// cameras do inventario sem ponto vira clicavel (escolher -> clicar no mapa
// = criar). Uma camada por vez para nao confundir qual "clique no mapa"
// pertence a qual.
async function openMapPointEditor(def) {
  if (!def?.updateUrl) {
    showToast('Esta camada nao suporta edicao de pontos.', true);
    return;
  }
  if (_mapEditLayerId && _mapEditLayerId !== def.id) {
    await exitMapPointEditor({ silent: true });
  }
  _mapEditLayerId = def.id;
  _mapEditDef = def;
  _mapEditPendingCam = null;

  const state = _mapLayerGroups[def.id];
  if (state && !state.active) {
    await toggleMapLayer(def.id, def, true);
  } else {
    reRenderMapLayerGroup(def.id, def, true);
  }

  if (!_mapEditClickHandler) {
    _mapEditClickHandler = (e) => handleMapEditClick(e);
    _map.on('click', _mapEditClickHandler);
  }

  renderMapEditBanner();
  showToast(`Modo de edicao ativo em "${def.label}". Arraste um ponto para mover ou escolha uma camera na lista para adicionar.`);
}

async function exitMapPointEditor({ silent = false } = {}) {
  const prevId = _mapEditLayerId;
  const prevDef = _mapEditDef;
  _mapEditLayerId = null;
  _mapEditDef = null;
  _mapEditPendingCam = null;
  if (_mapEditClickHandler) {
    _map.off('click', _mapEditClickHandler);
    _mapEditClickHandler = null;
  }
  if (prevId && _mapLayerGroups[prevId]?.active) {
    reRenderMapLayerGroup(prevId, prevDef || _mapLayerGroups[prevId].def, true);
  }
  renderMapEditBanner();
  if (!silent) showToast('Modo de edicao encerrado.');
}

// Ponto NOVO nao passa pela reescrita de descricao que a camada importada
// sofre no download-enriquecido (isso so acontece para camadas "imported" --
// ver _ensure_imported_layer_enriched no backend). Sem isso, um ponto criado
// aqui saia so com "IP: x.x.x.x" e sem a foto do imgbb. Monta a mesma cara do
// popup usado no restante do mapa (ver _popup_html em kmz_enricher.py) pra
// nao depender de reenriquecimento.
function mapEditBuildDescriptionHtml(cam) {
  const foto = cameraImgbbUrl(cam);
  const isOnline = String(cam?.status || '').toLowerCase() === 'online';
  const partes = [];
  if (foto) {
    partes.push(`<img src="${esc(foto)}" style="max-width:320px;width:100%;height:auto;border-radius:10px;display:block;"/><br/>`);
  }
  if (cam?.titulo) {
    partes.push(`<div style="font-weight:800;font-size:14px;margin:6px 0;color:#111827;">${esc(cam.titulo)}</div>`);
  }
  const linhas = [
    (cam?.modelo || cam?.model) ? `CAMERA: ${esc(cam.modelo || cam.model)}` : '',
    cam?.local ? `LOCAL: ${esc(cam.local)}` : '',
    cam?.mac ? `MAC: ${esc(cam.mac)}` : '',
    cam?.ip ? `IP: ${esc(cam.ip)}` : '',
    `STATUS: <span style="color:${isOnline ? '#16a34a' : '#dc2626'};font-weight:800">${isOnline ? 'ONLINE' : 'OFFLINE'}</span>`,
  ].filter(Boolean).join('<br/>');
  partes.push(linhas);
  return partes.join('\n');
}

async function handleMapEditClick(e) {
  if (!_mapEditPendingCam || !_mapEditLayerId) return;
  const def = _mapLayerGroups[_mapEditLayerId]?.def || _mapEditDef;
  if (!def?.updateUrl) return;
  const cam = _mapEditPendingCam;
  _mapEditPendingCam = null;
  renderMapEditBanner();
  await saveMapPoint(def, {
    nome: cam.titulo || cam.ip,
    lat: e.latlng.lat,
    lon: e.latlng.lng,
    descricao: mapEditBuildDescriptionHtml(cam),
  }, `Ponto criado para "${cam.titulo || cam.ip}".`);
}

async function handleMapPointDragEnd(def, nome, marker) {
  if (!nome) {
    showToast('Nao foi possivel identificar o nome deste ponto.', true);
    return;
  }
  const latlng = marker.getLatLng();
  await saveMapPoint(def, { nome, lat: latlng.lat, lon: latlng.lng }, `Ponto "${nome}" movido.`);
}

let _mapPointRenameTarget = null;

function openMapPointRename(def, nome) {
  if (!def?.updateUrl || !nome) return;
  _mapPointRenameTarget = { def, nome };
  const modal = document.getElementById('modalMapPointRename');
  const input = document.getElementById('mapPointRenameInput');
  if (!modal || !input) return;
  input.value = nome;
  modal.classList.remove('hidden');
  setTimeout(() => { input.focus(); input.select(); }, 30);
  lucide.createIcons();
}

function closeMapPointRename() {
  document.getElementById('modalMapPointRename')?.classList.add('hidden');
  _mapPointRenameTarget = null;
}

function handleMapPointRenameKey(event) {
  if (event.key === 'Escape') { closeMapPointRename(); return; }
  if (event.key === 'Enter') { event.preventDefault(); saveMapPointRename(); }
}

async function saveMapPointRename() {
  const alvo = _mapPointRenameTarget;
  if (!alvo) return;
  const input = document.getElementById('mapPointRenameInput');
  const novoNome = String(input?.value || '').trim();
  if (!novoNome) {
    showToast('Informe o nome do ponto.', true);
    return;
  }
  closeMapPointRename();
  if (novoNome === alvo.nome) return;
  await saveMapPoint(alvo.def, { nome: alvo.nome, novo_nome: novoNome }, `Ponto renomeado para "${novoNome}".`);
}

async function handleMapPointDelete(def, nome) {
  if (!def?.updateUrl || !nome) return;
  const ok = await showConfirm({
    title: 'Excluir ponto',
    msg: `Remover o ponto "${nome}" do mapa? A camera continua no inventario, so o pino some do KMZ.`,
    label: 'Excluir',
  });
  if (!ok) return;
  await saveMapPoint(def, { nome, remover: true }, `Ponto "${nome}" removido.`);
}

async function saveMapPoint(def, ponto, successMsg) {
  if (!def?.updateUrl) return;
  try {
    const res = await api(def.updateUrl, { method: 'PATCH', body: JSON.stringify({ ponto }) });
    const body = await res?.json().catch(() => ({}));
    if (!res?.ok || body?.ok === false) {
      showToast(body?.detail || body?.error || 'Nao foi possivel salvar o ponto.', true);
    } else {
      showToast(successMsg || 'Ponto salvo.');
    }
  } catch (err) {
    showToast(`Falha ao salvar ponto: ${err.message || err}`, true);
  }
  await reloadMapEditLayer();
}

// Recarrega as camadas do servidor (o KMZ mudou) mantendo o modo de edicao
// ativo na mesma camada, para o aviso "sem ponto no mapa" e os marcadores
// atualizarem na hora sem o usuario precisar sair e reentrar no modo.
async function reloadMapEditLayer() {
  const editingId = _mapEditLayerId;
  await loadMapLayers();
  if (editingId && _mapLayerGroups[editingId]) {
    _mapEditDef = _mapLayerGroups[editingId].def;
  } else {
    _mapEditLayerId = null;
    _mapEditDef = null;
  }
  renderMapEditBanner();
}

function renderMapEditBanner() {
  const panel = document.getElementById('mapEditPanel');
  if (!panel) return;

  if (!_mapEditLayerId) {
    panel.classList.add('hidden');
    panel.innerHTML = '';
    return;
  }

  const state = _mapLayerGroups[_mapEditLayerId];
  const def = state?.def || _mapEditDef;
  if (!def) {
    panel.classList.add('hidden');
    panel.innerHTML = '';
    return;
  }

  panel.classList.remove('hidden');

  const fora = camerasForaDoMapa(def);
  const lista = fora.lista || [];

  const pendingHtml = _mapEditPendingCam
    ? `<div class="map-edit-pending">
        Clique no mapa para posicionar <strong>${esc(_mapEditPendingCam.titulo || _mapEditPendingCam.ip)}</strong>.
        <button type="button" class="map-edit-cancel-pending" id="mapEditCancelPending">Cancelar</button>
      </div>`
    : '';

  const listHtml = lista.length
    ? lista.map(c => `
        <button type="button" class="map-edit-cam-item" data-map-edit-cam-ip="${esc(c.ip)}">
          <span class="map-edit-cam-dot" style="background:${String(c.status || '').toLowerCase() === 'online' ? '#16a34a' : '#dc2626'}"></span>
          <div><strong>${esc(c.titulo || c.ip)}</strong><span class="muted">${esc(c.ip)}</span></div>
        </button>`).join('')
    : '<div class="map-edit-empty">Todas as cameras do inventario ja tem ponto nesta camada.</div>';

  panel.innerHTML = `
    <div class="map-edit-header">
      <strong title="${esc(def.label || '')}">Editando: ${esc(def.label || '')}</strong>
      <button type="button" class="map-edit-exit" id="mapEditExitBtn" title="Sair do modo de edicao"><i data-lucide="x"></i></button>
    </div>
    <div class="map-edit-hint">Arraste um ponto de camera existente pra move-lo. Escolha uma camera na lista e clique no mapa pra adicionar.</div>
    ${pendingHtml}
    <div class="map-edit-cam-list-title">Sem ponto no mapa</div>
    <div class="map-edit-cam-list">${listHtml}</div>`;

  document.getElementById('mapEditExitBtn')?.addEventListener('click', () => exitMapPointEditor());
  document.getElementById('mapEditCancelPending')?.addEventListener('click', () => {
    _mapEditPendingCam = null;
    renderMapEditBanner();
  });
  panel.querySelectorAll('[data-map-edit-cam-ip]').forEach(btn => {
    btn.addEventListener('click', () => {
      const ip = btn.dataset.mapEditCamIp;
      const cam = lista.find(c => c.ip === ip);
      if (cam) {
        _mapEditPendingCam = cam;
        renderMapEditBanner();
      }
    });
  });

  lucide.createIcons();
}

function openMapLayerDetails(def) {
  const modal = document.getElementById('modalMapLayerDetails');
  const title = document.getElementById('mapLayerDetailsTitle');
  const summary = document.getElementById('mapLayerDetailsSummary');
  const list = document.getElementById('mapLayerDetailsList');
  if (!modal || !summary || !list) return;

  const stats = mapLayerStats(def.features, _mapCameraIndex);
  if (title) title.textContent = def.label || 'Detalhes da camada';
  summary.innerHTML = `
    <button class="map-detail-filter active" data-map-detail-filter="all"><span class="map-layer-stat-dot" style="background:#64748b"></span>${stats.total} itens</button>
    <button class="map-detail-filter" data-map-detail-filter="camera"><span class="map-layer-stat-dot" style="background:#16a34a"></span>${stats.cameras} cameras</button>
    <button class="map-detail-filter" data-map-detail-filter="online"><span class="map-layer-stat-dot" style="background:#16a34a"></span>${stats.online} online</button>
    <button class="map-detail-filter" data-map-detail-filter="offline"><span class="map-layer-stat-dot" style="background:#dc2626"></span>${stats.offline} offline</button>
    <span class="map-detail-pill"><span class="map-layer-stat-dot" style="background:#0ea5e9"></span>${stats.fiber} fibra</span>
    <span class="map-detail-pill"><span class="map-layer-stat-dot" style="background:#f59e0b"></span>${stats.boxes} caixas</span>
    <span class="map-detail-pill"><span class="map-layer-stat-dot" style="background:#64748b"></span>${stats.poles} postes</span>
    <label class="map-detail-search">
      <i data-lucide="search"></i>
      <input id="mapLayerDetailsSearch" type="search" placeholder="Buscar por nome, IP, local ou modelo">
    </label>`;

  const rows = (def.features || [])
    .map(f => {
      const cam = mapFindCamera(f, _mapCameraIndex);
      const type = mapFeatureType(f, cam);
      const status = mapFeatureStatus(f, cam);
      const name = cam?.titulo || mapFeatureName(f) || cam?.ip || mapTypeLabel(type);
      const ip = cam?.ip || mapExtractIp(f?.properties?.description) || '-';
      const local = mapFeatureLocal(f, cam) || '-';
      const color = type === 'camera'
        ? (status === 'online' ? '#16a34a' : status === 'offline' ? '#dc2626' : '#d97706')
        : ({ fiber: '#0ea5e9', pole: '#64748b', box: '#f59e0b', cto: '#1971c2', cdo: '#7950f2' }[type] || '#d97706');
      return {
        status,
      type,
      color,
      name,
      ip,
      local,
      key: mapFeatureKey(f, cam),
      layerId: def.id,
      model: cam?.modelo || cam?.model || cam?.fabricante || mapTypeLabel(type),
      search: [name, ip, local, cam?.modelo, cam?.model, cam?.fabricante, status, mapTypeLabel(type), type].filter(Boolean).join(' ').toLowerCase(),
      };
    })
    .filter(Boolean)
    .sort((a, b) => {
      const statusOrder = { offline: 0, online: 1, outros: 2 };
      return (statusOrder[a.status] ?? 3) - (statusOrder[b.status] ?? 3)
        || a.name.localeCompare(b.name, 'pt', { numeric: true });
    });

  list.innerHTML = rows.length
    ? rows.map(r => `
      <button class="map-detail-row" data-map-layer-id="${esc(r.layerId)}" data-map-feature-key="${esc(r.key)}" data-map-status="${esc(r.status)}" data-map-type="${esc(r.type)}" data-map-search="${esc(r.search)}" title="${esc(`${r.name} | ${r.ip} | ${r.local} | ${r.model}`)}">
        <span class="map-layer-stat-dot" style="background:${r.color}"></span>
        <div><strong>${esc(r.name)}</strong><span class="muted">${esc(r.model || r.status)}</span></div>
        <span class="monospace">${esc(r.ip)}</span>
        <span class="muted">${esc(r.local)}</span>
      </button>`).join('')
    : '<div style="padding:18px;text-align:center;color:var(--muted);font-size:13px">Nenhum item encontrado nessa camada.</div>';

  const applyDetailFilters = () => {
    const activeFilter = summary.querySelector('[data-map-detail-filter].active')?.dataset.mapDetailFilter || 'all';
    const q = (document.getElementById('mapLayerDetailsSearch')?.value || '').trim().toLowerCase();
    let visible = 0;
    list.querySelectorAll('.map-detail-row').forEach(row => {
      const statusOk = activeFilter === 'all'
        || row.dataset.mapStatus === activeFilter
        || row.dataset.mapType === activeFilter;
      const searchOk = !q || String(row.dataset.mapSearch || '').includes(q);
      row.hidden = !(statusOk && searchOk);
      if (!row.hidden) visible += 1;
    });
    let empty = list.querySelector('.map-detail-empty-filter');
    if (!empty) {
      empty = document.createElement('div');
      empty.className = 'map-detail-empty-filter';
      empty.textContent = 'Nenhum resultado para essa busca.';
      list.appendChild(empty);
    }
    empty.hidden = visible > 0 || !list.querySelector('.map-detail-row');
  };

  summary.querySelectorAll('[data-map-detail-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      summary.querySelectorAll('[data-map-detail-filter]').forEach(b => b.classList.toggle('active', b === btn));
      applyDetailFilters();
    });
  });
  document.getElementById('mapLayerDetailsSearch')?.addEventListener('input', applyDetailFilters);

  list.querySelectorAll('[data-map-feature-key]').forEach(row => {
    row.addEventListener('click', () => focusMapFeature(row.dataset.mapLayerId, row.dataset.mapFeatureKey));
  });

  modal.classList.remove('hidden');
  lucide.createIcons();
}

function closeMapLayerDetails() {
  document.getElementById('modalMapLayerDetails')?.classList.add('hidden');
}

async function focusMapFeature(layerId, featureKey) {
  if (!_map || !layerId || !featureKey) return;
  const state = _mapLayerGroups[layerId];
  if (!state) return;
  if (!state.active) {
    const def = state.def || MAP_LAYER_DEFS.find(d => d.id === layerId) || { id: layerId };
    await toggleMapLayer(layerId, def);
  }
  const marker = state.markers?.[featureKey];
  if (!marker) {
    showToast('Ponto nao encontrado no mapa.', true);
    return;
  }
  closeMapLayerDetails();
  if (typeof marker.getLatLng === 'function') {
    const latlng = marker.getLatLng();
    _map.flyTo(latlng, Math.max(_map.getZoom(), 18), { duration: 0.5 });
    setTimeout(() => marker.openPopup(), 550);
    return;
  }
  if (typeof marker.getBounds === 'function') {
    const bounds = marker.getBounds();
    _map.fitBounds(bounds, { padding: [40, 40], maxZoom: 18 });
    setTimeout(() => marker.openPopup(), 350);
  }
}

async function focusCameraOnMap(cam) {
  if (!cam) return;
  const targetIp = String(cam.ip || '').trim();
  const targetTitle = String(cam.titulo || cam.title || '').trim().toLowerCase();
  closeCamPanel();
  navigateTo('kmz');
  await new Promise(resolve => setTimeout(resolve, 350));
  if (!_map || !Object.keys(_mapLayerGroups || {}).length) {
    await loadKmz();
  } else {
    await loadMapLayers();
  }

  let found = null;
  for (const [layerId, state] of Object.entries(_mapLayerGroups || {})) {
    const feature = (state.features || []).find(f => {
      const matchedCam = mapFindCamera(f, _mapCameraIndex);
      const featureIp = matchedCam?.ip || mapExtractIp(f?.properties?.description) || mapExtractIp(mapFeatureName(f));
      const featureTitle = String(matchedCam?.titulo || mapFeatureName(f) || '').trim().toLowerCase();
      return (targetIp && featureIp === targetIp) || (targetTitle && featureTitle === targetTitle);
    });
    if (feature) {
      const matchedCam = mapFindCamera(feature, _mapCameraIndex) || cam;
      found = { layerId, key: mapFeatureKey(feature, matchedCam), def: state.def };
      break;
    }
  }

  if (!found) {
    showToast('Camera sem coordenada no mapa.', true);
    return;
  }

  const state = _mapLayerGroups[found.layerId];
  if (state && !state.active) {
    await toggleMapLayer(found.layerId, found.def || state.def, true);
  }
  await focusMapFeature(found.layerId, found.key);
}

function renderMapMarkers(camByName, camByIp) {
  // Remove camadas anteriores
  _mapLayers.forEach(l => _map.removeLayer(l));
  _mapLayers = [];

  const statusFilter = document.getElementById('mapFilterStatus')?.value || '';
  const siteFilter   = document.getElementById('mapFilterSite')?.value   || '';

  const cluster = L.layerGroup();

  const bounds = [];
  let shown = 0;

  console.log('[MAP] features total:', _mapFeatures.length, '| cluster type:', typeof cluster, typeof L.markerClusterGroup);

  _mapFeatures.forEach(f => {
    if (f.geometry?.type !== 'Point') return;
    const [lng, lat] = f.geometry?.coordinates || [];
    if (lat == null || lng == null || isNaN(+lat) || isNaN(+lng)) return;

    const props = f.properties || {};
    const name  = props.name || '';
    const desc  = props.description || '';

    // Busca dados da camera para popup e para status ao vivo (Zabbix), em vez
    // de confiar no texto estatico gravado no KMZ na hora que foi gerado.
    const cam = camByName[name.toLowerCase()] || Object.values(camByIp).find(c =>
      c.titulo?.toLowerCase() === name.toLowerCase()
    );
    const isOnline  = cam?.status ? String(cam.status).toLowerCase() === 'online' : desc.includes('ONLINE');
    const isOffline = cam?.status ? String(cam.status).toLowerCase() === 'offline' : (desc.includes('OFFLINE') || (!isOnline && desc.includes('STATUS')));
    const statusStr = isOnline ? 'online' : isOffline ? 'offline' : 'outros';

    if (statusFilter && statusStr !== statusFilter) return;
    if (siteFilter) {
      const m = desc.match(/LOCAL.*?>(.*?)</i);
      const local = m ? m[1].trim() : '';
      if (local !== siteFilter) return;
    }

    // Detecta tipo pelo nome -- se bate com uma camera real do inventario, e
    // camera, nao importa se o ponto veio de layer "gerado" ou importado.
    const nameLow = name.toLowerCase();
    let pointType = 'other';
    if (cam || !f._source || f._source === 'generated') {
      pointType = 'camera';
    } else if (/\bcto\b|^cto/i.test(name)) {
      pointType = 'cto';
    } else if (/\bcdo\b|^cdo|emenda|splice/i.test(name)) {
      pointType = 'cdo';
    } else if (/cam|camera|vip-|vipc|vip\s|\bcam\s/i.test(name)) {
      pointType = 'camera';
    }

    const typeConfig = {
      camera: { bg: isOnline ? '#16a34a' : '#dc2626', label: '' },
      cto:    { bg: '#1971c2', label: 'CTO' },
      cdo:    { bg: '#7950f2', label: 'CDO' },
      other:  { bg: '#d97706', label: '' },
    };
    const tc = typeConfig[pointType] || typeConfig.other;

    const icon  = L.divIcon({
      html: `<div style="background:${tc.bg};color:white;border:2px solid white;border-radius:6px;padding:3px 5px;font-size:10px;font-weight:700;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.4);cursor:pointer">${tc.label}</div>`,
      className: '',
      iconSize: [40, 22],
      iconAnchor: [20, 11],
      popupAnchor: [0, -14],
    });

    const marker = L.marker([lat, lng], { icon });

    // Popup
    const isImported = f._source === 'imported';
    const snapHtml = cam?.snapshot_url
      ? `<img src="${API_BASE}${cam.snapshot_url}" style="width:100%;display:block;max-height:150px;object-fit:cover">`
      : isImported
        ? `<div style="width:100%;height:60px;background:#2d3748;display:flex;align-items:center;justify-content:center;color:#a0aec0;font-size:11px;gap:6px"> Ponto importado</div>`
        : `<div style="width:100%;height:80px;background:#1a1a2e;display:flex;align-items:center;justify-content:center;color:#4a5568;font-size:12px">Sem snapshot</div>`;

    const statusBadge = isOnline
      ? `<span style="color:#16a34a;font-weight:700"> online</span>`
      : `<span style="color:#dc2626;font-weight:700"> offline</span>`;

    marker.bindPopup(`
      <div style="width:220px">
        ${snapHtml}
        <div style="padding:10px 12px">
          <div style="font-weight:700;font-size:13px;margin-bottom:4px">${esc(name)}</div>
          <div style="font-size:11px;color:#666;margin-bottom:6px">${statusBadge}</div>
          ${cam?.ip ? `<div style="font-size:11px;color:#888;font-family:monospace">${esc(cam.ip)}</div>` : ''}
          ${cam?.local ? `<div style="font-size:11px;color:#888">${esc(cam.local)}</div>` : ''}
          <div style="margin-top:8px;display:flex;gap:6px">
            <a href="https://www.google.com/maps?q=${lat},${lng}" target="_blank"
               style="flex:1;text-align:center;padding:5px;background:#f1f5f9;border-radius:5px;font-size:11px;color:#374151;text-decoration:none">
               Google Maps
            </a>
            ${cam?.ip ? `<a href="http://${cam.ip}" target="_blank"
               style="flex:1;text-align:center;padding:5px;background:#f1f5f9;border-radius:5px;font-size:11px;color:#374151;text-decoration:none">
               Camera
            </a>` : ''}
          </div>
        </div>
      </div>`, { maxWidth: 240 });

    cluster.addLayer(marker);
    bounds.push([+lat, +lng]);
    shown++;
  });

  _map.addLayer(cluster);
  _mapLayers.push(cluster);

  console.log('[MAP] shown:', shown, '| bounds:', bounds.slice(0,2));
  setText('mapCounter', `${shown} ponto${shown !== 1 ? 's' : ''} no mapa`);

  // Ajusta view
  if (bounds.length > 0) {
    try { _map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 }); } catch {}
  }
}

async function loadInvOlt() {
  _camSessionLoad();
  const desired = _invOltView || 'olt';
  await _loadCamForMode(desired);
  updateCamTabs();
  populateCamSiteFilter();
  applyInvOltFilters();
  startCamAutoRefresh();
}

// Enquanto a tela de Cameras IP estiver aberta, os dados envelhecem: a
// telemetria da OLT (estado da ONU, sinal) atualiza a cada ~10 min no servidor
// e a tela so buscava ao entrar. Quem deixa o painel aberto o dia todo ficava
// vendo "ONU nao verificada" com o dado ja disponivel do outro lado.
let _camAutoRefresh = null;

async function _camRefreshSilencioso() {
  const tela = document.getElementById('viewInvOlt');
  if (!tela || tela.classList.contains('hidden') || document.hidden) return;
  try {
    const modo = _invOltView || 'olt';
    await _loadCamForMode(modo);
    applyInvOltFilters();
    // se o painel lateral estiver aberto, atualiza o que ele mostra
    const painel = document.getElementById('camPanel');
    if (painel && !painel.classList.contains('hidden') && _invOltActive) {
      const atual = _invOltAll_get().find(c => c.ip === _invOltActive.ip);
      if (atual) openCamPanel(atual);
    }
  } catch {}
}

function startCamAutoRefresh(intervaloMs = 120000) {
  stopCamAutoRefresh();
  _camAutoRefresh = setInterval(_camRefreshSilencioso, intervaloMs);
  // voltar pra aba tambem merece um dado fresco
  document.addEventListener('visibilitychange', _camRefreshSilencioso);
}

function stopCamAutoRefresh() {
  if (_camAutoRefresh) { clearInterval(_camAutoRefresh); _camAutoRefresh = null; }
  document.removeEventListener('visibilitychange', _camRefreshSilencioso);
}

async function _loadCamForMode(mode) {
  const inventoryMode = mode === 'switch' ? 'switch' : mode === 'basico' ? 'basico' : 'olt';
  const cacheBust = Date.now();
  const [camData, swData, oltData] = await Promise.all([
    apiJson(`/api/cameras?mode=${encodeURIComponent(inventoryMode)}&_=${cacheBust}`, { forceRefresh: true }),
    mode === 'switch' ? apiJson(`/api/switch/rows?_=${cacheBust}`, { forceRefresh: true }) : Promise.resolve(null),
    mode === 'olt'    ? apiJson(`/api/olt/rows?compact=true&_=${cacheBust}`, { forceRefresh: true }) : Promise.resolve(null),
  ]);

  let cameras = camData?.cameras || (Array.isArray(camData) ? camData : []);

  if (mode === 'switch' && swData) {
    // MAC aprendido so na porta uplink e trafego de equipamento atras do
    // switch (outro segmento), nao a porta fisica real da camera -- fora do
    // casamento (mesmo criterio da tela Switch e do enrich no backend).
    const swByMac = {};
    (swData?.rows || []).forEach(r => {
      if (r.mac && r.port_role_guess !== 'uplink') swByMac[r.mac.toLowerCase()] = r;
    });
    cameras = cameras.map(c => {
      const sw = swByMac[(c.mac||'').toLowerCase()] || null;
      return { ...c,
        switch_name: sw ? (sw.switch_name || '') : (c.switch_name || ''),
        switch_ip:   sw ? (sw.switch_ip || '')   : (c.switch_ip   || ''),
        switch_port: sw ? (sw.port || '')        : (c.switch_port || ''),
        switch_vlan: sw ? (sw.vlan || '')        : (c.switch_vlan || ''),
      };
    });
  }

  if (mode === 'olt' && oltData) {
    const oltByMac = {};
    (oltData?.rows || []).forEach(r => { if (r.cpe_mac) oltByMac[r.cpe_mac.toLowerCase()] = r; });
    cameras = cameras.map(c => {
      const olt = oltByMac[(c.mac||'').toLowerCase()] || {};
      return { ...c,
        pon:        olt.pon        || c.pon        || '',
        onu_id:     olt.onu_id     || c.onu_id     || '',
        onu_name:   olt.onu_name   || c.onu_name   || '',
        onu_serial: olt.onu_serial || c.onu_serial || '',
        onu_oper_status: olt.oper_status || c.onu_oper_status || '',
        onu_omci_status: olt.omci_status || c.onu_omci_status || '',
        onu_rx: olt.onu_rx || c.onu_rx || '',
        olt_rx: olt.olt_rx || c.olt_rx || '',
        onu_telemetry_updated_at: olt.telemetry_updated_at || c.onu_telemetry_updated_at || '',
      };
    });
  }

  // Ordena por IP
  const ipToInt = ip => (ip||'0.0.0.0').split('.').reduce((a,b) => (a<<8)|(parseInt(b)||0), 0)>>>0;
  cameras.sort((a, b) => ipToInt(a.ip) - ipToInt(b.ip));

  _invCam[mode] = cameras;
  _camSessionSave(mode, cameras);
}

function populateCamSiteFilter() {
  const rows  = _invCam[_invOltView] || [];
  const sites = [...new Set(rows.map(c => c.local || c.site || c.site_name).filter(Boolean))].sort();
  const sel = document.getElementById('filterSiteOlt');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">Todos os sites</option>' +
    sites.map(s => `<option value="${esc(s)}"${s === current ? ' selected' : ''}>${esc(s)}</option>`).join('');
}

function matchesIpFilter(ip, query) {
  if (!query) return true;
  const q = query.trim();
  // lista: 10.0.0.1,10.0.0.2
  if (q.includes(',')) {
    return q.split(',').map(s => s.trim()).includes(ip);
  }
  // CIDR: 10.0.0.0/24
  if (q.includes('/')) {
    try {
      const [base, bits] = q.split('/');
      const mask = ~(0xffffffff >>> parseInt(bits)) >>> 0;
      const ipInt = ip.split('.').reduce((a, b) => (a << 8) | parseInt(b), 0) >>> 0;
      const baseInt = base.split('.').reduce((a, b) => (a << 8) | parseInt(b), 0) >>> 0;
      return (ipInt & mask) === (baseInt & mask);
    } catch { return false; }
  }
  // range: 10.0.0.1-10.0.0.50
  if (q.includes('-') && q.split('-').length === 2) {
    const [start, end] = q.split('-');
    const toInt = s => s.split('.').reduce((a, b) => (a << 8) | parseInt(b), 0) >>> 0;
    const ipInt = toInt(ip);
    try { return ipInt >= toInt(start) && ipInt <= toInt(end); } catch { /* fall through */ }
  }
  return false;
}

function _isBlankValue(value) {
  const s = String(value ?? '').trim();
  return !s || s === '' || s === '-' || /^n\/?a$/i.test(s);
}

function camHasMissingData(c) {
  const required = [c.ip, c.mac, c.fabricante, c.modelo || c.model, c.titulo, c.local];
  if (_invOltView === 'olt') required.push(c.pon, c.onu_id, c.onu_name, c.onu_serial);
  if (_invOltView === 'switch') required.push(c.switch_name, c.switch_ip, c.switch_port);
  return required.some(_isBlankValue);
}

function camHasDefaultTitle(c) {
  const title = String(c.titulo || '').trim().toLowerCase();
  if (_isBlankValue(title)) return true;
  return [
    /^c[aa]mera\s*\d{1,3}$/i,
    /^camera\s*\d{1,3}$/i,
    /^cam\s*\d{1,3}$/i,
    /camera\s*0?\d{1,3}/i,
    /c[aa]mera\s*0?\d{1,3}/i,
    /^ip\s*camera/i,
    /^hikvision/i,
    /hikvision/i,
    /^vip(?:-|_|\s|$)/i,
    /^vip\s*intelbras/i,
    /^intelbras\s*vip/i,
    /^vip-\d/i,
    /vip[-\s]?\d/i,
    // serial de fabrica que o proprio DVR/NVR reporta como nome de canal
    // quando ninguem nunca renomeou (ex: "7KOM0204255LX", "YP6K2403581TB") --
    // alfanumerico solido, sem espaco/hifen, MISTURANDO letra e digito
    // (exige pelo menos 1 digito, senao "PORTARIA"/"RECEPCAO" -- nome real
    // de uma palavra so, sem espaco -- cairia aqui por engano).
    /^(?=.*[0-9])[a-z0-9]{8,16}$/,
  ].some(rx => rx.test(title));
}

function camHasImgbbDown(c) {
  return !cameraImgbbUrl(c);
}

function camHasNoOltData(c) {
  return [c.pon, c.onu_id, c.onu_name, c.onu_serial].some(_isBlankValue);
}

let _camStatusRefreshTimer = null;
let _camStatusRefreshKey = '';

function scheduleFilteredCamStatusRefresh(rows, query) {
  const q = String(query || '').trim();
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(q)) {
    _camStatusRefreshKey = '';
    return;
  }
  // Status oficial vem do inventario/Zabbix. Ping automatico na busca e apenas
  // diagnostico e nao deve sobrescrever a tabela.
  _camStatusRefreshKey = q;
  clearTimeout(_camStatusRefreshTimer);
}

function applyInvOltFilters() {
  const q       = (document.getElementById('searchInvOlt')?.value || '').toLowerCase().trim();
  const status  = document.getElementById('filterStatusOlt')?.value || '';
  const site    = document.getElementById('filterSiteOlt')?.value || '';
  const normText = value => String(value || '')
    .toLowerCase()
    .replace(/[–—-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const qNorm = normText(q);

  const filtered = (_invCam[_invOltView] || []).filter(c => {
    if (status === 'missing_data' && !camHasMissingData(c)) return false;
    else if (status === 'default_title' && !camHasDefaultTitle(c)) return false;
    else if (status === 'imgbb_down' && !camHasImgbbDown(c)) return false;
    else if (status === 'no_olt' && !camHasNoOltData(c)) return false;
    else if (status && !['missing_data','default_title','imgbb_down','no_olt'].includes(status) && (c.status || '').toLowerCase() !== status) return false;
    if (site && ![c.local, c.site, c.site_name].some(v => String(v || '') === site)) return false;
    if (q) {
      // tenta match de IP com range/CIDR/lista
      const ipMatch = matchesIpFilter(c.ip || '', q);
      // ou texto livre em qualquer campo
      const textMatch = [
        c.ip, c.mac, c.fabricante, c.modelo, c.model, c.titulo, c.local,
        c.onu_name, c.onu_serial,
        c.switch_name, c.switch_ip, c.switch_port, c.switch_vlan,
        [c.switch_name, c.switch_ip, c.switch_port, c.switch_vlan].filter(Boolean).join(' '),
      ].some(f => {
        const raw = String(f || '').toLowerCase();
        return raw.includes(q) || (qNorm && normText(raw).includes(qNorm));
      });
      if (!ipMatch && !textMatch) return false;
    }
    return true;
  });

  // Ordena por IP crescente
  const ipToInt = ip => (ip || '0.0.0.0').split('.').reduce((a, b) => (a << 8) | (parseInt(b) || 0), 0) >>> 0;
  filtered.sort((a, b) => ipToInt(a.ip) - ipToInt(b.ip));

  renderInvOlt(filtered);
  scheduleFilteredCamStatusRefresh(filtered, q);
}

function renderInvOlt(cameras) {
  const def   = INV_COLS[_invOltView] || INV_COLS.basico;
  const ncols = def.cols.length;
  const tbody = document.getElementById('invOltTable');
  const table = document.getElementById('invOltTableEl');
  // Colunas em %: a tabela sempre cabe em 100% do container, sem forcar
  // min-width (isso e o que garante zero scroll horizontal).
  if (table) table.style.minWidth = def.minWidth || '';

  // Atualiza colgroup
  const colgroup = table.querySelector('colgroup');
  if (colgroup) colgroup.innerHTML = def.cols.map(w => `<col style="width:${w}">`).join('');

  // Atualiza thead
  const thead = table.querySelector('thead tr');
  if (thead) thead.innerHTML = def.heads.map((h, i) =>
    i === 0
      ? `<th><input type="checkbox" id="chkOltAll"></th>`
      : `<th>${h}</th>`
  ).join('');

  // Contadores
  const online  = cameras.filter(c => (c.status||'').toLowerCase() === 'online').length;
  const offline = cameras.filter(c => (c.status||'').toLowerCase() === 'offline').length;
  setText('invOltTotal',   cameras.length);
  setText('invOltOnline',  online);
  setText('invOltOffline', offline);
  setText('invOltOutros',  cameras.length - online - offline);
  setText('invOltFooter',  `${cameras.length} camera${cameras.length!==1?'s':''}`);

  if (!cameras.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${ncols}">Nenhum resultado.</td></tr>`;
    return;
  }

  tbody.innerHTML = cameras.map(c => {
    const cells = def.row(c);
    return `<tr class="inv-olt-row" data-ip="${esc(c.ip)}" style="cursor:pointer">
      ${cells.map((cell, i) =>
        i === 0
          ? `<td onclick="event.stopPropagation()">${cell}</td>`
          : `<td>${cell}</td>`
      ).join('')}
    </tr>`;
  }).join('');

  tbody.querySelectorAll('.inv-olt-row').forEach(tr => {
    tr.addEventListener('click', () => {
      const cam = _invOltAll_get().find(c => c.ip === tr.dataset.ip);
      if (cam) openCamPanel(cam);
    });
  });

  if (_pendingOpenCamIp) {
    const ip  = _pendingOpenCamIp;
    _pendingOpenCamIp = null;
    const cam = _invOltAll_get().find(c => c.ip === ip);
    if (cam) {
      const tr = tbody.querySelector(`[data-ip="${CSS.escape(ip)}"]`);
      if (tr) tr.scrollIntoView({ block: 'center', behavior: 'smooth' });
      openCamPanel(cam);
    }
  }

  document.getElementById('chkOltAll').onchange = function() {
    document.querySelectorAll('.chk-olt').forEach(c => c.checked = this.checked);
  };

  lucide.createIcons();
}

function isImgbbUrl(url) {
  if (!url) return false;
  return /imgbb\.com|ibb\.co/i.test(url);
}

function invStatusBadge(status) {
  if (!status) return '<span class="text-muted"></span>';
  const s = status.toLowerCase();
  if (s === 'online')      return `<span style="color:var(--primary);font-weight:600;font-size:12px">online</span>`;
  if (s === 'offline')     return `<span style="color:var(--danger);font-weight:600;font-size:12px">offline</span>`;
  if (s === 'auth_failed') return `<span style="color:var(--amber);font-weight:600;font-size:12px">auth_failed</span>`;
  return `<span style="color:var(--muted);font-size:12px">${esc(status)}</span>`;
}

function cameraOnuHealth(cam = {}) {
  if (!cam.pon || !cam.onu_id) {
    return { state: 'unlinked', label: 'Nao associada', detail: 'Camera sem PON/ONU associada' };
  }
  const oper = String(cam.onu_oper_status || '').trim().toLowerCase();
  const omci = String(cam.onu_omci_status || '').trim().toLowerCase();
  const up = ['active', 'online', 'up'].includes(oper);
  const down = ['inactive', 'offline', 'down', 'los', 'dying-gasp', 'dying_gasp'].includes(oper);
  const signal = cam.onu_rx ? `ONU RX ${cam.onu_rx}` : '';
  if (up) return { state: 'up', label: 'ONU online', detail: [cam.onu_oper_status, omci ? `OMCI ${cam.onu_omci_status}` : '', signal].filter(Boolean).join(' - ') };
  if (down) return { state: 'down', label: 'ONU offline', detail: [cam.onu_oper_status, signal].filter(Boolean).join(' - ') };
  return { state: 'unknown', label: 'ONU nao verificada', detail: 'Atualize os estados no Monitoramento' };
}

//  Painel lateral da camera
function openCamPanel(cam) {
  _invOltActive = cam;
  stopPing();
  closeCamPanelLive();

  // Destaca linha
  document.querySelectorAll('.inv-olt-row').forEach(tr => {
    tr.classList.toggle('row-selected', tr.dataset.ip === cam.ip);
  });

  // Preenche info
  const statusColor = cam.status === 'online' ? 'var(--primary)' : cam.status === 'offline' ? 'var(--danger)' : 'var(--amber)';
  const header = document.getElementById('camPanelStatus');
  header.textContent = cam.status || '';
  header.style.color = statusColor;
  setText('camPanelTitulo', cam.titulo || cam.ip);
  setText('cpIp',     cam.ip     || '');
  setText('cpMac',    cam.mac    || '');
  setText('cpModelo', cam.modelo || cam.model || '');
  setText('cpLocal',  cam.local  || '');

  const oltFields = document.getElementById('cpOltFields');
  const switchFields = document.getElementById('cpSwitchFields');
  const isSwitchView = _invOltView === 'switch';
  if (oltFields) oltFields.classList.toggle('hidden', isSwitchView);
  if (switchFields) switchFields.classList.toggle('hidden', !isSwitchView);

  if (isSwitchView) {
    setText('cpSwName', cam.switch_name || '');
    setText('cpSwIp',   cam.switch_ip   || '');
    setText('cpSwPort', cam.switch_port || '');
    setText('cpSwVlan', cam.switch_vlan || '');
  } else {
    setText('cpPonOnu', [cam.pon, cam.onu_id].filter(Boolean).join(' / ') || '');
    setText('cpSerial', cam.onu_serial || '');
    const onuHealth = cameraOnuHealth(cam);
    const onuStatus = document.getElementById('cpOnuStatus');
    if (onuStatus) {
      onuStatus.className = `cam-onu-status ${onuHealth.state}`;
      onuStatus.innerHTML = `<span class="cam-onu-status-dot"></span><span>${esc(onuHealth.label)}</span>`;
      onuStatus.title = onuHealth.detail;
    }
  }

  // Snapshot
  const img = document.getElementById('cpSnapshot');
  const empty = document.getElementById('cpSnapshotEmpty');
  const showSnapshotEmpty = () => {
    if (img) {
      img.style.display = 'none';
      img.removeAttribute('src');
    }
    if (empty) empty.style.display = 'flex';
    setText('cpSnapshotTitle', cam.titulo || cam.ip || '');
    setText('cpSnapshotTime', 'Sem snapshot');
  };
  if (img) img.onerror = showSnapshotEmpty;
  if (cam.snapshot_url) {
    const snapshotUrl = String(cam.snapshot_url || '');
    const sep = snapshotUrl.includes('?') ? '&' : '?';
    img.src = `${snapshotUrl}${sep}t=${Date.now()}`;
    img.style.display = 'block';
    empty.style.display = 'none';
    setText('cpSnapshotTitle', cam.titulo || cam.ip);
    setText('cpSnapshotTime',  '');
  } else {
    showSnapshotEmpty();
  }

  document.getElementById('cpPingResult')?.classList.add('hidden');
  document.getElementById('camPanelBackdrop')?.classList.remove('hidden');
  document.getElementById('camPanel').classList.remove('hidden');
  lucide.createIcons();
}

function closeCamPanel() {
  stopPing();
  closeCamPanelLive();
  _invOltActive = null;
  document.getElementById('camPanelBackdrop')?.classList.add('hidden');
  document.getElementById('camPanel').classList.add('hidden');
  document.querySelectorAll('.inv-olt-row').forEach(tr => tr.classList.remove('row-selected'));
}

//  Ping Terminal
let _pingIp    = null;
let _pingConnectorId = '';
let _pingConnectorTried = false;
let _pingCount = 0;
let _pingOk    = 0;
let _pingFail  = 0;

function openPingTerminal(ip, remoteConnectorId = '') {
  _pingIp = ip;
  _pingConnectorId = remoteConnectorId || '';
  _pingConnectorTried = false;
  document.getElementById('pingTermTitle').textContent = `ping ${ip}`;
  document.getElementById('pingTermBody').innerHTML = '';
  document.getElementById('pingTermStats').textContent = '';
  document.getElementById('pingTerminal').classList.remove('hidden');
  lucide.createIcons();
  runPing();
}

function runPing() {
  stopPing();
  _pingCount = 0; _pingOk = 0; _pingFail = 0;
  const ip = _pingIp;
  if (!ip) return;

  pingLine(`Iniciando ping para ${ip}`, 'info');

  _pingInterval = setInterval(async () => {
    // Limite de seguranca: um terminal esquecido aberto numa aba em segundo
    // plano gerava trafego real de ping contra a camera/rede do cliente
    // indefinidamente (1x/s, sempre com force=1). Para automaticamente apos
    // 10 minutos -- o usuario reabre o terminal se ainda precisar.
    if (_pingCount >= 600) {
      stopPing();
      pingLine('Ping parado automaticamente apos 10 minutos.', 'info');
      updatePingStats();
      return;
    }
    _pingCount++;
    const startedAt = performance.now();
    // Camera remota (atras de conector MikroTik, sem rota direta do servidor
    // ate a LAN do cliente): so tenta o fallback via conector UMA VEZ por
    // sessao, no primeiro tick que falhar -- o fallback pergunta ao proprio
    // MikroTik (pode levar ate ~45s) e repetir isso a cada segundo encheria
    // a fila de jobs do conector sem necessidade.
    const useConnector = _pingConnectorId && !_pingConnectorTried;
    if (useConnector) _pingConnectorTried = true;
    const url = `/api/cameras/ping?ip=${encodeURIComponent(ip)}&force=1`
      + (useConnector ? `&remote_connector_id=${encodeURIComponent(_pingConnectorId)}` : '');
    const res = await apiJson(url);
    const elapsedMs = performance.now() - startedAt;
    const rawMs = res?.ping_ms ?? res?.ms ?? res?.latency;
    const ms = Number.isFinite(Number(rawMs)) ? Number(rawMs) : elapsedMs;
    const ok  = Boolean(res?.online ?? res?.reachable);

    if (ok) {
      _pingOk++;
      const viaConn = res?.via_connector ? ' (via conector)' : '';
      pingLine(`[${_pingCount}] ${ip}: ${formatPingMs(ms)} (${res?.method || 'ping'})${viaConn}`, 'ok');
    } else {
      _pingFail++;
      const note = useConnector && res?.via_connector === null ? ' -- conector nao confirmou a tempo' : '';
      pingLine(`[${_pingCount}] ${ip}: offline (${res?.error || formatPingMs(elapsedMs)})${note}`, 'fail');
    }
    updatePingStats();
  }, 1000);
}

function formatPingMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 'sem medida';
  return `${n.toFixed(3)} ms`;
}

function pingLine(text, type = '') {
  const body = document.getElementById('pingTermBody');
  const line = document.createElement('div');
  if (type) line.className = `ping-term-line-${type}`;
  line.textContent = text;
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
}

function updatePingStats() {
  const loss = _pingCount > 0 ? Math.round((_pingFail / _pingCount) * 100) : 0;
  document.getElementById('pingTermStats').textContent =
    `Enviados: ${_pingCount}    OK: ${_pingOk}    Falhas: ${_pingFail}    Perda: ${loss}%`;
}

function stopPing() {
  if (_pingInterval) { clearInterval(_pingInterval); _pingInterval = null; }
}

function closePingTerminal() {
  stopPing();
  document.getElementById('pingTerminal').classList.add('hidden');
}

function startPing() {
  if (!_invOltActive) return;
  openPingTerminal(_invOltActive.ip, _invOltActive.remote_connector_id || '');
}

// Credencial de camera lembrada pelo resto da sessao (aba do navegador): uma
// vez digitada em qualquer camera, nao precisa digitar de novo pra ver outra
// -- a maioria dos clientes usa a mesma senha em todo o parque de cameras.
// Nunca persiste em disco (sessionStorage, some ao fechar a aba) nem sai pro
// backend fora do fluxo normal de "ver ao vivo".
function _camLiveCredGet() {
  try {
    return {
      user: sessionStorage.getItem('so_cam_live_user') || '',
      pass: sessionStorage.getItem('so_cam_live_pass') || '',
    };
  } catch { return { user: '', pass: '' }; }
}
function _camLiveCredSave(user, pass) {
  try {
    sessionStorage.setItem('so_cam_live_user', user || '');
    sessionStorage.setItem('so_cam_live_pass', pass || '');
  } catch {}
}

function openCamPanelLive() {
  if (!_invOltActive?.ip) return;
  const live = document.getElementById('cpInlineLive');
  const auth = document.getElementById('cpLiveAuth');
  const status = document.getElementById('cpLiveStatus');
  const video = document.getElementById('cpLiveVideo');
  if (!live || !auth || !status || !video) return;
  live.classList.remove('hidden');
  status.classList.add('hidden');
  video.classList.add('hidden');
  video.srcObject = null;
  _cpLiveSubtype = 1;
  const qualityLabel = document.querySelector('#cpLiveQuality span');
  if (qualityLabel) qualityLabel.textContent = 'SD';
  const remembered = _camLiveCredGet();
  const user = remembered.user || document.getElementById('mntCamUser')?.value || 'admin';
  const pass = remembered.pass || document.getElementById('mntCamPass')?.value || '';
  document.getElementById('cpLiveUser').value = user;
  document.getElementById('cpLivePass').value = pass;
  // Sempre tenta conectar primeiro, mesmo sem senha nenhuma conhecida NESTE
  // navegador: o servidor pode ja saber a senha desta camera/site (salva de
  // um acesso anterior, de qualquer operador). So mostra o formulario se o
  // servidor confirmar que realmente nao sabe (sinal 'credential_required'
  // vindo de startCamPanelLive).
  auth.style.display = 'none';
  startCamPanelLive();
}

function _openCamPanelLiveAuthForm() {
  const auth = document.getElementById('cpLiveAuth');
  if (!auth) return;
  auth.style.display = '';
  lucide.createIcons();
  setTimeout(() => {
    const passEl = document.getElementById('cpLivePass');
    if (passEl && !passEl.value) passEl.focus();
    else document.getElementById('cpLiveStart')?.focus();
  }, 60);
}

let _cpLiveHandle = null;
let _cpLiveSubtype = 1;

function closeCamPanelLive() {
  if (_cpLiveHandle) { _cpLiveHandle.stop(); _cpLiveHandle = null; }
  const video = document.getElementById('cpLiveVideo');
  if (video) { video.srcObject = null; video.classList.add('hidden'); }
  const live = document.getElementById('cpInlineLive');
  live?.classList.remove('playing', 'mobile-fullscreen');
  live?.classList.add('hidden');
  document.body.classList.remove('cam-live-lock');
  const status = document.getElementById('cpLiveStatus');
  if (status) status.classList.add('hidden');
}

function cameraStreamHint(ip, fallback = null) {
  const lists = [
    fallback ? [fallback] : [],
    _invOltActive ? [_invOltActive] : [],
    Array.isArray(_mntCamAll) ? _mntCamAll : [],
    _invCam?.basic || [],
    _invCam?.olt || [],
    _invCam?.switch || []
  ];
  for (const list of lists) {
    const cam = list.find?.(c => String(c?.ip || c?.host || '') === String(ip));
    if (cam) {
      return {
        vendor: cam.fabricante || cam.vendor || cam.brand || '',
        model: cam.modelo || cam.model || cam.camera_model || ''
      };
    }
  }
  return { vendor: '', model: '' };
}

function isHikvisionStream(hint = {}) {
  const vendor = String(hint.vendor || '').trim().toLowerCase();
  const model = String(hint.model || '').trim().toLowerCase();
  const isIntelbras = vendor.includes('intelbras') || vendor.includes('dahua') || /^(vip-|vipc-|vhd-)/.test(model);
  if (isIntelbras) return false;
  return vendor.includes('hikvision') || vendor.includes('hilook') || /^(ds-|ds2|ipc-)/.test(model);
}

function buildCameraRtspUrl(ip, user, pass, subtype = 1, hint = {}) {
  const st = Number(subtype) === 0 ? 0 : 1;
  const auth = `${encodeURIComponent(user || 'admin')}:${encodeURIComponent(pass || '')}`;
  if (isHikvisionStream(hint)) {
    return `rtsp://${auth}@${ip}:554/Streaming/Channels/${st === 0 ? '101' : '102'}`;
  }
  return `rtsp://${auth}@${ip}:554/cam/realmonitor?channel=1&subtype=${st}`;
}

function toggleCamLivePassword() {
  const input = document.getElementById('cpLivePass');
  const btn = document.getElementById('cpLivePassToggle');
  if (!input || !btn) return;
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  btn.innerHTML = `<i data-lucide="${show ? 'eye-off' : 'eye'}"></i>`;
  btn.title = show ? 'Ocultar senha' : 'Mostrar senha';
  btn.setAttribute('aria-label', btn.title);
  lucide.createIcons();
}

function fullscreenCamPanelLive() {
  const live = document.getElementById('cpInlineLive');
  if (!live) return;
  const btn = document.getElementById('cpLiveFullscreen');
  const setIcon = (expanded) => {
    if (!btn) return;
    btn.innerHTML = `<i data-lucide="${expanded ? 'minimize-2' : 'maximize-2'}"></i>`;
    btn.title = expanded ? 'Reduzir video' : 'Ampliar video';
    lucide.createIcons();
  };

  if (live.classList.contains('mobile-fullscreen')) {
    live.classList.remove('mobile-fullscreen');
    document.body.classList.remove('cam-live-lock');
    setIcon(false);
    return;
  }

  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
    setIcon(false);
    return;
  }

  const useFallback = () => {
    live.classList.add('mobile-fullscreen');
    document.body.classList.add('cam-live-lock');
    setIcon(true);
  };

  if (live.requestFullscreen) {
    live.requestFullscreen()
      .then(() => setIcon(true))
      .catch(useFallback);
  } else {
    useFallback();
  }
}

async function startCamPanelLive() {
  if (!_invOltActive?.ip) return;
  const ip = _invOltActive.ip;
  const user = document.getElementById('cpLiveUser')?.value.trim() || 'admin';
  const pass = document.getElementById('cpLivePass')?.value || '';
  // Sem senha nao bloqueia mais aqui: manda mesmo assim e deixa o servidor
  // decidir -- ele pode ja ter a senha desta camera/site salva de uma vez
  // anterior (de qualquer operador, nao so deste navegador). So volta a
  // pedir se o servidor confirmar que realmente nao sabe.
  if (pass) _camLiveCredSave(user, pass);

  const auth = document.getElementById('cpLiveAuth');
  const status = document.getElementById('cpLiveStatus');
  const statusText = status?.querySelector('span');
  const video = document.getElementById('cpLiveVideo');
  if (!auth || !status || !video) return;

  if (_cpLiveHandle) { _cpLiveHandle.stop(); _cpLiveHandle = null; }
  auth.style.display = 'none';
  status.classList.remove('hidden');
  if (statusText) statusText.textContent = 'Conectando...';
  video.srcObject = null;
  video.classList.remove('hidden');

  const hint = cameraStreamHint(ip, _invOltActive);
  _cpLiveHandle = mountLiveStream(video, {
    ip, user, pass,
    subtype: _cpLiveSubtype,
    vendor: hint.vendor,
    model: hint.model,
    onStatus: (texto) => {
      if (texto === 'credential_required') {
        status.classList.add('hidden');
        video.classList.add('hidden');
        _camLiveCredSave('', '');
        _openCamPanelLiveAuthForm();
        return;
      }
      if (texto) {
        if (statusText) statusText.textContent = texto;
        status.classList.remove('hidden');
      } else {
        status.classList.add('hidden');
        document.getElementById('cpInlineLive')?.classList.add('playing');
      }
    },
  });
}

function changeCamLiveCredential() {
  if (_cpLiveHandle) { _cpLiveHandle.stop(); _cpLiveHandle = null; }
  _camLiveCredSave('', '');
  const auth = document.getElementById('cpLiveAuth');
  const status = document.getElementById('cpLiveStatus');
  const video = document.getElementById('cpLiveVideo');
  if (!auth || !status || !video) return;
  video.srcObject = null;
  video.classList.add('hidden');
  status.classList.add('hidden');
  document.getElementById('cpInlineLive')?.classList.remove('playing');
  document.getElementById('cpLiveUser').value = 'admin';
  document.getElementById('cpLivePass').value = '';
  auth.style.display = '';
  setTimeout(() => document.getElementById('cpLivePass')?.focus(), 60);
}

function toggleCamPanelLiveQuality() {
  if (!_cpLiveHandle) return;
  _cpLiveSubtype = _cpLiveSubtype === 0 ? 1 : 0;
  _cpLiveHandle.setSubtype(_cpLiveSubtype);
  const label = document.querySelector('#cpLiveQuality span');
  if (label) label.textContent = _cpLiveSubtype === 0 ? 'HD' : 'SD';
}

//  Acoes do painel
function openCamAuthAction(action) {
  if (!_invOltActive) return;
  const cam = _invOltActive;
  const labels = {
    atualizar: { title: 'Atualizar snapshot', icon: 'refresh-cw', label: 'Atualizar' },
    reboot: { title: 'Reboot da camera', icon: 'power', label: 'Reboot' },
  };
  const meta = labels[action] || labels.atualizar;
  _camAuthAction = action;
  setText('camAuthEyebrow', cam.ip);
  setText('camAuthTitle', meta.title);
  document.getElementById('camAuthIp').value = `${cam.ip} - ${cam.titulo || ''}`;
  document.getElementById('camAuthUser').value = 'admin';
  document.getElementById('camAuthPass').value = '';
  document.getElementById('camAuthErro').hidden = true;
  const btn = document.getElementById('confirmCamAuthAction');
  btn.innerHTML = `<i data-lucide="${meta.icon}"></i> ${meta.label}`;
  btn.disabled = false;
  document.getElementById('modalCamAuthAction').classList.remove('hidden');
  setTimeout(() => document.getElementById('camAuthPass').focus(), 80);
  lucide.createIcons();
}

function closeCamAuthAction() {
  document.getElementById('modalCamAuthAction')?.classList.add('hidden');
  _camAuthAction = null;
}

function camAuthCreds() {
  return {
    user: document.getElementById('camAuthUser')?.value.trim() || 'admin',
    pass: document.getElementById('camAuthPass')?.value || '',
  };
}

async function updateCameraSnapshot(cam, cred) {
  showToast('Capturando snapshot...');
  const res = await api('/api/cameras/snapshot/capture', {
    method: 'POST',
    body: JSON.stringify({ ip: cam.ip, user: cred.user, password: cred.pass, mode: _invOltView || 'olt' }),
  });
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    showToast(data?.detail || data?.error || 'Erro ao capturar snapshot.', true);
    return;
  }
  cam.snapshot_url = data.url;
  _invOltActive = cam;
  const img = document.getElementById('cpSnapshot');
  const empty = document.getElementById('cpSnapshotEmpty');
  img.src = `${API_BASE}${data.url}?t=${Date.now()}`;
  img.style.display = 'block';
  empty.style.display = 'none';
  showToast('Snapshot atualizado.');
  setTimeout(loadInvOlt, 800);
}

async function rebootCamera(cam, cred) {
  const res = await api('/api/maintenance/batch/reboot', {
    method: 'POST',
    body: JSON.stringify({ ips: [cam.ip], user: cred.user, pass: cred.pass }),
  });
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    const first = (data?.results || []).find(r => !r.ok) || {};
    throw new Error(data?.error || first.error || 'Erro ao reiniciar camera.');
  }
  showToast('Reboot enviado.');
}

async function runCamAuthAction() {
  if (!_invOltActive || !_camAuthAction) return;
  const cam = _invOltActive;
  const action = _camAuthAction;
  const cred = camAuthCreds();
  const erro = document.getElementById('camAuthErro');
  const btn = document.getElementById('confirmCamAuthAction');
  if (!cred.pass) {
    erro.textContent = 'Informe a senha da camera.';
    erro.hidden = false;
    return;
  }
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i data-lucide="loader-2"></i> Executando';
  lucide.createIcons();
  try {
    if (action === 'atualizar') await updateCameraSnapshot(cam, cred);
    if (action === 'reboot') await rebootCamera(cam, cred);
    closeCamAuthAction();
  } catch (err) {
    erro.textContent = err.message || 'Falha ao executar acao.';
    erro.hidden = false;
    showToast(erro.textContent, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
    lucide.createIcons();
  }
}

async function camAction(action) {
  if (!_invOltActive) return;
  const cam = _invOltActive;

  if (action === 'atualizar') {
    openCamAuthAction('atualizar');
    return;
  }
  if (action === 'reboot') {
    openCamAuthAction('reboot');
    return;
  }
  if (action === 'web') {
    window.open(`${API_BASE}/api/maintenance/web/${encodeURIComponent(cam.ip)}/`, '_blank', 'noopener');
    return;
  }

  if (action === 'renomear') {
    openEditCamModal([cam], { renameDevice: true });
    return;
  }
  if (action === 'trocar-ip') {
    document.getElementById('trocarIpAtual').value = cam.ip;
    document.getElementById('trocarIpNovo').value  = '';
    document.getElementById('trocarIpMask').value  = '';
    document.getElementById('trocarIpGw').value    = '';
    document.getElementById('trocarIpUser').value  = 'admin';
    document.getElementById('trocarIpPass').value  = '';
    document.getElementById('trocarIpErro').hidden = true;
    _fillTrocarIpNetwork(cam.ip, true);
    document.getElementById('modalTrocarIp').classList.remove('hidden');
    setTimeout(() => document.getElementById('trocarIpNovo').focus(), 50);
    lucide.createIcons();
    return;
  }
  if (action === 'trocar-senha') {
    document.getElementById('trocarSenhaIp').value      = `${cam.ip}    ${cam.titulo || ''}`;
    document.getElementById('trocarSenhaUser').value    = 'admin';
    document.getElementById('trocarSenhaAtual').value   = '';
    document.getElementById('trocarSenhaNova').value    = '';
    document.getElementById('trocarSenhaConfirm').value = '';
    document.getElementById('trocarSenhaErro').hidden   = true;
    document.getElementById('modalTrocarSenha').classList.remove('hidden');
    setTimeout(() => document.getElementById('trocarSenhaNova').focus(), 50);
    lucide.createIcons();
    return;
  }
  if (action === 'data-hora') {
    const now = new Date();
    document.getElementById('dataHoraIp').value    = `${cam.ip}    ${cam.titulo || ''}`;
    document.getElementById('dataHoraUser').value  = 'admin';
    document.getElementById('dataHoraPass').value  = '';
    document.getElementById('dataHoraData').value  = now.toLocaleDateString('sv');
    document.getElementById('dataHoraHora').value  = now.toTimeString().slice(0,5);
    document.getElementById('dataHoraErro').hidden = true;
    document.getElementById('modalDataHora').classList.remove('hidden');
    lucide.createIcons();
    return;
  }
  if (action === 'limpar') {
    if (!await showConfirm({ title: `Remover camera`, msg: `Remover ${cam.ip}  ${cam.titulo || ''} do inventario?`, label: 'Remover' })) return;
    const key = _camKey(cam);
    const res = await api('/api/inventory/delete', {
      method: 'POST',
      body: JSON.stringify({
        ips: [cam.ip],
        keys: [key],
        mode: _invOltView || 'olt',
        site: cam.local || cam.site || cam.site_name || '',
        connector_id: cam.remote_connector_id || cam.connector_id || '',
      }),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      showToast(data?.detail || data?.error || 'NAo foi possAvel remover a cAmera.', true);
      return;
    }
    _camRemoveIpsLocally([key]);
    showToast('Camera removida.');
    closeCamPanel();
    updateCamTabs();
    populateCamSiteFilter();
    applyInvOltFilters();
    return;
  }
}

// Relatorio PDF de Cameras IP: roda como job assincrono (POST inicia,
// GET status acompanha done/total/etapa) em vez de uma unica requisicao
// sincrona -- com a galeria de fotos de volta, gerar pode levar de
// segundos a minutos num inventario grande, e sem progresso visivel a
// tela parece travada. O icone do botao gira e o texto/toast atualizam
// a cada etapa (tabela -> fotos -> finalizando) com contagem real.
async function runInvOltReport(button) {
  if (!button || button.disabled) return;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  const originalTip = button.dataset.tip;
  const originalIcon = button.innerHTML;
  button.innerHTML = '<i data-lucide="loader-2" class="spin"></i>';
  lucide.createIcons();

  const setProgress = (text) => {
    button.dataset.tip = text;
    showToast(text);
  };

  try {
    // O relatorio reflete exatamente o que esta na tela no momento do
    // clique: aba ativa (basico/olt/switch), filtro de site e a lista
    // filtrada/pesquisada na tabela -- ou so as marcadas, se houver
    // alguma selecionada.
    const mode = _invOltView || 'olt';
    const checked = [...document.querySelectorAll('.chk-olt:checked')].map(c => c.value);
    const visibleIps = [...document.querySelectorAll('#invOltTable .inv-olt-row')].map(tr => tr.dataset.ip).filter(Boolean);
    const totalInMode = (_invCam[mode] || []).length;
    const narrowed = checked.length > 0 || visibleIps.length !== totalInMode;
    const ips = checked.length ? checked : visibleIps;

    const params = new URLSearchParams({ mode });
    const site = document.getElementById('filterSiteOlt')?.value || '';
    if (site) params.set('site', site);
    if (narrowed) params.set('ips', ips.join(','));

    setProgress('Preparando relatório...');
    const start = await jsonOrReadableError(
      await api(`/api/inventory/report/job?${params.toString()}`, { method: 'POST' }),
      'Não foi possível iniciar a geração do relatório.'
    );
    const jobId = start?.job_id;
    if (!jobId) throw new Error('Não foi possível iniciar a geração do relatório.');

    const startedAt = Date.now();
    let state = null;
    while (Date.now() - startedAt < 15 * 60 * 1000) {
      state = await jsonOrReadableError(
        await api(`/api/inventory/report/job/${jobId}/status`),
        'Não foi possível acompanhar a geração do relatório.'
      );
      if (state?.status === 'done' || state?.status === 'error') break;
      const stageLabel = {
        preparando: 'Preparando relatório...',
        tabela: `Montando tabela... ${state?.done ?? 0}/${state?.total ?? 0}`,
        fotos: `Gerando fotos... ${state?.done ?? 0}/${state?.total ?? 0}`,
        finalizando: 'Finalizando PDF...',
      }[state?.stage] || 'Gerando relatório...';
      setProgress(stageLabel);
      await new Promise(resolve => setTimeout(resolve, 700));
    }

    if (state?.status === 'error') throw new Error(state?.error || 'Falha ao gerar o relatório.');
    if (state?.status !== 'done') throw new Error('A geração está demorando mais que o esperado. Tente novamente em instantes.');

    setProgress('Baixando PDF...');
    const res = await api(`/api/inventory/report/job/${jobId}/download`);
    if (!res || !res.ok) throw new Error('Não foi possível baixar o relatório gerado.');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `relatorio-cameras-ip-${mode}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Relatório gerado. O download foi iniciado.');
  } catch (err) {
    showToast(err?.message || 'Não foi possível gerar o relatório.', true);
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    button.dataset.tip = originalTip || 'Relatório PDF';
    button.innerHTML = originalIcon;
    lucide.createIcons();
  }
}

//  Inventario DVR


// Clique em qualquer outro lugar fecha o menu de camada aberto.
document.addEventListener('click', function fecharMenusDeCamada(ev) {
  if (ev.target.closest?.('.map-layer-menu-wrap')) return;
  document.querySelectorAll('.map-layer-menu').forEach(m => { m.hidden = true; });
});

// Com position:fixed o menu nao acompanha a rolagem da lista: ficaria solto
// na tela, longe da camada que o abriu.
document.addEventListener('scroll', () => {
  document.querySelectorAll('.map-layer-menu').forEach(m => { m.hidden = true; });
}, true);
