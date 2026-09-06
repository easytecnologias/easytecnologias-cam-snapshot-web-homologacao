function _drawerFilterBar(statusFilters, activeStatusKey, sites, activeSite, onStatusSelect, onSiteSelect) {
  const el = document.getElementById('dashDrawerFilters');
  const statusHtml = `<div class="drawer-filter-row">` +
    statusFilters.map(f =>
      `<button class="drawer-filter-btn${f.key === activeStatusKey ? ' active' : ''}" data-filter="${f.key}">${f.label}${f.count != null ? ` (${f.count})` : ''}</button>`
    ).join('') + `</div>`;
  const siteHtml = sites.length
    ? `<div class="drawer-site-filter">
        <label for="dashDrawerSiteSelect"><i data-lucide="map-pin"></i><span>Site</span></label>
        <select id="dashDrawerSiteSelect" class="drawer-site-select">
          <option value="">Todos os sites</option>
          ${sites.map(s => `<option value="${esc(s)}"${s === activeSite ? ' selected' : ''}>${esc(s)}</option>`).join('')}
        </select>
      </div>`
    : '';
  el.innerHTML = statusHtml + siteHtml;
  el.querySelectorAll('.drawer-filter-btn[data-filter]').forEach(btn => btn.addEventListener('click', () => onStatusSelect(btn.dataset.filter)));
  el.querySelector('#dashDrawerSiteSelect')?.addEventListener('change', event => onSiteSelect(event.target.value || null));
}

function _drawerRenderRows(html) {
  document.getElementById('dashDrawerBody').innerHTML = html || '<div style="padding:32px 20px;text-align:center;color:var(--muted);font-size:13px">Nenhum item encontrado.</div>';
  lucide.createIcons();
}

async function openDashDrawerIp(filterKey, activeSite) {
  filterKey  = filterKey  || 'all';
  activeSite = activeSite || null;
  _openDashDrawer('Inventario', 'Cameras IP');
  if (!_dashDrawerData?.ip) {
    const [basicRes, oltRes, switchRes] = await Promise.all([
      apiJson('/api/cameras?mode=basico').catch(() => ({ cameras: [] })),
      apiJson('/api/cameras?mode=olt').catch(() => ({ cameras: [] })),
      apiJson('/api/cameras?mode=switch').catch(() => ({ cameras: [] })),
    ]);
    if (!_dashDrawerData) _dashDrawerData = {};
    _dashDrawerData.ip = [
      ...(basicRes?.cameras || []).map(r => ({ ...r, _dashMode: 'basico' })),
      ...(oltRes?.cameras || []).map(r => ({ ...r, _dashMode: 'olt' })),
      ...(switchRes?.cameras || []).map(r => ({ ...r, _dashMode: 'switch' })),
    ];
  }
  const rows = _dashDrawerData.ip;
  const isOnline  = r => ['online','ok','up','ativo','active'].includes((r.status||'').toLowerCase());
  const isOffline = r => ['offline','down','inativo','inactive','auth_failed','timeout','erro','error'].includes((r.status||'').toLowerCase());
  const noSnap    = r => !r.snapshot_url && !r.imgbb_url;
  const rowSite   = r => String(r.local || r.site || r.site_name || '').trim();

  const sites = [...new Set(rows.map(rowSite).filter(Boolean))].sort((a,b) => a.localeCompare(b,'pt'));
  if (activeSite && !sites.includes(activeSite)) activeSite = null;
  const siteRows = activeSite ? rows.filter(r => rowSite(r) === activeSite) : rows;
  const counts = { all: siteRows.length, online: siteRows.filter(isOnline).length, offline: siteRows.filter(isOffline).length, no_snap: siteRows.filter(noSnap).length };

  _drawerFilterBar(
    [{ key:'all', label:'Todos', count:counts.all }, { key:'online', label:' Online', count:counts.online },
     { key:'offline', label:' Offline', count:counts.offline }, { key:'no_snap', label:'Sem snapshot', count:counts.no_snap }],
    filterKey, sites, activeSite,
    k => openDashDrawerIp(k, activeSite),
    s => openDashDrawerIp(filterKey, s)
  );

  let filtered = siteRows;
  if (filterKey === 'online')  filtered = filtered.filter(isOnline);
  if (filterKey === 'offline') filtered = filtered.filter(isOffline);
  if (filterKey === 'no_snap') filtered = filtered.filter(noSnap);

  filtered.sort((a, b) => (a.titulo || a.ip || '').localeCompare(b.titulo || b.ip || '', 'pt', { numeric: true }));
  _drawerRenderRows(filtered.map(r => {
    const ip = esc(r.ip || '');
    const view = r._dashMode === 'switch' ? 'inv-switch' : 'inv-olt';
    return `<div class="drawer-item" style="cursor:pointer" onclick="_drawerGoToInventory('${view}','${ip}','${esc(r._dashMode || 'olt')}')" title="Abrir no inventario">
      ${_drawerStatusDot(r.status)}
      <div class="drawer-item-main">
        <div class="drawer-item-title">${esc(r.titulo || r.ip || '')}</div>
        <div class="drawer-item-sub">${esc(r.ip)}  ${esc(rowSite(r))}  ${esc(r.modelo || r.model || '')}</div>
      </div>
      ${r.snapshot_url ? `<img src="${esc(r.snapshot_url)}" style="width:52px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" loading="lazy">` : '<span style="width:52px;flex-shrink:0"></span>'}
      <i data-lucide="chevron-right" style="width:13px;height:13px;color:var(--muted);flex-shrink:0"></i>
    </div>`;
  }).join(''));
}

async function refreshDashboardLiveCameraStatus() {
  showToast('Sincronizando status real pelo Zabbix...');
  const res = await api('/api/scripts/zabbix/status-sync', {
    method: 'POST',
    body: JSON.stringify({ source: 'ip', mode: 'all', site: '' }),
  });
  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || body?.error || 'Zabbix nao configurado para status das cameras.', true);
    return;
  }
  _dashDrawerData = null;
  const unknown = Number(body.unknown || 0);
  const extra = unknown ? `, ${unknown} sem dado Zabbix` : '';
  showToast(`Zabbix atualizado: ${body.online || 0}/${body.total || 0} online, ${body.offline || 0} offline${extra}.`);
  await loadDashboard();
}

async function openDashDrawerRecorder(source, filterKey, activeSite) {
  filterKey  = filterKey  || 'all';
  activeSite = activeSite || null;
  const sources = source === 'all' ? ['dvr', 'nvr'] : [source === 'dvr' ? 'dvr' : 'nvr'];
  const label = source === 'all' ? 'DVR/NVR' : (source === 'dvr' ? 'DVR' : 'NVR');
  _openDashDrawer('Gravadores', `Canais ${label}`);
  if (!_dashDrawerData) _dashDrawerData = {};
  for (const src of sources) {
    if (!_dashDrawerData?.[src]) {
      const res = await apiJson(`/api/${src}/inventory`);
      _dashDrawerData[src] = (res?.inventory || []).map(row => ({ ...row, _dashRecorderSource: src }));
    }
  }
  const rows = sources.flatMap(src => _dashDrawerData[src] || []);
  const isOnline  = r => ['online','ok','up','ativo','active'].includes((r.status||'').toLowerCase());
  const isOffline = r => ['offline','down','inativo','inactive','auth_failed','timeout','erro','error','video_loss'].includes((r.status||'').toLowerCase());
  const rowSite   = r => String(r.local || r.site || r.site_name || '').trim();

  // Mesmo nome de gravador (DVR-NN ou o que o usuario ja batizou) usado na
  // tela Gravadores -- calculado aqui de novo porque o dashboard carrega
  // seu proprio snapshot do inventario (_dashDrawerData), independente do
  // estado da tela de Gravadores.
  const dvrNameByHost = (() => {
    const siteByHost = {};
    const nameByHost = {};
    rows.forEach(r => {
      const host = r.host || r.ip || '';
      if (!host) return;
      if (!siteByHost[host]) siteByHost[host] = rowSite(r);
      if (!nameByHost[host] && r.recorder_name) nameByHost[host] = r.recorder_name;
    });
    const hostsBySite = {};
    Object.keys(siteByHost).forEach(host => {
      (hostsBySite[siteByHost[host]] = hostsBySite[siteByHost[host]] || []).push(host);
    });
    const display = {};
    Object.values(hostsBySite).forEach(hosts => {
      hosts.sort((a, b) => a.localeCompare(b, 'pt', { numeric: true })).forEach((host, idx) => {
        display[host] = nameByHost[host] || `DVR-${String(idx + 1).padStart(2, '0')}`;
      });
    });
    return display;
  })();
  const dvrChannelLabel = r => {
    const titulo = r.title || r.titulo || '';
    if (titulo && !camHasDefaultTitle({ titulo })) return titulo;
    return `Camera ${String(r.channel || 0).padStart(2, '0')}`;
  };

  const sites = [...new Set(rows.map(rowSite).filter(Boolean))].sort((a,b) => a.localeCompare(b,'pt'));
  if (activeSite && !sites.includes(activeSite)) activeSite = null;
  const siteRows = activeSite ? rows.filter(r => rowSite(r) === activeSite) : rows;
  const counts = { all: siteRows.length, online: siteRows.filter(isOnline).length, offline: siteRows.filter(isOffline).length };

  _drawerFilterBar(
    [{ key:'all', label:'Todos', count:counts.all }, { key:'online', label:' Online', count:counts.online }, { key:'offline', label:' Offline', count:counts.offline }],
    filterKey, sites, activeSite,
    k => openDashDrawerRecorder(source, k, activeSite),
    s => openDashDrawerRecorder(source, filterKey, s)
  );

  let filtered = siteRows;
  if (filterKey === 'online')  filtered = filtered.filter(isOnline);
  if (filterKey === 'offline') filtered = filtered.filter(isOffline);

  filtered.sort((a, b) => {
    const hostCmp = (a.host||a.ip||'').localeCompare(b.host||b.ip||'', 'pt');
    return hostCmp !== 0 ? hostCmp : (a.channel||0) - (b.channel||0);
  });
  _drawerRenderRows(filtered.map(r => {
    const host = esc(r.host||r.ip||'');
    const src = r._dashRecorderSource || source || 'nvr';
    const view = src === 'dvr' ? 'inv-dvr' : 'inv-nvr';
    const typeBadge = source === 'all' ? `<span class="drawer-mini-badge">${src === 'dvr' ? 'DVR' : 'NVR'}</span>` : '';
    const dvrNome = dvrNameByHost[r.host || r.ip || ''] || (r.host || r.ip || '');
    return `<div class="drawer-item" style="cursor:pointer" onclick="_drawerGoToInventory('${view}','${host}')" title="Abrir no inventario">
      ${_drawerStatusDot(r.status)}
      <div class="drawer-item-main">
        <div class="drawer-item-title">CH${String(r.channel||0).padStart(2,'0')} ${typeBadge} ${esc(dvrChannelLabel(r))}</div>
        <div class="drawer-item-sub">${esc(dvrNome)}  ${esc(rowSite(r))}</div>
      </div>
      ${r.snapshot_url ? `<img src="${esc(r.snapshot_url)}" style="width:52px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" loading="lazy">` : '<span style="width:52px;flex-shrink:0"></span>'}
      <i data-lucide="chevron-right" style="width:13px;height:13px;color:var(--muted);flex-shrink:0"></i>
    </div>`;
  }).join(''));
}

async function openDashDrawerWindows(filterKey) {
  filterKey = filterKey || 'all';
  _openDashDrawer('Inventario', 'Windows');
  if (!_dashDrawerData?.windows) {
    const res = await apiJson('/api/windows/inventory');
    if (!_dashDrawerData) _dashDrawerData = {};
    _dashDrawerData.windows = res?.inventory || [];
  }
  const rows = _dashDrawerData.windows;
  const isOnline  = r => (r.status||'').toLowerCase() === 'online';
  const isOffline = r => ['offline','error','erro'].includes((r.status||'').toLowerCase());

  const counts = { all: rows.length, online: rows.filter(isOnline).length, offline: rows.filter(isOffline).length };
  _drawerFilterBar(
    [{ key:'all', label:'Todos', count:counts.all }, { key:'online', label:' Online', count:counts.online }, { key:'offline', label:' Offline', count:counts.offline }],
    filterKey, [], null,
    k => openDashDrawerWindows(k), () => {}
  );

  let filtered = rows;
  if (filterKey === 'online')  filtered = filtered.filter(isOnline);
  if (filterKey === 'offline') filtered = filtered.filter(isOffline);

  filtered.sort((a, b) => (a.hostname || a.ip || '').localeCompare(b.hostname || b.ip || '', 'pt', { numeric: true }));
  _drawerRenderRows(filtered.map(r => `
    <div class="drawer-item" style="cursor:pointer" onclick="_drawerGoToInventory('inv-windows','')" title="Abrir no inventario">
      ${_drawerStatusDot(r.status)}
      <div class="drawer-item-main">
        <div class="drawer-item-title">${esc(r.hostname || r.ip || '')}</div>
        <div class="drawer-item-sub">${esc(r.ip||'')}  ${esc(r.local||r.site||'')}  SSD: ${r.has_ssd ? 'Sim' : 'Nao'}</div>
      </div>
      <i data-lucide="chevron-right" style="width:13px;height:13px;color:var(--muted);flex-shrink:0"></i>
    </div>`).join(''));
}

function _dashNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function _dashPct(part, total) {
  const t = _dashNum(total);
  if (!t) return 0;
  return Math.max(0, Math.min(100, Math.round((_dashNum(part) / t) * 100)));
}

function _dashAttentionTotal(monitoring) {
  return Object.values(monitoring || {}).reduce((sum, item) => (
    sum + _dashNum(item?.down ?? item?.offline) + _dashNum(item?.unstable) + _dashNum(item?.unknown)
  ), 0);
}

function _dashPriorityAction(action) {
  if (action === 'ip_offline') return openDashDrawerIp('offline');
  if (action === 'no_snapshot') return openDashDrawerIp('no_snap');
  if (action === 'dvr_offline') return openDashDrawerRecorder('dvr', 'offline');
  if (action === 'nvr_offline') return openDashDrawerRecorder('nvr', 'offline');
  if (action === 'windows_offline') return openDashDrawerWindows('offline');
  if (action === 'onu_attention' && typeof openMonitoringDrawer === 'function') return openMonitoringDrawer('onu', 'all');
  if (action === 'olt_attention' && typeof openMonitoringDrawer === 'function') return openMonitoringDrawer('olt', 'all');
  if (action === 'connector_attention' && typeof openMonitoringDrawer === 'function') return openMonitoringDrawer('connector', 'all');
  if (action === 'map') return navigateTo('kmz');
  return null;
}

function renderDashboardPriorities(data) {
  const inv = data.inventory || {};
  const ip = inv.ip || {};
  const dvr = inv.dvr || {};
  const nvr = inv.nvr || {};
  const win = inv.windows || {};
  const monitoring = data.monitoring?.types || {};
  const attentionCount = (status) => _dashNum(status?.down ?? status?.offline) + _dashNum(status?.unstable) + _dashNum(status?.unknown);
  const priorities = [
    { label: 'Cameras offline', sub: 'Abrir cameras com falha', count: _dashNum(ip.offline), icon: 'alert-circle', level: 'danger', action: 'ip_offline' },
    { label: 'ONUs em atencao', sub: 'Down, instaveis ou nao verificadas', count: attentionCount(monitoring.onu), icon: 'wifi-off', level: 'danger', action: 'onu_attention' },
    { label: 'OLTs em atencao', sub: 'Conexao down ou ainda nao verificada', count: attentionCount(monitoring.olt), icon: 'radio-tower', level: 'danger', action: 'olt_attention' },
    { label: 'Conectores em atencao', sub: 'Conectores offline ou sem verificacao', count: attentionCount(monitoring.connector), icon: 'plug-zap', level: 'danger', action: 'connector_attention' },
    { label: 'Sem snapshot', sub: 'Cameras sem imagem recente', count: _dashNum(ip.missing_snapshot), icon: 'image-off', level: 'warning', action: 'no_snapshot' },
    { label: 'Sem local', sub: 'Itens sem site/local definido', count: _dashNum(ip.missing_local) + _dashNum(dvr.missing_local) + _dashNum(nvr.missing_local), icon: 'map-pin-off', level: 'info', action: 'map' },
    { label: 'Gravadores offline', sub: 'DVR/NVR com erro ou sem resposta', count: _dashNum(dvr.offline) + _dashNum(nvr.offline), icon: 'hard-drive', level: 'danger', action: 'dvr_offline' },
    { label: 'Windows offline', sub: 'Computadores sem resposta', count: _dashNum(win.offline), icon: 'monitor-x', level: 'info', action: 'windows_offline' },
  ].filter(item => item.count > 0).slice(0, 6);

  const el = document.getElementById('dashPriorityList');
  if (!el) return;
  if (!priorities.length) {
    el.innerHTML = `<div class="dash-priority-item" style="cursor:default">
      <span class="dash-priority-icon info"><i data-lucide="check-circle-2"></i></span>
      <span class="dash-priority-copy"><span class="dash-priority-title">Sem pendencias criticas</span><span class="dash-priority-sub">O painel nao encontrou alertas principais agora.</span></span>
      <span class="dash-priority-count">OK</span>
    </div>`;
    return;
  }
  el.innerHTML = priorities.map(item => `
    <div class="dash-priority-item" data-priority-action="${esc(item.action)}">
      <span class="dash-priority-icon ${item.level === 'warning' ? 'warning' : item.level === 'info' ? 'info' : ''}"><i data-lucide="${item.icon}"></i></span>
      <span class="dash-priority-copy"><span class="dash-priority-title">${esc(item.label)}</span><span class="dash-priority-sub">${esc(item.sub)}</span></span>
      <span class="dash-priority-count">${item.count}</span>
    </div>
  `).join('');
  el.querySelectorAll('[data-priority-action]').forEach(btn => {
    btn.addEventListener('click', () => _dashPriorityAction(btn.dataset.priorityAction));
  });
}

function renderDashboardHealth(data) {
  const inv = data.inventory || {};
  const ip = inv.ip || {};
  const dvr = inv.dvr || {};
  const nvr = inv.nvr || {};
  const monitoring = data.monitoring?.types || {};
  const recTotal = _dashNum(dvr.total) + _dashNum(nvr.total);
  const recOnline = _dashNum(dvr.online) + _dashNum(nvr.online);
  const availability = (status) => {
    const total = _dashNum(status?.total);
    const online = _dashNum(status?.up ?? status?.online);
    return { total, online, pct: total ? _dashPct(online, total) : null };
  };
  const cameras = availability(ip);
  const onus = availability(monitoring.onu);
  const olts = availability(monitoring.olt);
  const connectors = availability(monitoring.connector);
  const accessDevices = availability(monitoring.access_device);
  const whatsapp = availability(monitoring.whatsapp);
  const recorders = { total: recTotal, online: recOnline, pct: recTotal ? _dashPct(recOnline, recTotal) : null };
  const pending = _dashAttentionTotal(monitoring);
  const monitoredTotal = Object.values(monitoring).reduce((sum, item) => sum + _dashNum(item?.total), 0);
  const health = [
    { label: 'Cameras IP', value: cameras.pct == null ? '--' : `${cameras.pct}%`, sub: cameras.total ? `${cameras.online} online de ${cameras.total}` : 'nenhuma cadastrada', pct: cameras.pct, action: 'cameras' },
    { label: 'ONUs / ONTs', value: onus.pct == null ? '--' : `${onus.pct}%`, sub: onus.total ? `${onus.online} up de ${onus.total}` : 'nenhuma monitorada', pct: onus.pct, action: 'onus' },
    { label: 'OLTs', value: olts.pct == null ? '--' : `${olts.pct}%`, sub: olts.total ? `${olts.online} up de ${olts.total}` : 'nenhuma cadastrada', pct: olts.pct, action: 'olts' },
    { label: 'Conectores', value: connectors.pct == null ? '--' : `${connectors.pct}%`, sub: connectors.total ? `${connectors.online} online de ${connectors.total}` : 'nenhum cadastrado', pct: connectors.pct, action: 'connectors' },
    { label: 'Controladoras de Acesso', value: accessDevices.pct == null ? '--' : `${accessDevices.pct}%`, sub: accessDevices.total ? `${accessDevices.online} online de ${accessDevices.total}` : 'nenhuma cadastrada', pct: accessDevices.pct, action: 'access_devices' },
    { label: 'WhatsApp', value: whatsapp.pct == null ? '--' : `${whatsapp.pct}%`, sub: whatsapp.total ? `${whatsapp.online} conectado(s) de ${whatsapp.total} configurado(s)` : 'nenhum canal configurado', pct: whatsapp.pct, action: 'whatsapp' },
    { label: 'Gravadores', value: recorders.pct == null ? '--' : `${recorders.pct}%`, sub: recorders.total ? `${recorders.online} canais online de ${recorders.total}` : 'nenhum canal cadastrado', pct: recorders.pct, action: 'recorders' },
    { label: 'Pendencias', value: pending, sub: 'equipamentos exigem verificacao', pct: pending ? Math.max(8, 100 - _dashPct(pending, monitoredTotal)) : 100, action: 'attention', state: pending ? 'warning' : 'healthy' },
  ];
  const el = document.getElementById('dashHealthGrid');
  if (!el) return;
  el.innerHTML = health.map(item => `
    <button type="button" class="dash-health-card ${item.state || (item.pct == null ? 'neutral' : item.pct >= 95 ? 'healthy' : item.pct >= 75 ? 'warning' : 'critical')}" data-health-action="${item.action}">
      <div class="dash-health-label">${esc(item.label)}</div>
      <div class="dash-health-value">${esc(item.value)}</div>
      <div class="dash-health-sub">${esc(item.sub)}</div>
      <div class="dash-meter"><span style="width:${item.pct ?? 0}%"></span></div>
      <i data-lucide="chevron-right" class="dash-health-arrow"></i>
    </button>
  `).join('');
  el.querySelectorAll('[data-health-action]').forEach(card => card.addEventListener('click', () => {
    const action = card.dataset.healthAction;
    if (action === 'cameras') openDashDrawerIp('all');
    if (action === 'onus' && typeof openMonitoringDrawer === 'function') openMonitoringDrawer('onu', 'all');
    if (action === 'olts' && typeof openMonitoringDrawer === 'function') openMonitoringDrawer('olt', 'all');
    if (action === 'connectors' && typeof openMonitoringDrawer === 'function') openMonitoringDrawer('connector', 'all');
    if (action === 'access_devices' && typeof openMonitoringDrawer === 'function') openMonitoringDrawer('access_device', 'all');
    if (action === 'whatsapp' && typeof openMonitoringDrawer === 'function') openMonitoringDrawer('whatsapp', 'all');
    if (action === 'recorders') openDashDrawerRecorder('all', 'all');
    if (action === 'attention' && typeof openMonitoringAttentionDrawer === 'function') openMonitoringAttentionDrawer();
  }));
}

function renderDashboardSiteSummary(data) {
  const rows = Array.isArray(data.site_summary) ? data.site_summary.slice(0, 8) : [];
  const el = document.getElementById('dashSiteSummary');
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = rows.map(site => `
    <div class="dash-site-card" data-dash-site="${esc(site.site || '')}">
      <div class="dash-site-head">
        <span class="dash-site-name" title="${esc(site.site || '')}">${esc(site.site || '')}</span>
        <span class="dash-site-total">${_dashNum(site.total)} itens</span>
      </div>
      <div class="dash-site-stats">
        <span><i class="dash-dot online"></i>${_dashNum(site.online)} online</span>
        <span><i class="dash-dot offline"></i>${_dashNum(site.offline)} offline</span>
        <span><i class="dash-dot warning"></i>${_dashNum(site.missing_snapshot)} sem foto</span>
      </div>
    </div>
  `).join('');
  el.querySelectorAll('[data-dash-site]').forEach(card => {
    card.addEventListener('click', () => {
      _openDashDrawer('Site', card.dataset.dashSite || 'Site');
      document.getElementById('dashDrawerFilters').innerHTML = '';
      _drawerRenderRows(`<div class="drawer-item"><i data-lucide="map-pin" style="width:14px;height:14px;color:var(--primary)"></i><div class="drawer-item-main"><div class="drawer-item-title">${esc(card.dataset.dashSite || '')}</div><div class="drawer-item-sub">Use o filtro de site nos inventarios para ver os itens.</div></div></div>`);
    });
  });
}

// 
async function loadDashboard() {
  _dashDrawerData = null; // limpa cache ao recarregar
  const data = await apiJson('/api/dashboard/summary');
  if (!data) return;

  const inv = data.inventory || {};
  const ip  = inv.ip  || {};
  const dvr = inv.dvr || {};
  const nvr = inv.nvr || {};
  const win = inv.windows || {};
  const tot = data.totals || {};
  const monitoring = data.monitoring?.types || {};

  // KPIs
  const ipOnline = ip.online ?? '';
  const ipTotal  = ip.total  ?? '';
  setText('mCamsOnline', ipOnline);
  setText('mCamsTotal',  ipTotal ? `de ${ipTotal} total` : '');

  const dvrRec = dvr.recorders ?? 0;
  const nvrRec = nvr.recorders ?? 0;
  setText('mDvrNvr',      dvrRec + nvrRec);
  const dvrCh  = dvr.total ?? 0;
  const nvrCh  = nvr.total ?? 0;
  setText('mDvrNvrCanais', `${dvrRec} DVR · ${nvrRec} NVR · ${dvrCh + nvrCh} canais`);

  setText('mSites',     tot.sites     ?? '');

  const onuStatus = monitoring.onu || {};
  const onuUp = _dashNum(onuStatus.up ?? onuStatus.online);
  const onuTotal = _dashNum(onuStatus.total);
  setText('mOnusOnline', onuUp);
  setText('mOnusTotal', onuTotal ? `de ${onuTotal} monitoradas` : 'nenhuma monitorada');

  const setMonitoringKpi = (key, onlineId, totalId) => {
    const item = monitoring[key] || {};
    const up = _dashNum(item.up ?? item.online);
    const total = _dashNum(item.total);
    setText(onlineId, up);
    setText(totalId, total ? `de ${total} monitorados` : 'nenhum monitorado');
  };
  const oltKpi = monitoring.olt || {};
  const oltTotal = _dashNum(oltKpi.total);
  const oltUp = _dashNum(oltKpi.up ?? oltKpi.online);
  const oltAttention = _dashNum(oltKpi.down) + _dashNum(oltKpi.unstable) + _dashNum(oltKpi.unknown);
  setText('mOltsOnline', oltTotal);
  setText('mOltsTotal', oltTotal ? `${oltUp} online · ${oltAttention} em atencao` : 'nenhuma cadastrada');
  setMonitoringKpi('connector', 'mConnectorsOnline', 'mConnectorsTotal');
  setMonitoringKpi('windows', 'mComputersOnline', 'mComputersTotal');
  setMonitoringKpi('access_device', 'mAccessDevicesOnline', 'mAccessDevicesTotal');
  setMonitoringKpi('whatsapp', 'mWhatsappOnline', 'mWhatsappTotal');

  const attentionTotal = _dashAttentionTotal(monitoring);
  setText('mAttention', attentionTotal);
  setText('mAttentionSub', attentionTotal === 1 ? 'equipamento exige verificação' : 'equipamentos exigem verificação');
  setText('dashLastUpdated', `Atualizado às ${new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })}`);

  // Alertas  clicaveis
  const alerts = data.alerts || [];
  const alertsEl = document.getElementById('dashAlerts');
  const alertsList = document.getElementById('dashAlertsList');
  const alertActionMap = {
    'Cameras IP sem snapshot':         'no_snapshot',
    'Itens offline ou com erro':       'ip_offline',
    'Itens sem local':                  null,
    'Possiveis duplicidades IP/MAC':    null,
    'Computadores Windows sem SSD detectado': 'win_offline',
  };
  if (alerts.length) {
    const colorMap = { danger: '#fa5252', warning: '#fd7e14', info: '#339af0' };
    const iconMap  = { danger: 'alert-circle', warning: 'alert-triangle', info: 'info' };
    alertsList.innerHTML = alerts.map(a => {
      const color      = colorMap[a.level] || '#888';
      const icon       = iconMap[a.level]  || 'info';
      const actionKey  = alertActionMap[a.label];
      const clickable  = actionKey ? `data-dash-alert="${actionKey}" style="cursor:pointer"` : '';
      const hint       = actionKey ? `<i data-lucide="chevron-right" style="width:13px;height:13px;color:${color};flex-shrink:0"></i>` : '';
      return `<div ${clickable} class="dash-alert-item ${esc(a.level || 'info')}" style="--alert-color:${color}">
        <span class="dash-alert-icon"><i data-lucide="${icon}"></i></span>
        <span class="dash-alert-label">${esc(a.label)}</span>
        <strong>${a.count}</strong>
        ${hint}
      </div>`;
    }).join('');
    alertsEl.style.display = '';
    lucide.createIcons();
  } else {
    alertsEl.style.display = 'none';
  }

  renderDashboardPriorities(data);
  renderDashboardHealth(data);

  // Status por tipo  clicaveis
  const statusGrid = document.getElementById('dashStatusGrid');
  const statusTypes = [
    { label: 'Conectores', icon: 'plug',        s: monitoring.connector || {}, type: 'monitoring' },
    { label: 'OLTs',       icon: 'radio-tower', s: monitoring.olt || {},       type: 'monitoring' },
    { label: 'ONUs/ONTs',  icon: 'wifi',        s: monitoring.onu || {},       type: 'monitoring' },
    { label: 'Controladoras', icon: 'shield-check', s: monitoring.access_device || {}, type: 'monitoring' },
    { label: 'WhatsApp',      icon: 'message-circle', s: monitoring.whatsapp || {},    type: 'monitoring' },
    { label: 'Cameras IP',  icon: 'camera',     s: ip,  type: 'ip'      },
    { label: 'DVR canais',  icon: 'hard-drive',  s: dvr, type: 'dvr'     },
    { label: 'NVR canais',  icon: 'hard-drive',  s: nvr, type: 'nvr'     },
    { label: 'Windows',     icon: 'monitor',     s: win, type: 'windows' },
  ];
  statusGrid.innerHTML = statusTypes.map(t => {
    const total   = t.s.total   ?? 0;
    const online  = t.s.online  ?? t.s.up ?? 0;
    const offline = t.s.offline ?? t.s.down ?? 0;
    const pct     = total > 0 ? Math.round((online / total) * 100) : null;
    const barColor = pct === null ? 'var(--muted)' : pct >= 80 ? 'var(--primary)' : pct >= 50 ? '#fd7e14' : '#fa5252';
    return `<div class="dash-status-row clickable" data-type="${t.type}" title="Ver detalhes">
      <i data-lucide="${t.icon}" style="width:14px;height:14px;color:var(--muted);flex-shrink:0"></i>
      <span style="flex:1;font-size:13px">${t.label}</span>
      <span style="font-size:12px;color:var(--primary);font-weight:600">${online} online</span>
      <span style="font-size:12px;color:var(--muted);margin-left:4px">/ ${total}</span>
      ${offline ? `<span style="font-size:11px;color:#fa5252;margin-left:6px;font-weight:600">${offline} off</span>` : ''}
      <div style="width:60px;height:4px;background:var(--border);border-radius:2px;margin-left:8px;overflow:hidden">
        <div style="height:100%;width:${pct ?? 0}%;background:${barColor};transition:width .4s"></div>
      </div>
      <i data-lucide="chevron-right" style="width:13px;height:13px;color:var(--muted);margin-left:4px;flex-shrink:0"></i>
    </div>`;
  }).join('');
  lucide.createIcons();

  // Atividade recente
  const activity = data.recent_activity || [];
  const actEl = document.getElementById('dashActivity');
  if (activity.length) {
    actEl.innerHTML = activity.map(a => {
      let ago = '';
      try {
        const diff = Date.now() - new Date(a.updated_at).getTime();
        const mins = Math.floor(diff / 60000);
        const hrs  = Math.floor(mins / 60);
        const days = Math.floor(hrs / 24);
        ago = days  > 0 ? `${days}d atras` :
              hrs   > 0 ? `${hrs}h atras`  :
              mins  > 0 ? `${mins}min atras` : 'agora';
      } catch {}
      return `<div class="dash-status-row">
        <i data-lucide="file-text" style="width:14px;height:14px;color:var(--muted);flex-shrink:0"></i>
        <span style="flex:1;font-size:13px">${esc(a.label)}</span>
        <span style="font-size:11px;color:var(--muted)">${ago}</span>
      </div>`;
    }).join('');
  } else {
    actEl.innerHTML = '<div class="dash-status-row"><span style="color:var(--muted);font-size:13px">Nenhuma atividade registrada.</span></div>';
  }
  lucide.createIcons();

  // Sites usados pelo card e pelo filtro lateral, sem repetir um painel no rodape.
  const sites = data.sites || [];

  // Click handlers: KPI cards
  const kpiCamerasIp = document.getElementById('kpiCamerasIp');
  if (kpiCamerasIp) kpiCamerasIp.onclick = () => openDashDrawerIp('all');
  const kpiGravadores = document.getElementById('kpiGravadores');
  if (kpiGravadores) kpiGravadores.onclick = () => openDashDrawerRecorder('all', 'all');
  const kpiSites = document.getElementById('kpiSites');
  if (kpiSites) kpiSites.onclick = () => {
    _openDashDrawer('Sites', 'Sites monitorados');
    document.getElementById('dashDrawerFilters').innerHTML = '';
    _drawerRenderRows(sites.length
      ? sites.map(s => `<div class="drawer-item"><i data-lucide="map-pin" style="width:14px;height:14px;color:var(--primary)"></i><div class="drawer-item-main"><div class="drawer-item-title">${esc(s)}</div></div></div>`).join('')
      : '');
  };
  const kpiOnus = document.getElementById('kpiOnus');
  if (kpiOnus) kpiOnus.onclick = () => typeof openMonitoringDrawer === 'function' ? openMonitoringDrawer('onu', 'all') : navigateTo('monitoring');
  const kpiOlts = document.getElementById('kpiOlts');
  if (kpiOlts) kpiOlts.onclick = () => typeof openMonitoringDrawer === 'function' ? openMonitoringDrawer('olt', 'all') : navigateTo('monitoring');
  const kpiConnectors = document.getElementById('kpiConnectors');
  if (kpiConnectors) kpiConnectors.onclick = () => typeof openMonitoringDrawer === 'function' ? openMonitoringDrawer('connector', 'all') : navigateTo('monitoring');
  const kpiComputers = document.getElementById('kpiComputers');
  if (kpiComputers) kpiComputers.onclick = () => openDashDrawerWindows('all');
  const kpiAccessDevices = document.getElementById('kpiAccessDevices');
  if (kpiAccessDevices) kpiAccessDevices.onclick = () => typeof openMonitoringDrawer === 'function' ? openMonitoringDrawer('access_device', 'all') : navigateTo('monitoring');
  const kpiWhatsapp = document.getElementById('kpiWhatsapp');
  if (kpiWhatsapp) kpiWhatsapp.onclick = () => typeof openMonitoringDrawer === 'function' ? openMonitoringDrawer('whatsapp', 'all') : navigateTo('monitoring');
  const kpiAttention = document.getElementById('kpiAttention');
  if (kpiAttention) kpiAttention.onclick = () => {
    if (typeof openMonitoringAttentionDrawer === 'function') openMonitoringAttentionDrawer();
    else navigateTo('monitoring');
  };

  // Click handlers: status rows (adicionados via dataset)
  document.querySelectorAll('.dash-status-row.clickable').forEach(row => {
    row.addEventListener('click', () => {
      const type = row.dataset.type;
      if (type === 'ip')      openDashDrawerIp('all');
      if (type === 'dvr')     openDashDrawerRecorder('dvr', 'all');
      if (type === 'nvr')     openDashDrawerRecorder('nvr', 'all');
      if (type === 'windows') openDashDrawerWindows('all');
      if (type === 'monitoring') navigateTo('monitoring');
    });
  });

  // Click handlers: alertas
  document.querySelectorAll('[data-dash-alert]').forEach(el => {
    el.addEventListener('click', () => {
      const a = el.dataset.dashAlert;
      if (a === 'ip_offline')   openDashDrawerIp('offline');
      if (a === 'dvr_offline')  openDashDrawerRecorder('dvr', 'offline');
      if (a === 'nvr_offline')  openDashDrawerRecorder('nvr', 'offline');
      if (a === 'win_offline')  openDashDrawerWindows('offline');
      if (a === 'no_snapshot')  openDashDrawerIp('no_snap');
    });
  });

  lucide.createIcons();
}

