async function loadInvDvr() {
  const data = await apiJson('/api/dvr/inventory');
  const dvrs = data?.dvrs || data || [];
  const tbody = document.getElementById('invDvrTable');
  if (!dvrs.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">Nenhum DVR. Execute uma varredura.</td></tr>';
    setText('invDvrFooter', '0 DVRs');
    return;
  }
  tbody.innerHTML = dvrs.map(d => `
    <tr>
      <td class="monospace">${esc(d.ip)}</td>
      <td>${esc(d.brand || '')} ${esc(d.model || '')}</td>
      <td class="text-muted">${esc(d.channels || '')}</td>
      <td class="text-muted monospace">${esc(d.firmware || '')}</td>
      <td class="text-muted">${esc(d.last_snapshot || '')}</td>
      <td>${statusBadge(d.status)}</td>
      <td>
        <button class="ghost-action" style="padding:4px 8px;font-size:12px" onclick="openCamera('${esc(d.ip)}')">
          <i data-lucide="external-link"></i>
        </button>
      </td>
    </tr>`).join('');
  setText('invDvrFooter', `${dvrs.length} DVR${dvrs.length !== 1 ? 's' : ''}`);
  lucide.createIcons();
}

// Inventario NVR
// Gravadores  NVR e DVR com dados por modo
const _invNvr   = { basico: [], olt: [], switch: [] };
const _invDvr   = { basico: [], olt: [], switch: [] };
let _invNvrView   = (() => {
  return 'basico';
})();
let _recType      = (() => {
  try { return sessionStorage.getItem('so_rec_type') || 'nvr'; } catch { return 'nvr'; }
})(); // 'nvr' | 'dvr'
let _nvrAbortCtrl = null;
let _recActive    = null;
let _recAction    = null;
let _nvrActiveScan = null;

function _recSessionSave(type, mode, rows) {
  try { sessionStorage.setItem(`so_${type}_${mode}`, JSON.stringify(rows)); } catch {}
}
function _recSessionLoad() {
  ['nvr','dvr'].forEach(type => {
    const store = type === 'nvr' ? _invNvr : _invDvr;
    ['basico','olt','switch'].forEach(m => {
      try {
        const d = JSON.parse(sessionStorage.getItem(`so_${type}_${m}`) || 'null');
        if (Array.isArray(d)) store[m] = d;
      } catch {}
    });
    pruneSyntheticRecModes(type);
  });
}
function _nvrSessionSave(mode, rows) { _recSessionSave('nvr', mode, rows); }
function _nvrSessionLoad() { _recSessionLoad(); }

async function _loadRecBasico() {
  const cacheBust = Date.now();
  const [nvrData, dvrData] = await Promise.all([
    apiJson(`/api/nvr/inventory?_=${cacheBust}`, { forceRefresh: true }),
    apiJson(`/api/dvr/inventory?_=${cacheBust}`, { forceRefresh: true }),
  ]);
  _invNvr.basico = (nvrData?.inventory || []).filter(row => recRowInventoryMode(row) === 'basico');
  _invDvr.basico = (dvrData?.inventory || []).filter(row => recRowInventoryMode(row) === 'basico');
  _recSessionSave('nvr', 'basico', _invNvr.basico);
  _recSessionSave('dvr', 'basico', _invDvr.basico);
}

async function _loadRecBasicoForType(type) {
  const endpoint = type === 'dvr' ? '/api/dvr/inventory' : '/api/nvr/inventory';
  const data = await apiJson(`${endpoint}?_=${Date.now()}`, { forceRefresh: true });
  const rows = (data?.inventory || []).filter(row => recRowInventoryMode(row) === 'basico');
  const store = type === 'dvr' ? _invDvr : _invNvr;
  store.basico = rows;
  _recSessionSave(type, 'basico', rows);
  return rows;
}

async function _loadRecForMode(type, mode) {
  const store = type === 'dvr' ? _invDvr : _invNvr;
  if (mode === 'basico') {
    return _loadRecBasicoForType(type);
  }
  const endpoint = type === 'dvr' ? '/api/dvr/inventory' : '/api/nvr/inventory';
  const data = await apiJson(`${endpoint}?_=${Date.now()}`, { forceRefresh: true });
  const sourceRows = (data?.inventory || []).filter(row => recRowInventoryMode(row) === mode);
  const rows = await enrichRecRowsForMode(sourceRows, mode);
  // O modo gravado no canal e a fonte de verdade. Dados complementares de
  // OLT/Switch podem estar temporariamente indisponiveis e nao devem fazer o
  // inventario inteiro desaparecer enquanto os snapshots continuam visiveis.
  if (sourceRows.length) {
    store[mode] = rows;
    _recSessionSave(type, mode, rows);
    return rows;
  }
  store[mode] = [];
  try { sessionStorage.removeItem(`so_${type}_${mode}`); } catch {}
  return [];
}

async function _loadRecAllModesForType(type) {
  const endpoint = type === 'dvr' ? '/api/dvr/inventory' : '/api/nvr/inventory';
  const data = await apiJson(`${endpoint}?_=${Date.now()}`, { forceRefresh: true });
  const allRows = data?.inventory || [];
  const store = type === 'dvr' ? _invDvr : _invNvr;
  for (const mode of ['basico', 'olt', 'switch']) {
    const sourceRows = allRows.filter(row => recRowInventoryMode(row) === mode);
    const rows = mode === 'basico' ? sourceRows : await enrichRecRowsForMode(sourceRows, mode);
    store[mode] = rows;
    if (rows.length) _recSessionSave(type, mode, rows);
    else {
      try { sessionStorage.removeItem(`so_${type}_${mode}`); } catch {}
    }
  }
  return allRows;
}
function _nvrSessionClear() {
  ['nvr','dvr'].forEach(type => {
    const store = type === 'nvr' ? _invNvr : _invDvr;
    ['basico','olt','switch'].forEach(m => {
      try { sessionStorage.removeItem(`so_${type}_${m}`); } catch {}
      store[m] = [];
    });
  });
}

function compareIpv4(a, b) {
  const ap = String(a || '').split('.').map(n => Number(n));
  const bp = String(b || '').split('.').map(n => Number(n));
  if (ap.length === 4 && bp.length === 4 && ap.every(Number.isFinite) && bp.every(Number.isFinite)) {
    for (let i = 0; i < 4; i += 1) {
      if (ap[i] !== bp[i]) return ap[i] - bp[i];
    }
    return 0;
  }
  return String(a || '').localeCompare(String(b || ''), undefined, { numeric: true });
}

function recModeHasRealData(rows, mode) {
  if (!Array.isArray(rows) || !rows.length) return false;
  if (mode === 'olt') return rows.some(r => ![r.pon, r.onu_id, r.onu_name, r.onu_serial].every(_isBlankValue));
  if (mode === 'switch') return rows.some(r => ![r.switch_ip, r.switch_port, r.switch_vlan].every(_isBlankValue));
  return rows.length > 0;
}

function pruneSyntheticRecModes(type) {
  const store = type === 'dvr' ? _invDvr : _invNvr;
  ['olt', 'switch'].forEach(mode => {
    if (store[mode]?.length && !recModeHasRealData(store[mode], mode)) {
      store[mode] = [];
      try { sessionStorage.removeItem(`so_${type}_${mode}`); } catch {}
    }
  });
}

function removeRecItemsLocally(type, items, onlyMode = '') {
  const store = type === 'dvr' ? _invDvr : _invNvr;
  const keys = new Set(items.map(x => `${x.host}|${Number(x.channel || 0)}`));
  ['basico', 'olt', 'switch'].forEach(mode => {
    if (onlyMode && mode !== onlyMode) return;
    if (!store[mode]?.length) return;
    store[mode] = store[mode].filter(r => !keys.has(`${r.host}|${Number(r.channel || 0)}`));
    _recSessionSave(type, mode, store[mode]);
  });
}

function removeRecHostLocally(type, host) {
  const store = type === 'dvr' ? _invDvr : _invNvr;
  ['basico', 'olt', 'switch'].forEach(mode => {
    if (!store[mode]?.length) return;
    store[mode] = store[mode].filter(r => String(r.host || '') !== String(host || ''));
    _recSessionSave(type, mode, store[mode]);
  });
}

function discardActiveRecorderScan() {
  const scan = _nvrActiveScan;
  if (!scan?.host) return;
  const items = Array.from({ length: Math.max(0, scan.end - scan.start + 1) }, (_, i) => ({
    host: scan.host,
    channel: scan.start + i,
  }));
  removeRecHostLocally(scan.type, scan.host);
  api(`/api/${scan.type}/delete`, { method: 'POST', body: JSON.stringify({ items }), skipLogout: true }).catch(() => {});
  updateNvrTabs();
  populateNvrFilters();
  applyNvrFilters();
}

function recImgbbUrl(r) {
  return r.imgbb_url || r.imgbb_thumb_url || (isImgbbUrl(r.snapshot_url) ? r.snapshot_url : '');
}

function recImgbbCell(r) {
  const url = recImgbbUrl(r);
  return url
    ? `<a href="${esc(url)}" target="_blank" onclick="event.stopPropagation()" style="color:var(--primary);font-weight:700;font-size:12px;text-decoration:none"> up</a>`
    : `<span style="color:var(--danger);font-weight:700;font-size:12px"> down</span>`;
}

function recHasMissingData(r) {
  const required = _recType === 'dvr'
    ? [r.host, r.channel, r.title, r.local, r.status, r.mac, r.modelo || r.model]
    : [r.host, r.channel, r.title, r.local, r.status, r.camera_ip, r.camera_model || r.modelo, r.camera_mac || r.mac];
  if (_invNvrView === 'olt') required.push(r.pon, r.onu_id, r.onu_name, r.onu_serial);
  if (_invNvrView === 'switch') required.push(r.switch_ip, r.switch_port);
  return required.some(_isBlankValue);
}

function recHasDefaultTitle(r) {
  return camHasDefaultTitle({ titulo: r.title });
}

function recHasNoCamera(r) {
  return _recType === 'nvr'
    ? [r.camera_ip, r.camera_model || r.modelo, r.camera_mac || r.mac].some(_isBlankValue)
    : _isBlankValue(r.mac);
}

// Larguras em % (nunca px fixo): garantem que a tabela nunca ultrapasse
// 100% do container, ou seja, zero scroll horizontal em qualquer zoom/tela.
// Ver skill sightops-table-fix para o metodo completo.
const NVR_COLS = {
  basico: {
    cols: ['3%','10%','8%','4%','15%','7%','7%','5%','9%','8%','11%','8%','5%'],
    heads: ['','Host NVR','Modelo NVR','CH','Titulo','Local','Status','ImgBB','IP Camera','Modelo Cam.','MAC Camera','Serial','V.Loss'],
    row: r => [
      `<input type="checkbox" class="chk-nvr" value="${esc(r.host+'_'+r.channel)}" data-host="${esc(r.host||'')}" data-channel="${esc(String(r.channel||''))}">`,
      `<span class="monospace" title="${esc(r.host||'')}">${esc(r.host||'')}</span>`,
      `<span class="text-muted" title="${esc(r.nvr_model||'')}">${esc(r.nvr_model||'')}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.channel??''))}</span>`,
      `<strong title="${esc(r.title||'')}">${esc(r.title||'')}</strong>`,
      `<span title="${esc(r.local||'')}">${esc(r.local||'')}</span>`,
      (r.status||'').toLowerCase()==='online'
        ? `<span style="color:var(--primary);font-weight:600;font-size:12px">online</span>`
        : `<span style="color:var(--danger);font-weight:600;font-size:12px">offline</span>`,
      recImgbbCell(r),
      `<span class="monospace text-muted" title="${esc(r.camera_ip||'')}">${esc(r.camera_ip||'')}</span>`,
      `<span class="text-muted" title="${esc(r.camera_model||r.modelo||'')}">${esc(r.camera_model||r.modelo||'')}</span>`,
      `<span class="monospace text-muted" title="${esc(r.camera_mac||r.mac||'')}" style="font-size:11px">${esc(r.camera_mac||r.mac||'')}</span>`,
      `<span class="text-muted" title="${esc(r.equip_serial||'')}">${esc(r.equip_serial||'')}</span>`,
      r.video_loss
        ? `<span style="color:var(--danger);font-weight:600;font-size:11px">SIM</span>`
        : `<span class="text-muted" style="font-size:11px">nao</span>`,
    ],
  },
  olt: {
    cols: ['3%','11%','4%','15%','7%','7%','5%','9%','8%','4%','5%','10%','12%'],
    heads: ['','Host NVR','CH','Titulo','Local','Status','ImgBB','IP Camera','Modelo Cam.','PON','ONU ID','ONU Name','ONU Serial'],
    row: r => [
      `<input type="checkbox" class="chk-nvr" value="${esc(r.host+'_'+r.channel)}" data-host="${esc(r.host||'')}" data-channel="${esc(String(r.channel||''))}">`,
      `<span class="monospace" title="${esc(r.host||'')}">${esc(r.host||'')}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.channel??''))}</span>`,
      `<strong title="${esc(r.title||'')}">${esc(r.title||'')}</strong>`,
      `<span title="${esc(r.local||'')}">${esc(r.local||'')}</span>`,
      (r.status||'').toLowerCase()==='online'
        ? `<span style="color:var(--primary);font-weight:600;font-size:12px">online</span>`
        : `<span style="color:var(--danger);font-weight:600;font-size:12px">offline</span>`,
      recImgbbCell(r),
      `<span class="monospace text-muted" title="${esc(r.camera_ip||'')}">${esc(r.camera_ip||'')}</span>`,
      `<span class="text-muted" title="${esc(r.camera_model||r.modelo||'')}">${esc(r.camera_model||r.modelo||'')}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.pon||''))}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.onu_id||''))}</span>`,
      `<span class="text-muted" title="${esc(r.onu_name||'')}" style="font-size:11px">${esc(r.onu_name||'')}</span>`,
      `<span class="monospace text-muted" title="${esc(r.onu_serial||'')}" style="font-size:11px">${esc(r.onu_serial||'')}</span>`,
    ],
  },
  switch: {
    cols: ['4%','12%','4%','18%','8%','8%','6%','10%','9%','12%','6%','3%'],
    heads: ['','Host NVR','CH','Titulo','Local','Status','ImgBB','IP Camera','Modelo Cam.','Switch IP','Porta','VLAN'],
    row: r => [
      `<input type="checkbox" class="chk-nvr" value="${esc(r.host+'_'+r.channel)}" data-host="${esc(r.host||'')}" data-channel="${esc(String(r.channel||''))}">`,
      `<span class="monospace" title="${esc(r.host||'')}">${esc(r.host||'')}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.channel??''))}</span>`,
      `<strong title="${esc(r.title||'')}">${esc(r.title||'')}</strong>`,
      `<span title="${esc(r.local||'')}">${esc(r.local||'')}</span>`,
      (r.status||'').toLowerCase()==='online'
        ? `<span style="color:var(--primary);font-weight:600;font-size:12px">online</span>`
        : `<span style="color:var(--danger);font-weight:600;font-size:12px">offline</span>`,
      recImgbbCell(r),
      `<span class="monospace text-muted" title="${esc(r.camera_ip||'')}">${esc(r.camera_ip||'')}</span>`,
      `<span class="text-muted" title="${esc(r.camera_model||r.modelo||'')}">${esc(r.camera_model||r.modelo||'')}</span>`,
      `<span class="monospace text-muted" title="${esc(r.switch_ip||'')}">${esc(r.switch_ip||'')}</span>`,
      `<span class="text-muted" title="${esc(r.switch_port||'')}">${esc(r.switch_port||'')}</span>`,
      `<span class="text-muted" title="${esc(r.switch_vlan||'')}">${esc(r.switch_vlan||'')}</span>`,
    ],
  },
};

const DVR_COLS = {
  basico: {
    cols: ['4%','12%','4%','18%','8%','8%','6%','13%','9%','9%','5%','4%'],
    heads: ['','Host DVR','CH','Titulo','Local','Status','ImgBB','MAC DVR','Modelo','Serial','V.Loss','Foto'],
    row: r => [
      `<input type="checkbox" class="chk-nvr" value="${esc(r.host+'_'+r.channel)}" data-host="${esc(r.host||'')}" data-channel="${esc(String(r.channel||''))}">`,
      `<span class="monospace" title="${esc(r.host||'')}">${esc(r.host||'')}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.channel??''))}</span>`,
      `<strong title="${esc(r.title||'')}">${esc(r.title||'')}</strong>`,
      `<span title="${esc(r.local||'')}">${esc(r.local||'')}</span>`,
      (r.status||'').toLowerCase()==='online'
        ? `<span style="color:var(--primary);font-weight:600;font-size:12px">online</span>`
        : `<span style="color:var(--danger);font-weight:600;font-size:12px">offline</span>`,
      recImgbbCell(r),
      `<span class="monospace text-muted" title="${esc(r.mac||'')}" style="font-size:11px">${esc(r.mac||'')}</span>`,
      `<span class="text-muted" title="${esc(r.modelo||'')}">${esc(r.modelo||'')}</span>`,
      `<span class="text-muted" title="${esc(r.equip_serial||'')}">${esc(r.equip_serial||'')}</span>`,
      r.video_loss ? `<span style="color:var(--danger);font-weight:600;font-size:11px">SIM</span>` : `<span class="text-muted" style="font-size:11px">nao</span>`,
      r.snapshot_url ? `<a href="${esc(r.snapshot_url)}" target="_blank" style="color:var(--primary);font-size:12px"> ver</a>` : `<span class="text-muted"></span>`,
    ],
  },
  olt: {
    cols: ['4%','12%','4%','17%','7%','7%','5%','4%','5%','10%','12%','13%'],
    heads: ['','Host DVR','CH','Titulo','Local','Status','ImgBB','PON','ONU ID','ONU Name','ONU Serial','MAC DVR'],
    row: r => [
      `<input type="checkbox" class="chk-nvr" value="${esc(r.host+'_'+r.channel)}" data-host="${esc(r.host||'')}" data-channel="${esc(String(r.channel||''))}">`,
      `<span class="monospace" title="${esc(r.host||'')}">${esc(r.host||'')}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.channel??''))}</span>`,
      `<strong title="${esc(r.title||'')}">${esc(r.title||'')}</strong>`,
      `<span title="${esc(r.local||'')}">${esc(r.local||'')}</span>`,
      (r.status||'').toLowerCase()==='online'
        ? `<span style="color:var(--primary);font-weight:600;font-size:12px">online</span>`
        : `<span style="color:var(--danger);font-weight:600;font-size:12px">offline</span>`,
      recImgbbCell(r),
      `<span style="text-align:center;display:block">${esc(String(r.pon||''))}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.onu_id||''))}</span>`,
      `<span class="text-muted" title="${esc(r.onu_name||'')}" style="font-size:11px">${esc(r.onu_name||'')}</span>`,
      `<span class="monospace text-muted" title="${esc(r.onu_serial||'')}" style="font-size:11px">${esc(r.onu_serial||'')}</span>`,
      `<span class="monospace text-muted" title="${esc(r.mac||'')}" style="font-size:11px">${esc(r.mac||'')}</span>`,
    ],
  },
  switch: {
    cols: ['4%','13%','4%','19%','9%','8%','6%','12%','6%','4%','15%'],
    heads: ['','Host DVR','CH','Titulo','Local','Status','ImgBB','Switch IP','Porta','VLAN','MAC DVR'],
    row: r => [
      `<input type="checkbox" class="chk-nvr" value="${esc(r.host+'_'+r.channel)}" data-host="${esc(r.host||'')}" data-channel="${esc(String(r.channel||''))}">`,
      `<span class="monospace" title="${esc(r.host||'')}">${esc(r.host||'')}</span>`,
      `<span style="text-align:center;display:block">${esc(String(r.channel??''))}</span>`,
      `<strong title="${esc(r.title||'')}">${esc(r.title||'')}</strong>`,
      `<span title="${esc(r.local||'')}">${esc(r.local||'')}</span>`,
      (r.status||'').toLowerCase()==='online'
        ? `<span style="color:var(--primary);font-weight:600;font-size:12px">online</span>`
        : `<span style="color:var(--danger);font-weight:600;font-size:12px">offline</span>`,
      recImgbbCell(r),
      `<span class="monospace text-muted" title="${esc(r.switch_ip||'')}">${esc(r.switch_ip||'')}</span>`,
      `<span class="text-muted" title="${esc(r.switch_port||'')}">${esc(r.switch_port||'')}</span>`,
      `<span class="text-muted" title="${esc(r.switch_vlan||'')}">${esc(r.switch_vlan||'')}</span>`,
      `<span class="monospace text-muted" title="${esc(r.mac||'')}" style="font-size:11px">${esc(r.mac||'')}</span>`,
    ],
  },
};

async function enrichRecRowsForMode(rows, mode) {
  if (!['olt', 'switch'].includes(mode) || !rows.length) return rows;
  const camMode = mode === 'switch' ? 'switch' : 'olt';
  const cacheBust = Date.now();
  const [camData, oltData] = await Promise.all([
    apiJson(`/api/cameras?mode=${encodeURIComponent(camMode)}&_=${cacheBust}`, { forceRefresh: true }),
    mode === 'olt' ? apiJson(`/api/olt/rows?compact=true&_=${cacheBust}`, { forceRefresh: true }) : Promise.resolve(null),
  ]);
  const camByIp = {};
  (camData?.cameras || []).forEach(c => { if (c.ip) camByIp[c.ip] = c; });
  const oltByMac = {};
  if (oltData) (oltData?.rows || []).forEach(r => { if (r.cpe_mac) oltByMac[r.cpe_mac.toLowerCase()] = r; });

  return rows.map(r => {
    const cam = camByIp[r.camera_ip] || {};
    const mac = (r.camera_mac || r.mac || '').toLowerCase();
    const olt = oltByMac[mac] || {};
    return mode === 'olt'
      ? { ...r, pon: cam.pon || olt.pon || '', onu_id: cam.onu_id || olt.onu_id || '', onu_name: cam.onu_name || olt.onu_name || '', onu_serial: cam.onu_serial || olt.onu_serial || '' }
      : { ...r, switch_ip: cam.switch_ip || '', switch_port: cam.switch_port || '', switch_vlan: cam.switch_vlan || '' };
  });
}

function setNvrView(view) {
  _invNvrView = view;
  try { sessionStorage.setItem('so_rec_view', view); } catch {}
  document.querySelectorAll('[data-nvr-view]').forEach(t =>
    t.classList.toggle('active', t.dataset.nvrView === view)
  );
  applyNvrFilters();
}

function updateNvrTabs() {
  const store = _currentRecStore();
  document.querySelectorAll('[data-nvr-view]').forEach(t => {
    t.style.display = ['basico', 'olt', 'switch'].includes(t.dataset.nvrView) || store[t.dataset.nvrView]?.length > 0 ? '' : 'none';
  });
  if (!['basico', 'olt', 'switch'].includes(_invNvrView)) setNvrView('basico');
}

function setRecType(type) {
  _recType = type;
  try { sessionStorage.setItem('so_rec_type', type); } catch {}
  document.querySelectorAll('[data-rec-type]').forEach(t =>
    t.classList.toggle('active', t.dataset.recType === type)
  );
  updateNvrTabs();
  populateNvrFilters();
  applyNvrFilters();
}

async function loadInvNvr() {
  _recSessionLoad();
  // Carrega e distribui todos os modos. Assim uma base somente OLT/Switch nao
  // fica invisivel por a tela ter iniciado na aba Basico.
  const loaded = await _loadRecAllModesForType(_recType);
  if (!loaded.length) {
    const fallbackType = _recType === 'nvr' ? 'dvr' : 'nvr';
    const fallbackRows = await _loadRecAllModesForType(fallbackType);
    if (fallbackRows.length) setRecType(fallbackType);
  }
  updateNvrTabs();
  populateNvrFilters();
  applyNvrFilters();
}

function _currentRecStore() { return _recType === 'dvr' ? _invDvr : _invNvr; }
function recIsSyntheticFreeChannel(row) {
  return _recType === 'nvr'
    && _isBlankValue(row?.camera_ip)
    && /(^|\s|-)livre($|\s|-)/i.test(String(row?.title || ''));
}

function recRowInventoryMode(row) {
  const value = String(row?.inventory_mode || '').trim().toLowerCase();
  if (value === 'olt') return 'olt';
  if (value === 'switch') return 'switch';
  return 'basico';
}
function _currentNvrRows() { return (_currentRecStore()[_invNvrView] || []).filter(row => !recIsSyntheticFreeChannel(row)); }
function _currentColDef()  { return (_recType === 'dvr' ? DVR_COLS : NVR_COLS)[_invNvrView] || NVR_COLS.basico; }

function populateNvrFilters() {
  const all    = Object.values(_currentRecStore()).flat();
  const locais = [...new Set(all.map(r => r.local).filter(Boolean))].sort();
  const hosts  = [...new Set(all.map(r => r.host).filter(Boolean))].sort(compareIpv4);

  const selLocal = document.getElementById('filterNvrLocal');
  const selHost  = document.getElementById('filterNvrHost');
  if (selLocal) {
    const cur = selLocal.value;
    selLocal.innerHTML = '<option value="">Todos os locais</option>' +
      locais.map(l => `<option${l===cur?' selected':''}>${esc(l)}</option>`).join('');
  }
  if (selHost) {
    const cur = selHost.value;
    const label = _recType === 'dvr' ? 'Todos os DVRs' : 'Todos os NVRs';
    selHost.innerHTML = `<option value="">${label}</option>` +
      hosts.map(h => `<option${h===cur?' selected':''}>${esc(h)}</option>`).join('');
  }

  const rows   = _currentNvrRows();
  const online = rows.filter(r => r.status==='online').length;
  const vloss  = rows.filter(r => r.video_loss).length;
  setText('nvrTotal',   rows.length);
  setText('nvrOnline',  online);
  setText('nvrOffline', rows.length - online);
  setText('nvrVloss',   vloss);
}

function applyNvrFilters() {
  const q      = (document.getElementById('searchInvNvr')?.value || '').toLowerCase();
  const status = document.getElementById('filterNvrStatus')?.value || '';
  const local  = document.getElementById('filterNvrLocal')?.value  || '';
  const host   = document.getElementById('filterNvrHost')?.value   || '';

  const filtered = _currentNvrRows().filter(r => {
    if (status === 'video_loss' && !r.video_loss) return false;
    else if (status === 'missing_data' && !recHasMissingData(r)) return false;
    else if (status === 'default_title' && !recHasDefaultTitle(r)) return false;
    else if (status === 'imgbb_down' && recImgbbUrl(r)) return false;
    else if (status === 'no_camera' && !recHasNoCamera(r)) return false;
    else if (status === 'no_olt' && ![r.pon, r.onu_id, r.onu_name, r.onu_serial].some(_isBlankValue)) return false;
    else if (status && !['video_loss','missing_data','default_title','imgbb_down','no_camera','no_olt'].includes(status) && (r.status||'').toLowerCase() !== status) return false;
    if (local  && r.local !== local) return false;
    if (host   && r.host  !== host)  return false;
    if (q) return [r.host, r.nvr_model, r.modelo, r.model, r.title, r.local, r.camera_ip, r.camera_model, r.camera_mac, r.mac, r.equip_serial, r.onu_name, r.onu_serial, String(r.channel)]
      .some(f => (f||'').toLowerCase().includes(q));
    return true;
  });

  filtered.sort((a, b) => {
    const hostCmp = compareIpv4(a.host, b.host);
    if (hostCmp) return hostCmp;
    const channelCmp = Number(a.channel || 0) - Number(b.channel || 0);
    if (channelCmp) return channelCmp;
    const aFree = recHasNoCamera(a) ? 1 : 0;
    const bFree = recHasNoCamera(b) ? 1 : 0;
    return aFree - bFree;
  });
  renderNvrTable(filtered);
}

function renderNvrTable(rows) {
  const def   = _currentColDef();
  const tbody = document.getElementById('invNvrTable');
  const table = tbody?.closest('table');
  // Colunas em %: nunca forcar min-width em px (isso e o que garante zero scroll horizontal).
  if (table) table.style.minWidth = '';
  setText('invNvrFooter', `${rows.length} canal${rows.length!==1?'s':''}`);

  // Atualiza colgroup e thead
  if (table) {
    const cg = table.querySelector('colgroup');
    if (cg) cg.innerHTML = def.cols.map(w => `<col style="width:${w}">`).join('');
    const th = table.querySelector('thead tr');
    if (th) th.innerHTML = def.heads.map((h, i) =>
      i === 0 ? `<th><input type="checkbox" id="chkNvrAll"></th>` : `<th>${h}</th>`
    ).join('');
  }

  if (!rows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${def.cols.length}">Nenhum resultado.</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const isOnline = (r.status||'').toLowerCase() === 'online';
    const cells = def.row(r);
    return `<tr class="inv-nvr-row${isOnline?'':' nvr-row-offline'}" data-key="${esc(r.host+'_'+r.channel)}" style="cursor:pointer">
      ${cells.map((cell, i) =>
        i === 0
          ? `<td onclick="event.stopPropagation()">${cell}</td>`
          : `<td>${cell}</td>`
      ).join('')}
    </tr>`;
  }).join('');

  document.getElementById('chkNvrAll').onchange = function() {
    document.querySelectorAll('.chk-nvr').forEach(c => c.checked = this.checked);
  };

  tbody.querySelectorAll('.inv-nvr-row').forEach(tr => {
    tr.addEventListener('click', () => {
      const [host, channel] = (tr.dataset.key || '_').split('_');
      const row = rows.find(r => String(r.host || '') === host && String(r.channel || '') === String(channel));
      if (row) openRecPanel(row);
    });
  });

  lucide.createIcons();
}

// Inventario Windows
function recEndpointBase() {
  return _recActive?._type === 'dvr' ? '/api/dvr' : '/api/nvr';
}

function recTypeName(type = _recActive?._type) {
  return type === 'dvr' ? 'Analogico (DVR)' : 'NVR  IP';
}

function recSnapshotUrl(r) {
  const raw = r?.snapshot_url || '';
  if (raw.startsWith('http://') || raw.startsWith('https://')) return raw;
  if (raw.startsWith('/')) return `${API_BASE}${raw}`;
  const file = r?.snapshot_file || '';
  if (file) return `${API_BASE}/data/${file}`;
  return '';
}

function openRecPanel(row) {
  _recActive = { ...row, _type: _recType };
  const r = _recActive;
  const isOnline = (r.status || '').toLowerCase() === 'online';
  const status = document.getElementById('recPanelStatus');
  status.textContent = r.status || '';
  status.style.color = isOnline ? 'var(--primary)' : 'var(--danger)';
  setText('recPanelTitulo', r.title || `${r.host} CH${r.channel}`);
  setText('rpTipo', recTypeName(r._type));
  setText('rpHost', r.host || '');
  setText('rpChannel', r.channel || '');
  setText('rpLocal', r.local || '');
  setText('rpModelo', r._type === 'dvr' ? (r.modelo || r.model || '') : (r.camera_model || r.modelo || r.nvr_model || ''));
  setText('rpCameraIp', r._type === 'dvr' ? 'analogico' : (r.camera_ip || ''));
  setText('rpMac', r._type === 'dvr' ? (r.mac || '') : (r.camera_mac || r.mac || ''));
  setText('rpPonOnu', [r.pon, r.onu_id].filter(Boolean).join(' / ') || '');
  setText('rpSerial', r.equip_serial || r.onu_serial || '');
  setText('rpSnapshotTitle', r.title || `${r.host} CH${r.channel}`);
  setText('rpSnapshotTime', '');

  const img = document.getElementById('rpSnapshot');
  const empty = document.getElementById('rpSnapshotEmpty');
  const url = recSnapshotUrl(r);
  if (url) {
    img.src = `${url}${url.includes('?') ? '&' : '?'}t=${Date.now()}`;
    img.style.display = 'block';
    empty.style.display = 'none';
  } else {
    img.src = '';
    img.style.display = 'none';
    empty.style.display = 'flex';
  }

  document.querySelectorAll('.inv-nvr-row').forEach(tr => tr.classList.toggle('row-selected', tr.dataset.key === `${r.host}_${r.channel}`));
  document.getElementById('recPanelBackdrop')?.classList.remove('hidden');
  document.getElementById('recPanel')?.classList.remove('hidden');
  lucide.createIcons();
}

function closeRecPanel() {
  _recActive = null;
  document.getElementById('recPanelBackdrop')?.classList.add('hidden');
  document.getElementById('recPanel')?.classList.add('hidden');
  document.querySelectorAll('.inv-nvr-row').forEach(tr => tr.classList.remove('row-selected'));
}

function recActionGroups(...ids) {
  ['recActionTitleGroup','recActionIpGroup','recActionPassGroup','recActionNtpGroup'].forEach(id => {
    document.getElementById(id)?.classList.toggle('hidden', !ids.includes(id));
  });
}

function openRecAction(action) {
  if (!_recActive) return;
  _recAction = action;
  const r = _recActive;
  const labels = {
    snapshot: { title: 'Atualizar snapshot', icon: 'refresh-cw', label: 'Atualizar' },
    rename: { title: 'Renomear canal', icon: 'pencil', label: 'Renomear' },
    ip: { title: 'Trocar IP', icon: 'network', label: 'Aplicar IP' },
    password: { title: 'Trocar senha', icon: 'key', label: 'Trocar senha' },
    datetime: { title: 'Data/hora', icon: 'clock', label: 'Aplicar NTP' },
    reboot: { title: 'Reboot do gravador', icon: 'power', label: 'Reboot' },
  };
  const meta = labels[action] || labels.snapshot;
  setText('recActionEyebrow', `${recTypeName(r._type)}  ${r.host} CH${r.channel}`);
  setText('recActionTitle', meta.title);
  document.getElementById('recActionTarget').value = `${r.host}  canal ${r.channel}  ${r.title || ''}`;
  document.getElementById('recActionUser').value = 'admin';
  document.getElementById('recActionPass').value = '';
  document.getElementById('recActionErro').hidden = true;
  document.getElementById('recActionChannelTitle').value = r.title || '';
  document.getElementById('recActionNewIp').value = '';
  document.getElementById('recActionMask').value = '';
  document.getElementById('recActionGateway').value = '';
  document.getElementById('recActionNewPass').value = '';
  document.getElementById('recActionNewPassConfirm').value = '';
  document.getElementById('recActionNtp').value = 'pool.ntp.org';
  recActionGroups(
    ...(action === 'rename' ? ['recActionTitleGroup'] : []),
    ...(action === 'ip' ? ['recActionIpGroup'] : []),
    ...(action === 'password' ? ['recActionPassGroup'] : []),
    ...(action === 'datetime' ? ['recActionNtpGroup'] : []),
  );
  setText('recActionNewIpLabel', r._type === 'nvr' && r.camera_ip ? 'Novo IP da camera do canal' : 'Novo IP do gravador');
  const btn = document.getElementById('confirmRecAction');
  btn.innerHTML = `<i data-lucide="${meta.icon}"></i> ${meta.label}`;
  btn.disabled = false;
  document.getElementById('modalRecAction').classList.remove('hidden');
  setTimeout(() => {
    const first = action === 'rename' ? 'recActionChannelTitle' : action === 'ip' ? 'recActionNewIp' : 'recActionPass';
    document.getElementById(first)?.focus();
  }, 80);
  lucide.createIcons();
}

function closeRecAction() {
  document.getElementById('modalRecAction')?.classList.add('hidden');
  _recAction = null;
}

async function runRecAction() {
  if (!_recActive || !_recAction) return;
  const r = _recActive;
  const user = document.getElementById('recActionUser').value.trim() || 'admin';
  const password = document.getElementById('recActionPass').value;
  const erro = document.getElementById('recActionErro');
  const btn = document.getElementById('confirmRecAction');
  if (!password) { erro.textContent = 'Informe a senha do gravador.'; erro.hidden = false; return; }

  const base = recEndpointBase();
  const common = { ip: r.host, http_port: Number(r.http_port || 80), channel: Number(r.channel || 1), user, password };
  let path = '';
  let payload = {};

  if (_recAction === 'snapshot') {
    path = `${base}/snapshot/update`;
    payload = { ...common, imgbb: false };
  } else if (_recAction === 'rename') {
    const title = document.getElementById('recActionChannelTitle').value.trim();
    if (!title) { erro.textContent = 'Informe o titulo do canal.'; erro.hidden = false; return; }
    path = `${base}/channel/rename`;
    payload = { ...common, title };
  } else if (_recAction === 'ip') {
    const newIp = document.getElementById('recActionNewIp').value.trim();
    if (!newIp) { erro.textContent = 'Informe o novo IP.'; erro.hidden = false; return; }
    if (r._type === 'nvr' && r.camera_ip) {
      path = `${base}/channel/change_ip`;
      payload = { ...common, new_ip: newIp };
    } else {
      path = `${base}/change_ip`;
      payload = {
        ...common,
        new_ip: newIp,
        mask: document.getElementById('recActionMask').value.trim(),
        gateway: document.getElementById('recActionGateway').value.trim(),
      };
    }
  } else if (_recAction === 'password') {
    const newPass = document.getElementById('recActionNewPass').value;
    const conf = document.getElementById('recActionNewPassConfirm').value;
    if (!newPass) { erro.textContent = 'Informe a nova senha.'; erro.hidden = false; return; }
    if (newPass !== conf) { erro.textContent = 'As senhas nao coincidem.'; erro.hidden = false; return; }
    path = '/api/maintenance/batch/password';
    payload = { ips: [r.host], user, old_pass: password, new_pass: newPass };
  } else if (_recAction === 'datetime') {
    const ntp = document.getElementById('recActionNtp').value.trim() || 'pool.ntp.org';
    path = `${base}/ntp`;
    payload = { ...common, address: ntp };
  } else if (_recAction === 'reboot') {
    path = `${base}/reboot`;
    payload = common;
  }

  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i data-lucide="loader-2"></i> Executando';
  lucide.createIcons();
  try {
    const res = await api(path, { method: 'POST', body: JSON.stringify(payload) });
    const body = await res?.json().catch(() => ({}));
    if (!res?.ok || body?.ok === false) throw new Error(body?.detail || body?.error || 'Falha ao executar acao.');
    closeRecAction();
    showToast('Acao concluida.');
    if (_recAction === 'rename') {
      applyRecPayloadsLocally([{ host: r.host, channel: r.channel, title: payload.title }], r._type);
      _recActive = { ..._recActive, title: payload.title };
    } else if (_recAction === 'ip' && r._type === 'nvr' && r.camera_ip) {
      applyRecPayloadsLocally([{ host: r.host, channel: r.channel, camera_ip: payload.new_ip }], r._type);
      _recActive = { ..._recActive, camera_ip: payload.new_ip };
    }
    updateNvrTabs();
    populateNvrFilters();
    applyNvrFilters();
    if (_recActive) openRecPanel(_recActive);
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

function recPanelAction(action) {
  if (!_recActive) return;
  if (action === 'web') { window.open(`${API_BASE}/api/maintenance/web/${encodeURIComponent(_recActive.host)}/`, '_blank'); return; }
  if (action === 'ping') { openPingTerminal(_recActive.host); return; }
  openRecAction(action);
}

function selectedRecItems() {
  return [...document.querySelectorAll('.chk-nvr:checked')].map(c => ({
    host: c.dataset.host || '',
    channel: Number(c.dataset.channel || 0),
    mode: _invNvrView || 'basico',
  })).filter(x => x.host && x.channel > 0);
}

function selectedRecRows() {
  const keys = new Set(selectedRecItems().map(x => `${x.host}|${x.channel}`));
  return _currentNvrRows().filter(r => keys.has(`${r.host}|${Number(r.channel || 0)}`));
}

async function runNvrReport(button) {
  if (!button || button.disabled) return;
  const visibleItems = [...document.querySelectorAll('#invNvrTable .inv-nvr-row')].map(tr => {
    const [host, channel] = String(tr.dataset.key || '|').split('_');
    return { host, channel: Number(channel || 0) };
  }).filter(x => x.host && x.channel > 0);
  const selected = selectedRecItems();
  const items = selected.length ? selected : visibleItems;
  if (!items.length) {
    showToast('Nenhum canal para gerar relatorio.', true);
    return;
  }

  const oldHtml = button.innerHTML;
  const oldTip = button.dataset.tip;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.innerHTML = '<i data-lucide="loader-2" class="spin"></i>';
  lucide.createIcons();
  try {
    const params = new URLSearchParams({ mode: _invNvrView || 'basico' });
    const site = document.getElementById('filterNvrLocal')?.value || '';
    if (site) params.set('site', site);
    params.set('items', items.map(x => `${x.host}|${x.channel}`).join(','));
    const endpoint = _recType === 'dvr' ? '/api/dvr/report.pdf' : '/api/nvr/report.pdf';
    showToast('Gerando relatorio de gravadores...');
    const res = await api(`${endpoint}?${params.toString()}`);
    if (!res || !res.ok) {
      const txt = await res?.text().catch(() => '');
      throw new Error(txt.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 180) || 'Nao foi possivel gerar o relatorio.');
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `relatorio-gravadores-${_recType}-${_invNvrView || 'basico'}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Relatorio gerado. O download foi iniciado.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel gerar o relatorio.', true);
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    button.dataset.tip = oldTip || 'Relatorio PDF';
    button.innerHTML = oldHtml;
    lucide.createIcons();
  }
}

function applyRecPayloadsLocally(payloads, type = _recType) {
  const store = type === 'dvr' ? _invDvr : _invNvr;
  ['basico', 'olt', 'switch'].forEach(mode => {
    if (!store[mode]?.length) return;
    store[mode] = store[mode].map(row => {
      const patch = payloads.find(p =>
        String(p.host || '') === String(row.host || '') &&
        Number(p.channel || 0) === Number(row.channel || 0)
      );
      return patch ? { ...row, ...patch } : row;
    });
    _recSessionSave(type, mode, store[mode]);
  });
}

function openEditRecModal(rows) {
  const count = rows.length;
  const typeLabel = _recType === 'dvr' ? 'DVR' : 'NVR';
  document.getElementById('modalEditRecTitle').textContent =
    count === 1 ? `Editar ${typeLabel}  ${rows[0].host} ch${rows[0].channel}` : `Editar ${count} canais ${typeLabel}`;
  document.getElementById('editRecErro').hidden = true;

  const s = 'width:100%;padding:4px 6px;border:1px solid var(--border);border-radius:5px;font-size:12px;font-family:inherit;background:var(--surface);color:var(--text);outline:none;box-sizing:border-box';
  document.getElementById('editRecTableBody').innerHTML = rows.map(r => {
    const model = _recType === 'dvr' ? (r.modelo || r.model || '') : (r.camera_model || r.modelo || '');
    const camIp = _recType === 'dvr' ? '' : (r.camera_ip || '');
    const mac = _recType === 'dvr' ? (r.mac || '') : (r.camera_mac || r.mac || '');
    return `
      <tr>
        <td class="monospace" style="font-size:11px;color:var(--muted);white-space:nowrap">${esc(r.host || '')}</td>
        <td class="monospace" style="font-size:11px;color:var(--muted);text-align:center">${esc(String(r.channel || ''))}</td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="title" style="${s}" value="${esc(r.title || '')}" placeholder="Titulo"></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="local" style="${s}" value="${esc(r.local || '')}" placeholder="Local"></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="${_recType === 'dvr' ? 'modelo' : 'camera_model'}" style="${s}" value="${esc(model)}" placeholder="Modelo"></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="camera_ip" style="${s};font-family:monospace" value="${esc(camIp)}" placeholder="IP camera"${_recType === 'dvr' ? ' disabled' : ''}></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="${_recType === 'dvr' ? 'mac' : 'camera_mac'}" style="${s};font-family:monospace" value="${esc(mac)}" placeholder="MAC"></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="pon" style="${s};text-align:center" value="${esc(r.pon || '')}" placeholder="-"></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="onu_id" style="${s};text-align:center" value="${esc(r.onu_id || '')}" placeholder="-"></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="onu_name" style="${s}" value="${esc(r.onu_name || '')}" placeholder="gpon x onu y"></td>
        <td><input data-host="${esc(r.host || '')}" data-channel="${esc(String(r.channel || ''))}" data-field="onu_serial" style="${s};font-family:monospace" value="${esc(r.onu_serial || '')}" placeholder="ONU Serial"></td>
      </tr>`;
  }).join('');

  document.getElementById('modalEditRec').classList.remove('hidden');
  lucide.createIcons();
}

function closeEditRecModal() {
  document.getElementById('modalEditRec').classList.add('hidden');
}

async function saveEditRec() {
  const rows = document.querySelectorAll('#editRecTableBody tr');
  const byKey = {};
  rows.forEach(tr => {
    tr.querySelectorAll('input[data-host]').forEach(inp => {
      const host = inp.dataset.host || '';
      const channel = Number(inp.dataset.channel || 0);
      if (!host || !channel) return;
      const key = `${host}|${channel}`;
      byKey[key] ||= { host, channel };
      if (!inp.disabled) byKey[key][inp.dataset.field] = inp.value.trim();
    });
  });
  const payloads = Object.values(byKey);
  if (!payloads.length) return;

  const btn = document.getElementById('saveEditRec');
  btn.disabled = true;
  btn.textContent = 'Salvando...';
  const endpoint = _recType === 'dvr' ? '/api/dvr/save' : '/api/nvr/save';
  const res = await api(endpoint, { method: 'POST', body: JSON.stringify({ recorders: payloads }) });
  const body = await res?.json().catch(() => ({}));
  btn.disabled = false;
  btn.innerHTML = '<i data-lucide="check"></i> Salvar tudo';
  lucide.createIcons();

  if (!res?.ok || body?.ok === false) {
    const el = document.getElementById('editRecErro');
    el.textContent = body?.detail || body?.error || 'Canais nao foram salvos. Verifique e tente novamente.';
    el.hidden = false;
    return;
  }

  showToast(`${payloads.length} canal(is) salvo(s)!`);
  closeEditRecModal();
  applyRecPayloadsLocally(payloads);
  updateNvrTabs();
  populateNvrFilters();
  applyNvrFilters();
}

let _winRows = [];
let _winFilteredRows = [];
let _winSelected = new Set();
let _winEditingKey = '';
let _winActive = null;

