function loadPlayback() {
  if (!_playbackBound) bindPlayback();
  playbackDefaults();
}

function playbackDefaults() {
  const date = document.getElementById('playbackDate');
  const start = document.getElementById('playbackStart');
  const end = document.getElementById('playbackEnd');
  if (date && !date.value) date.value = new Date().toISOString().slice(0, 10);
  if (start && !start.value) start.value = '08:30:00';
  if (end && !end.value) end.value = '09:00:00';
}

function bindPlayback() {
  _playbackBound = true;
  document.getElementById('playbackForm')?.addEventListener('submit', playbackCreateClip);
  document.getElementById('btnPlaybackSnapshot')?.addEventListener('click', playbackCreateSnapshot);
  document.getElementById('btnPlaybackFrames')?.addEventListener('click', playbackCreateFrames);
}

function playbackDateTime(timeId) {
  const date = document.getElementById('playbackDate').value;
  const time = document.getElementById(timeId).value;
  if (!date || !time) return '';
  return `${date} ${time.length === 5 ? `${time}:00` : time}`;
}

function playbackPayload() {
  return {
    host: document.getElementById('playbackHost').value.trim(),
    user: document.getElementById('playbackUser').value.trim(),
    password: document.getElementById('playbackPassword').value,
    channel: Number(document.getElementById('playbackChannel').value),
    start: playbackDateTime('playbackStart'),
    end: playbackDateTime('playbackEnd'),
    format: document.getElementById('playbackFormat').value,
  };
}

function playbackIntervalSeconds() {
  const min = Number(document.getElementById('playbackIntervalMin')?.value || 0);
  const sec = Number(document.getElementById('playbackIntervalSec')?.value || 0);
  return Math.max(1, (min * 60) + sec);
}

function setPlaybackStatus(text, isError = false) {
  const el = document.getElementById('playbackStatus');
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? 'var(--danger)' : 'var(--muted)';
}

function playbackFileUrl(path) {
  const sep = path.includes('?') ? '&' : '?';
  const token = _token ? `${sep}token=${encodeURIComponent(_token)}` : '';
  return `${API_BASE}${path}${token}`;
}

function renderPlaybackLink(data) {
  const box = document.getElementById('playbackDownloads');
  if (!box || !data?.url) return;
  const url = playbackFileUrl(data.url);
  const warning = data.warning ? `<p class="playback-warning">${esc(data.warning)}</p>` : '';
  const source = data.source_url ? `<a class="secondary-action" href="${playbackFileUrl(data.source_url)}" target="_blank" rel="noopener"><i data-lucide="file-video"></i> DAV</a>` : '';
  box.innerHTML = `
    ${warning}
    <div class="playback-download-row">
      <a class="primary-action" href="${url}" target="_blank" rel="noopener"><i data-lucide="download"></i> ${esc(data.filename || 'arquivo')}</a>
      ${source}
    </div>`;
  lucide.createIcons();
}

function clearPlaybackFrames() {
  const grid = document.getElementById('playbackFramesGrid');
  if (grid) grid.innerHTML = '';
}

function renderPlaybackFrames(data) {
  const grid = document.getElementById('playbackFramesGrid');
  if (!grid) return;
  const frames = data?.frames || [];
  grid.innerHTML = frames.map(frame => {
    const url = playbackFileUrl(frame.url);
    return `
      <a class="playback-frame-card" href="${url}" target="_blank" rel="noopener">
        <img src="${url}" alt="${esc(frame.timestamp)}">
        <span>${esc(frame.timestamp?.slice(11) || '')}</span>
      </a>`;
  }).join('');
}

async function playbackCreateClip(e) {
  e.preventDefault();
  const btn = document.getElementById('btnPlaybackClip');
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i data-lucide="loader-2"></i> Gerando';
  lucide.createIcons();
  setPlaybackStatus('Consultando DVR...');
  try {
    const res = await api('/api/playback/clip', { method: 'POST', body: JSON.stringify(playbackPayload()) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok) throw new Error(data?.detail || 'Falha ao gerar trecho.');
    clearPlaybackFrames();
    renderPlaybackLink(data);
    setPlaybackStatus(data.warning ? 'Trecho salvo em DAV.' : 'Trecho pronto.');
    showToast('Trecho gerado.');
  } catch (err) {
    setPlaybackStatus(err.message, true);
    showToast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
    lucide.createIcons();
  }
}

async function playbackCreateSnapshot() {
  const btn = document.getElementById('btnPlaybackSnapshot');
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i data-lucide="loader-2"></i> Gerando';
  lucide.createIcons();
  setPlaybackStatus('Extraindo frame...');
  try {
    const base = playbackPayload();
    const res = await api('/api/playback/snapshot', {
      method: 'POST',
      body: JSON.stringify({ ...base, timestamp: base.start }),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok) throw new Error(data?.detail || 'Falha ao gerar frame.');
    const img = document.getElementById('playbackPreview');
    const frameUrl = playbackFileUrl(data.url);
    img.src = `${frameUrl}${frameUrl.includes('?') ? '&' : '?'}t=${Date.now()}`;
    img.classList.remove('hidden');
    clearPlaybackFrames();
    renderPlaybackLink(data);
    setPlaybackStatus('Frame pronto.');
    showToast('Frame gerado.');
  } catch (err) {
    setPlaybackStatus(err.message, true);
    showToast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
    lucide.createIcons();
  }
}

async function playbackCreateFrames() {
  const btn = document.getElementById('btnPlaybackFrames');
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i data-lucide="loader-2"></i> Gerando';
  lucide.createIcons();
  setPlaybackStatus('Extraindo sequencia...');
  try {
    const payload = { ...playbackPayload(), interval_seconds: playbackIntervalSeconds(), timeout_sec: 900 };
    const res = await api('/api/playback/frames', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok) throw new Error(data?.detail || 'Falha ao gerar frames.');
    const img = document.getElementById('playbackPreview');
    img.classList.add('hidden');
    renderPlaybackFrames(data);
    renderPlaybackLink({ url: data.source_url, filename: 'DAV original' });
    setPlaybackStatus(`${data.count} frames gerados a cada ${data.interval_seconds}s.`);
    showToast(`${data.count} frames gerados.`);
  } catch (err) {
    setPlaybackStatus(err.message, true);
    showToast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
    lucide.createIcons();
  }
}

//  IA NVR
let _iaNvrBound = false;
let _iaNvrTargets = [];

let _iaNvrCamTitleByIp = {};
async function loadIaNvr() {
  if (!_iaNvrBound) bindIaNvr();
  iaNvrDefaults();
  const [data] = await Promise.all([
    apiJson('/api/ia/nvr/targets'),
    fetchCameraTitleByIp().then(map => { _iaNvrCamTitleByIp = map; }),
  ]);
  _iaNvrTargets = data?.targets || [];
  populateIaNvrSiteFilter();
  populateIaNvrDvrSelect();
  populateIaNvrCameraSelect();
}

function iaNvrDefaults() {
  const date = document.getElementById('iaNvrDate');
  const start = document.getElementById('iaNvrStart');
  const end = document.getElementById('iaNvrEnd');
  if (date && !date.value) date.value = new Date().toISOString().slice(0, 10);
  if (start && !start.value) start.value = '08:30:00';
  if (end && !end.value) end.value = '09:00:00';
}

function bindIaNvr() {
  _iaNvrBound = true;
  document.getElementById('iaNvrSite')?.addEventListener('change', () => {
    populateIaNvrDvrSelect();
    populateIaNvrCameraSelect();
  });
  document.getElementById('iaNvrDvr')?.addEventListener('change', populateIaNvrCameraSelect);
  document.getElementById('iaNvrForm')?.addEventListener('submit', iaNvrRunSearch);
}

function populateIaNvrSiteFilter() {
  const sel = document.getElementById('iaNvrSite');
  if (!sel) return;
  const sites = [...new Set(_iaNvrTargets.map(t => t.site).filter(Boolean))].sort();
  const cur = sel.value;
  sel.innerHTML = '<option value="">Todos os sites</option>' +
    sites.map(s => `<option${s === cur ? ' selected' : ''}>${esc(s)}</option>`).join('');
}

// Mesma ideia da tela Gravadores: nome do DVR = recorder_name se alguem ja
// batizou, senao "DVR-NN" sequencial dentro do site -- calculado aqui de
// novo porque esta tela carrega seu proprio _iaNvrTargets, independente do
// estado da tela Gravadores.
function computeIaNvrDvrNames() {
  const siteByHost = {};
  const nameByHost = {};
  _iaNvrTargets.forEach(t => {
    const host = t.host || '';
    if (!host) return;
    if (!siteByHost[host]) siteByHost[host] = t.site || t.local || '';
    if (!nameByHost[host] && t.recorder_name) nameByHost[host] = t.recorder_name;
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
}

function iaNvrCameraLabel(t) {
  const titulo = t.title || '';
  if (titulo && !camHasDefaultTitle({ titulo })) return titulo;
  const doCadastro = _iaNvrCamTitleByIp[t.camera_ip];
  if (doCadastro) return doCadastro;
  return `Camera ${String(t.channel || 0).padStart(2, '0')}`;
}

function populateIaNvrDvrSelect() {
  const site = document.getElementById('iaNvrSite')?.value || '';
  const sel = document.getElementById('iaNvrDvr');
  if (!sel) return;
  const names = computeIaNvrDvrNames();
  const scoped = site ? _iaNvrTargets.filter(t => t.site === site) : _iaNvrTargets;
  const hosts = [...new Set(scoped.map(t => t.host).filter(Boolean))]
    .sort((a, b) => (names[a] || a).localeCompare(names[b] || b, 'pt', { numeric: true }));
  const cur = sel.value;
  sel.innerHTML = '<option value="">Todos os gravadores</option>' +
    hosts.map(h => {
      const total = scoped.filter(t => t.host === h).length;
      const label = `${names[h] || h} (${total} camera${total !== 1 ? 's' : ''})`;
      return `<option value="${esc(h)}"${h === cur ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('');
  if (!hosts.includes(sel.value)) sel.value = '';
}

function populateIaNvrCameraSelect() {
  const site = document.getElementById('iaNvrSite')?.value || '';
  const dvr = document.getElementById('iaNvrDvr')?.value || '';
  const sel = document.getElementById('iaNvrCamera');
  if (!sel) return;
  let filtered = site ? _iaNvrTargets.filter(t => t.site === site) : _iaNvrTargets;
  if (dvr) filtered = filtered.filter(t => t.host === dvr);
  filtered = [...filtered].sort((a, b) => (a.channel || 0) - (b.channel || 0));
  const cur = sel.value;
  sel.innerHTML = '<option value="">Selecione a camera</option>' +
    filtered.map(t => {
      const value = `${t.host}|${t.http_port}|${t.channel}`;
      const label = `CH${String(t.channel || 0).padStart(2, '0')} - ${iaNvrCameraLabel(t)}`;
      return `<option value="${esc(value)}"${value === cur ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('');
}

function iaNvrDateTime(timeId) {
  const date = document.getElementById('iaNvrDate').value;
  const time = document.getElementById(timeId).value;
  if (!date || !time) return '';
  return `${date} ${time.length === 5 ? `${time}:00` : time}`;
}

function iaNvrResultCard(hit, index) {
  const clip = hit.clip_url ? `<a class="secondary-action" href="${playbackFileUrl(hit.clip_url)}" target="_blank" rel="noopener"><i data-lucide="film"></i> Baixar trecho</a>` : '';
  const pct = Math.round((hit.confidence || 0) * 100);
  return `
    <div class="ia-nvr-hit" style="padding:14px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-weight:700;font-size:12px;color:var(--muted)">#${index + 1}</span>
        <strong>${esc(hit.title || hit.host)}</strong>
        <span style="font-size:11px;color:var(--muted)">${esc(hit.site || '')}</span>
        <span style="margin-left:auto;font-size:11px;font-weight:700;color:var(--primary)">${esc(hit.timestamp)}</span>
      </div>
      <p style="margin:6px 0 0;font-size:12px;color:var(--text)">${esc(hit.description || '')}</p>
      <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
        <span style="font-size:11px;color:var(--muted)">Confianca: ${pct}%</span>
        ${clip}
      </div>
    </div>`;
}

function iaNvrMissRow(miss) {
  return `<div style="padding:6px 0;font-size:12px;color:var(--muted)">
    <i data-lucide="x-circle" style="width:12px;height:12px;vertical-align:-2px"></i>
    ${esc(miss.title || miss.host)}: ${esc(miss.reason)}
  </div>`;
}

function renderIaNvrResult(result) {
  const el = document.getElementById('iaNvrResults');
  if (!el) return;
  const hits = result?.hits || [];
  const misses = result?.misses || [];
  if (!hits.length) {
    el.innerHTML = `<p style="color:var(--muted);font-size:13px;padding:16px 0">Nada encontrado nessa janela.</p>` +
      (misses.length ? `<div>${misses.map(iaNvrMissRow).join('')}</div>` : '');
    lucide.createIcons();
    return;
  }
  const truncatedNote = result.truncated
    ? `<p style="color:var(--amber);font-size:12px;margin-top:10px">Busca interrompida pelo limite de tempo -- resultado parcial.</p>` : '';
  el.innerHTML = hits.map(iaNvrResultCard).join('') +
    (misses.length ? `<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12px;color:var(--muted)">Cameras verificadas sem resultado (${misses.length})</summary>${misses.map(iaNvrMissRow).join('')}</details>` : '') +
    truncatedNote;
  lucide.createIcons();
}

async function iaNvrPollJob(jobId) {
  const statusEl = document.getElementById('iaNvrStatus');
  for (;;) {
    const data = await apiJson(`/api/ia/nvr/search/jobs/${jobId}`);
    if (!data) { if (statusEl) statusEl.textContent = 'Falha ao consultar a busca.'; return; }
    if (data.status === 'done') {
      if (statusEl) statusEl.textContent = `Busca concluida (${data.elapsed_sec}s).`;
      renderIaNvrResult(data.result);
      return;
    }
    if (data.status === 'error') {
      if (statusEl) statusEl.textContent = data.error || 'Falha na busca.';
      showToast(data.error || 'Falha na busca.', true);
      return;
    }
    if (statusEl) statusEl.textContent = `Buscando... (${data.elapsed_sec}s)`;
    await new Promise(r => setTimeout(r, 3000));
  }
}

async function iaNvrRunSearch(e) {
  e.preventDefault();
  const camValue = document.getElementById('iaNvrCamera')?.value || '';
  const [host, httpPort, channel] = camValue.split('|');
  if (!host || !channel) { showToast('Selecione uma camera.', true); return; }
  const query = document.getElementById('iaNvrQuery').value.trim();
  if (!query) return;

  const btn = document.getElementById('btnIaSearch');
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i data-lucide="loader-2"></i> Buscando';
  lucide.createIcons();
  document.getElementById('iaNvrResults').innerHTML = '';
  const statusEl = document.getElementById('iaNvrStatus');
  if (statusEl) statusEl.textContent = 'Enviando busca...';

  try {
    const payload = {
      host,
      http_port: Number(httpPort) || 80,
      channel: Number(channel),
      user: document.getElementById('iaNvrUser').value.trim(),
      password: document.getElementById('iaNvrPassword').value,
      site: document.getElementById('iaNvrSite')?.value || '',
      start_time: iaNvrDateTime('iaNvrStart'),
      end_time: iaNvrDateTime('iaNvrEnd'),
      query,
      max_hops: Number(document.getElementById('iaNvrMaxHops').value) || 0,
      window_min: Number(document.getElementById('iaNvrWindowMin').value) || 5,
    };
    const res = await api('/api/ia/nvr/search/jobs', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || !data?.job_id) throw new Error(data?.detail || 'Falha ao iniciar busca.');
    await iaNvrPollJob(data.job_id);
  } catch (err) {
    if (statusEl) statusEl.textContent = err.message;
    showToast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
    lucide.createIcons();
  }
}

//  OLT 
let _oltRows   = [];
let _oltCamMap = {}; // serial|mac  camera
let _oltOnuMonitoringRows = [];
let _oltWs     = null;

async function loadOlt() {
  const [oltData, camData, onuMonitoringData] = await Promise.all([
    apiJson('/api/olt/rows'),
    apiJson('/api/cameras'),
    apiJson('/api/monitoring/entities?entity_type=onu&limit=2000').catch(() => ({ entities: [] })),
  ]);

  _oltRows = oltData?.rows || (Array.isArray(oltData) ? oltData : []);
  _oltOnuMonitoringRows = Array.isArray(onuMonitoringData?.entities) ? onuMonitoringData.entities : [];

  // Monta indice de cameras por serial e por mac
  const cams = camData?.cameras || (Array.isArray(camData) ? camData : []);
  _oltCamMap = {};
  cams.forEach(c => {
    if (c.onu_serial) _oltCamMap[c.onu_serial.toUpperCase()] = c;
    if (c.mac)        _oltCamMap[c.mac.toLowerCase()] = c;
  });

  renderOltTable(sortOltRows(_oltRows));
  populateOltMacSiteFilter();
}

function populateOltMacSiteFilter() {
  const sites = [...new Set(_oltRows.map(r => r.site).filter(Boolean))].sort();
  const selSite = document.getElementById('oltFilterSite');
  if (selSite) {
    const cur = selSite.value;
    selSite.innerHTML = '<option value="">Todos os sites</option>' +
      sites.map(s => `<option${s === cur ? ' selected' : ''}>${esc(s)}</option>`).join('');
  }
}

function renderOltTable(rows) {
  const tbody = document.getElementById('oltTableBody');
  if (!tbody) return;
  const siteFilter = String(document.getElementById('oltFilterSite')?.value || '').trim();
  const query = String(document.getElementById('oltSearch')?.value || '').trim();
  const onuKeys = new Set();
  const deviceKeys = new Set();
  rows.forEach(r => {
    const oltKey = String(r.olt_ip || r.olt_name || '').trim().toLowerCase();
    const pon = String(r.pon ?? '').trim();
    const onuId = String(r.onu_id ?? '').trim();
    const serial = String(r.onu_serial || '').trim().toUpperCase();
    const onuKey = oltKey && pon && onuId ? `${oltKey}|${pon}|${onuId}` : serial ? `${oltKey}|serial|${serial}` : '';
    if (onuKey) onuKeys.add(onuKey);
    const mac = String(r.cpe_mac || '').trim().toLowerCase();
    if (mac) deviceKeys.add(mac);
  });
  const monitoredOnus = _oltOnuMonitoringRows.filter(row => !siteFilter || String(row.site || '').trim() === siteFilter);
  // Duas fontes diferentes: com busca ativa, conta so o que apareceu na
  // ultima coleta (onuKeys); sem busca, prioriza a contagem de monitoramento
  // (fonte separada, atualizada por outro fluxo) -- podem nao bater se
  // estiverem dessincronizadas. O title explica isso ao passar o mouse.
  const usingMonitoringCount = !query && monitoredOnus.length > 0;
  const onuTotal = query ? onuKeys.size : (monitoredOnus.length || onuKeys.size);
  const deviceTotal = deviceKeys.size;
  const sites = new Set(rows.map(r => String(r.site || '').trim()).filter(Boolean));
  const olts = new Set(rows.map(r => String(r.olt_ip || r.olt_name || '').trim()).filter(Boolean));
  setText('oltOnuTotal', onuTotal);
  const onuTotalEl = document.getElementById('oltOnuTotal');
  if (onuTotalEl) {
    onuTotalEl.title = usingMonitoringCount
      ? 'Total de ONUs monitoradas (pode diferir da ultima coleta se estiver dessincronizado)'
      : 'Total de ONUs na ultima coleta desta tela';
  }
  setText('oltDeviceTotal', deviceTotal);
  setText('oltSiteCount', sites.size);
  setText('oltCount', olts.size);
  setText('oltTableFooter', `${onuTotal} ONU${onuTotal !== 1 ? 's' : ''} · ${deviceTotal} dispositivo${deviceTotal !== 1 ? 's' : ''}`);
  if (!rows.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">Nenhum dado. Execute a coleta.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td style="text-align:center">${esc(String(r.pon ?? ''))}</td>
      <td style="text-align:center">${esc(String(r.onu_id ?? ''))}</td>
      <td class="text-muted">${esc(r.onu_name || '')}</td>
      <td class="monospace">${esc(r.onu_serial || '')}</td>
      <td class="monospace">${r.cpe_mac
        ? esc(r.cpe_mac)
        : '<span class="text-muted">Nenhum MAC aprendido</span>'}</td>
      <td class="text-muted" style="text-align:center">${r.vlan
        ? esc(r.vlan)
        : (r.vlan_mode === 'untagged' ? 'Sem tag' : '—')}</td>
      <td class="monospace text-muted">${esc(r.olt_ip || '')}</td>
      <td class="text-muted">${esc(r.olt_name || '')}</td>
      <td class="text-muted">${esc(r.site || '')}</td>
    </tr>`).join('');
}

function sortOltRows(rows) {
  return [...rows].sort((a, b) => {
    const ponDiff = (Number(a.pon) || 0) - (Number(b.pon) || 0);
    if (ponDiff !== 0) return ponDiff;
    return (Number(a.onu_id) || 0) - (Number(b.onu_id) || 0);
  });
}

function filterOltTable() {
  const site = document.getElementById('oltFilterSite')?.value || '';
  const q    = (document.getElementById('oltSearch')?.value   || '').toLowerCase();
  const filtered = _oltRows.filter(r => {
    if (site && String(r.site ?? '').trim() !== String(site).trim()) return false;
    // String() em todos: a API manda vlan como numero e (f || '').toLowerCase()
    // estourava TypeError, matando o filtro no meio da lista
    if (q) return [r.site, r.olt_ip, r.olt_name, r.onu, r.onu_name, r.onu_serial,
                   r.cpe_mac, r.mac, r.vlan, r.pon, r.onu_id, r.port, r.ip]
      .some(f => String(f ?? '').toLowerCase().includes(q));
    return true;
  });
  renderOltTable(sortOltRows(filtered));
}

function oltConsoleLog(text, cls = '') {
  const el = document.getElementById('oltConsole');
  if (!el) return;
  const line = document.createElement('div');
  if (cls) line.className = `ping-term-line-${cls === 'error' ? 'err' : cls === 'ok' ? 'ok' : 'info'}`;
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

async function refreshOltConnectors() {
  const sel = document.getElementById('oltConnector');
  if (!sel) return;
  try {
    const data = await apiJson('/api/connectors');
    _connectors = Array.isArray(data?.connectors) ? data.connectors : [];
  } catch {
    _connectors = _connectors || [];
  }
  const rows = _routerConnectors();
  sel.innerHTML = '<option value="">Opcional: usar servidor local/VPN</option>' + rows.map(c => {
    const online = _connectorIsOnline(c);
    const tunnel = _connectorHasTunnel(c) ? ' + VPN' : '';
    return `<option value="${esc(c.id || '')}" ${online ? '' : 'disabled'}>${esc(_connectorLabel(c))}${tunnel}${online ? '' : ' (offline)'}</option>`;
  }).join('');

  const site = (document.getElementById('oltSite')?.value || document.getElementById('oltFilterSite')?.value || '').trim();
  const match = _findConnectorForSite(site);
  if (match?.id) sel.value = match.id;
}
