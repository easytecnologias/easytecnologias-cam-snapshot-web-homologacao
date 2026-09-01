function deploySetResult(html, isError = false) {
  const box = document.getElementById('deployLookupResult');
  if (!box) return;
  box.innerHTML = html || 'Aguardando consulta no conector.';
  box.classList.toggle('error', !!isError);
}

function deploySetRecorderLoginResult(html, isError = false) {
  const box = document.getElementById('deployRecorderLoginResult');
  if (!box) return;
  box.innerHTML = html || 'Informe host, usuario e senha do gravador e clique em Entrar.';
  box.classList.toggle('error', !!isError);
}

function deployRenderRecorderChannels(channels = []) {
  const input = document.getElementById('deployRecorderChannel');
  const grid = document.getElementById('deployRecorderChannelGrid');
  const toggle = document.getElementById('deployRecorderChannelButton');
  const labelEl = document.getElementById('deployRecorderChannelLabel');
  const dropdown = document.getElementById('deployRecorderChannelDropdown');
  if (!input || !grid || !toggle || !labelEl || !dropdown) return;
  const previous = input.value;
  if (!channels.length) {
    input.value = '';
    labelEl.textContent = 'Entre no gravador';
    toggle.disabled = true;
    dropdown.classList.add('disabled');
    grid.innerHTML = '<span class="deploy-channel-empty">Entre no gravador para carregar os canais.</span>';
    grid.classList.add('hidden');
    return;
  }
  toggle.disabled = false;
  dropdown.classList.remove('disabled');
  grid.innerHTML = channels.map(item => {
    const ch = Number(item.channel || 0);
    const used = !!item.used;
    const detail = [item.title, item.camera_ip].filter(Boolean).join(' - ');
    const selected = !used && String(ch) === String(previous);
    const label = String(ch).padStart(2, '0');
    const title = used ? `Canal ${label} em uso${detail ? `: ${detail}` : ''}` : `Canal ${label} livre`;
    return `<button type="button" class="deploy-channel-pill ${used ? 'used' : 'free'} ${selected ? 'selected' : ''}" data-channel="${esc(ch)}" ${used ? 'disabled' : ''} title="${esc(title)}">${esc(label)}</button>`;
  }).join('');
  const free = channels.filter(item => !item.used);
  const canKeep = free.some(item => String(item.channel) === String(previous));
  if (canKeep) {
    input.value = previous;
  } else {
    input.value = free[0]?.channel ? String(free[0].channel) : '';
  }
  labelEl.textContent = input.value ? `Canal ${String(input.value).padStart(2, '0')}` : 'Sem canal livre';
  grid.querySelectorAll('.deploy-channel-pill').forEach(btn => {
    btn.classList.toggle('selected', btn.dataset.channel === input.value);
  });
}

function deployToggleRecorderChannelDropdown() {
  const toggle = document.getElementById('deployRecorderChannelButton');
  const grid = document.getElementById('deployRecorderChannelGrid');
  if (!toggle || !grid || toggle.disabled) return;
  grid.classList.toggle('hidden');
}

function deployResetRecorderLogin() {
  deployRenderRecorderChannels();
  deploySetRecorderLoginResult();
}

function deployRecorderRowConnectorId(row) {
  return String(row?.remote_connector_id || row?.connector_id || '').trim();
}

function deployRecorderRowSite(row) {
  return String(row?.site || row?.local || row?.site_name || '').trim();
}

async function deployLoadAvailableRecorders() {
  const select = document.getElementById('deployRecorderHost');
  if (!select) return;
  const type = document.getElementById('deployRecorderType')?.value || '';
  const connectorId = deploySelectedConnectorId();
  const site = document.getElementById('deploySite')?.value.trim() || '';
  const previous = select.value;
  if (!type || (!connectorId && !deployIsLocalOrigin())) {
    _deployAvailableRecorders = [];
    select.innerHTML = '<option value="">Escolha primeiro o tipo e a origem</option>';
    deployResetRecorderLogin();
    return;
  }
  const data = await apiJson(`/api/${encodeURIComponent(type)}/inventory?site=`).catch(() => null);
  const rows = Array.isArray(data?.inventory) ? data.inventory : [];
  const unique = new Map();
  rows.forEach(row => {
    const host = String(row.host || row.ip || '').trim();
    if (!host) return;
    const rowConnector = deployRecorderRowConnectorId(row);
    const rowSite = deployRecorderRowSite(row);
    const sameConnector = connectorId && rowConnector === connectorId;
    const legacySameSite = connectorId && !rowConnector && site && rowSite.toLowerCase() === site.toLowerCase();
    const localAllowed = deployIsLocalOrigin() && (!site || rowSite.toLowerCase() === site.toLowerCase());
    if (!sameConnector && !legacySameSite && !localAllowed) return;
    unique.set(host, { ...(unique.get(host) || {}), ...row, host });
  });
  _deployAvailableRecorders = [...unique.values()].sort((a, b) => a.host.localeCompare(b.host, undefined, { numeric: true }));
  select.innerHTML = '<option value="">Escolha um gravador do inventario</option>' + _deployAvailableRecorders.map(row => {
    const label = [row.name || row.recorder_name, row.host, deployRecorderRowSite(row)].filter(Boolean).join(' - ');
    return `<option value="${esc(row.host)}">${esc(label)}</option>`;
  }).join('');
  if (_deployAvailableRecorders.some(row => row.host === previous)) select.value = previous;
  if (!_deployAvailableRecorders.length) select.innerHTML = '<option value="">Nenhum gravador acessivel neste conector/site</option>';
  deployResetRecorderLogin();
}

function deployApplySelectedRecorder() {
  const host = document.getElementById('deployRecorderHost')?.value || '';
  const row = _deployAvailableRecorders.find(item => item.host === host);
  const user = document.getElementById('deployRecorderUser');
  if (user) user.value = String(row?.recorder_user || row?.user || 'admin');
  deployResetRecorderLogin();
  deployRenderSummary();
  deployUpdateStepLocks({ autoAdvance: true });
}

function deployScheduleAvailableRecorders() {
  clearTimeout(_deployRecorderListTimer);
  _deployRecorderListTimer = setTimeout(deployLoadAvailableRecorders, 250);
}

function deployStepState() {
  const p = deployPayload();
  const step1Done = Boolean(deployOriginReady() && p.site);
  const step2Done = step1Done && Boolean(p.camera_ip && p.camera_title && p.camera_user && p.camera_password);
  const step3Done = step2Done && Boolean(p.recorder_type && p.recorder_host && p.recorder_channel);
  return {
    step1Done,
    step2Unlocked: step1Done,
    step2Done,
    step3Unlocked: step2Done,
    step3Done,
  };
}

function deploySetStepState(id, { locked = false, complete = false, ready = false } = {}) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('onu-step-locked', locked);
  el.classList.toggle('onu-step-complete', complete);
  el.classList.toggle('onu-step-ready', ready && !locked && !complete);
  const summary = el.querySelector(':scope > summary');
  if (summary) {
    summary.setAttribute('aria-disabled', locked ? 'true' : 'false');
    summary.tabIndex = locked ? -1 : 0;
  }
  if (locked) el.open = false;
}

function deployOpenStep(id) {
  const el = document.getElementById(id);
  if (!el || el.classList.contains('onu-step-locked')) return;
  document.querySelectorAll('#deployForm .onu-step').forEach(step => {
    step.open = step === el;
  });
}

function deployUpdateStepLocks({ autoAdvance = false } = {}) {
  const state = deployStepState();
  const wasStep2Locked = document.getElementById('cftvStep2')?.classList.contains('onu-step-locked');
  const wasStep3Locked = document.getElementById('cftvStep3')?.classList.contains('onu-step-locked');

  deploySetStepState('cftvStep1', {
    locked: false,
    complete: state.step1Done,
    ready: !state.step1Done,
  });
  deploySetStepState('cftvStep2', {
    locked: !state.step2Unlocked,
    complete: state.step2Done,
    ready: state.step2Unlocked && !state.step2Done,
  });
  deploySetStepState('cftvStep3', {
    locked: !state.step3Unlocked,
    complete: state.step3Done,
    ready: state.step3Unlocked && !state.step3Done,
  });

  if (!state.step2Unlocked) {
    deployOpenStep('cftvStep1');
  } else if (autoAdvance && wasStep2Locked) {
    deployOpenStep('cftvStep2');
  } else if (autoAdvance && state.step3Unlocked && wasStep3Locked) {
    deployOpenStep('cftvStep3');
  }

  const commitBtn = document.getElementById('btnDeployCommitCamera');
  if (commitBtn) commitBtn.disabled = !state.step2Done;
  const recorderAddBtn = document.getElementById('btnDeployRecorderAddCamera');
  if (recorderAddBtn) recorderAddBtn.disabled = !state.step3Unlocked;
}

function deployEnsureStepUnlocked(stepId, message) {
  deployUpdateStepLocks();
  const step = document.getElementById(stepId);
  if (step?.classList.contains('onu-step-locked')) {
    showToast(message || 'Conclua a etapa anterior primeiro.', true);
    return false;
  }
  return true;
}

function deployBindStepGuards() {
  ['cftvStep2', 'cftvStep3'].forEach(id => {
    const step = document.getElementById(id);
    const summary = step?.querySelector(':scope > summary');
    if (!summary || summary.dataset.deployGuardBound === '1') return;
    summary.dataset.deployGuardBound = '1';
    summary?.addEventListener('click', (ev) => {
      deployUpdateStepLocks();
      if (step.classList.contains('onu-step-locked')) {
        ev.preventDefault();
        showToast(id === 'cftvStep2' ? 'Conclua a etapa 1 para liberar a camera.' : 'Conclua a etapa 2 para liberar o gravador.', true);
      }
    });
  });
}

function deploySelectRecorderChannel(channel) {
  const input = document.getElementById('deployRecorderChannel');
  const grid = document.getElementById('deployRecorderChannelGrid');
  const labelEl = document.getElementById('deployRecorderChannelLabel');
  if (!input || !grid || !channel) return;
  const btn = grid.querySelector(`.deploy-channel-pill[data-channel="${CSS.escape(String(channel))}"]`);
  if (!btn || btn.disabled) return;
  input.value = String(channel);
  if (labelEl) labelEl.textContent = `Canal ${String(channel).padStart(2, '0')}`;
  grid.querySelectorAll('.deploy-channel-pill').forEach(item => item.classList.toggle('selected', item === btn));
  grid.classList.add('hidden');
  deployRenderSummary();
}

async function deployLoadRecorderChannels() {
  const payload = deployPayload();
  if (!payload.recorder_type || !payload.recorder_host) {
    deployRenderRecorderChannels();
    return;
  }
  try {
    const res = await api('/api/deployments/recorder-channels', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (res?.ok && data?.ok !== false) {
      deployRenderRecorderChannels(Array.isArray(data.channels) ? data.channels : []);
      deployRenderSummary();
    }
  } catch (err) {
    console.warn('Falha ao carregar canais do gravador', err);
  }
}

function deployRenderSummary() {
  deploySyncRecorderCameraIp();
  const p = deployPayload();
  const conn = deploySelectedConnector();
  const rows = [
    ['Conector', conn ? `${conn.name || conn.id} / ${conn.site || '-'}` : '-'],
    ['Camera', [p.camera_title, p.camera_ip].filter(Boolean).join(' - ') || '-'],
    ['MAC camera', p.camera_mac || '-'],
    ['Gravador', [p.recorder_type?.toUpperCase(), p.recorder_host, p.recorder_channel && `CH ${p.recorder_channel}`].filter(Boolean).join(' / ') || '-'],
    ['Site', p.site || '-'],
  ];
  const filled = [p.connector_id, p.site, p.camera_ip, p.camera_title].filter(Boolean).length;
  const summary = document.getElementById('deploySummary');
  const status = document.getElementById('deploySummaryStatus');
  if (status) status.textContent = filled >= 4 ? 'Pronto para registrar camera.' : 'Preencha conector, site, IP e titulo.';
  if (summary) {
    summary.innerHTML = rows.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  }
  deployUpdateStepLocks();
}

async function loadDeployHistory() {
  const box = document.getElementById('deployHistory');
  if (!box) return;
  const data = await apiJson('/api/deployments');
  const rows = Array.isArray(data?.deployments) ? data.deployments : [];
  if (!rows.length) {
    box.innerHTML = '<div class="deployment-history-empty">Nenhuma implantacao salva ainda.</div>';
    return;
  }
  box.innerHTML = rows.slice(0, 12).map(r => `
    <div class="deployment-history-item">
      <b>${esc(r.camera_title || r.title || 'Sem titulo')}</b>
      <span>${esc(r.camera_ip || '-')} ${r.site ? `- ${esc(r.site)}` : ''}</span>
      <small>${esc(r.status || 'rascunho')} ${r.updated_at ? `- ${esc(r.updated_at)}` : ''}</small>
    </div>
  `).join('');
}

async function loadDeploySites() {
  const list = document.getElementById('deploySiteList');
  if (!list) return;
  const modes = ['olt', 'basico', 'switch'];
  const results = await Promise.all(modes.map(mode => apiJson(`/api/cameras?mode=${encodeURIComponent(mode)}`).catch(() => null)));
  const sites = new Set();
  results.forEach(data => {
    const rows = Array.isArray(data?.cameras) ? data.cameras : (Array.isArray(data?.rows) ? data.rows : []);
    rows.forEach(row => {
      const site = String(row.site || row.site_name || row.local || '').trim();
      if (site) sites.add(site);
    });
  });
  _deploySites = [...sites].sort((a, b) => a.localeCompare(b, 'pt-BR'));
  list.innerHTML = _deploySites.map(site => `<option value="${esc(site)}"></option>`).join('');
}

let _deployOltRows = [];

function deployOltMatchesOrigin(row, originValue) {
  const rowConnector = String(row?.connector_id || '').trim();
  const origin = String(originValue || '').trim();
  if (originValue === DEPLOY_LOCAL_ORIGIN || originValue === '__local__') return !rowConnector;
  if (!origin) return false;
  if (rowConnector === origin) return true;
  const connector = typeof _connectorById === 'function' ? _connectorById(origin) : null;
  if (!connector) return false;
  const rowTerms = [row?.site, row?.name, row?.host].map(_connectorNorm).filter(Boolean);
  const connectorTerms = [connector.site, connector.client, connector.name].map(_connectorNorm).filter(Boolean);
  return rowTerms.some(rowTerm =>
    connectorTerms.some(connTerm =>
      rowTerm === connTerm
      || rowTerm.includes(connTerm)
      || connTerm.includes(rowTerm)
    )
  );
}

function deployOltOptionsForOrigin(originValue) {
  return _deployOltRows.filter(row => deployOltMatchesOrigin(row, originValue));
}

function deployLoadOltContextOptions(data) {
  const select = document.getElementById('deployOltContext');
  if (!select) return;
  _deployOltRows = (Array.isArray(data?.items) ? data.items : []).filter(row => row?.active);
  deployRenderOltContextForOrigin();
}

function deployRenderOltContextForOrigin() {
  const select = document.getElementById('deployOltContext');
  if (!select) return;
  const origin = document.getElementById('deployConnector')?.value || '';
  const rows = deployOltOptionsForOrigin(origin);
  select.disabled = !origin;
  select.innerHTML = !origin
    ? '<option value="">Escolha primeiro o conector</option>'
    : '<option value="">Sem OLT vinculada / informar manualmente</option>'
      + rows.map(row => `<option value="${esc(row.id)}">${esc(row.name)} - ${esc(row.site || 'sem site')} - ${esc(row.host)}</option>`).join('');
}

function deployApplyOltContext() {
  const id = document.getElementById('deployOltContext')?.value || '';
  const olt = _deployOltRows.find(row => String(row.id) === String(id));
  if (!olt) return;
  const site = document.getElementById('deploySite');
  if (site) site.value = olt.site || '';
  deployRenderConnectorStatus();
  deployRenderSummary();
  deployUpdateStepLocks({ autoAdvance: true });
  deployLoadAvailableRecorders();
}

async function loadDeployNew() {
  const sel = document.getElementById('deployConnector');
  if (!sel) return;
  const [data, oltData] = await Promise.all([
    apiJson('/api/connectors'),
    apiJson('/api/olt/registry'),
    loadDeploySites(),
  ]);
  _deployConnectors = (Array.isArray(data?.connectors) ? data.connectors : [])
    .filter(c => _connectorNorm(c.type) === 'routeros');
  const connectorOptions = _deployConnectors
    .map(c => {
      const online = deployConnectorOnline(c);
      const tunnel = deployConnectorVpnReady(c);
      const disabled = online && tunnel ? '' : 'disabled';
      const suffix = !online ? ' (offline)' : (!tunnel ? ' (sem VPN)' : ' + VPN');
      return `<option value="${esc(deployConnectorKey(c))}" ${disabled}>${esc(deployConnectorLabel(c))}${suffix}</option>`;
    })
    .join('');
  sel.innerHTML = `<option value="">Escolha a origem de acesso</option><option value="${DEPLOY_LOCAL_ORIGIN}">Local / VPN do servidor</option>${connectorOptions}`;
  sel.value = '';
  deployLoadOltContextOptions(oltData);
  deploymentApplyPreferredInventoryMode();
  deployApplyOriginFields();
  deploySetResult('Aguardando consulta no conector.');
  deployRenderConnectorStatus();
  deployRenderSummary();
  deployOpenStep('cftvStep1');
  await loadDeployHistory();
  bindAccordionExclusive('#viewDeployNew');
  deployBindStepGuards();
  await deployLoadAvailableRecorders();
  lucide.createIcons();
}

// Implantacao - Gravadores
let _deployStandaloneRecorderProbe = null;
let _deployStandaloneRecorderSaved = false;
let _deployRecorderSelectedChannel = 0;
let _deployStandaloneRecorderSavedItems = [];
let _deployStandaloneRecorderModalMode = 'create';

function deployStandaloneRecorderPayload() {
  const channels = Number(document.getElementById('deployStandaloneRecorderChannelTotal')?.value || 32);
  return {
    olt_id: Number(document.getElementById('deployStandaloneRecorderOlt')?.value || 0) || null,
    connector_id: deployStandaloneRecorderSelectedConnectorId(),
    inventory_mode: document.getElementById('deployStandaloneRecorderInventoryMode')?.value || 'basic',
    recorder_type: document.getElementById('deployStandaloneRecorderType')?.value || 'nvr',
    recorder_host: document.getElementById('deployStandaloneRecorderHost')?.value.trim() || '',
    recorder_http_port: Number(document.getElementById('deployStandaloneRecorderPort')?.value || 80),
    recorder_user: document.getElementById('deployStandaloneRecorderUser')?.value.trim() || 'admin',
    recorder_password: document.getElementById('deployStandaloneRecorderPassword')?.value || '',
    recorder_channel_total: Number.isFinite(channels) && channels > 0 ? channels : 32,
    channel_total: Number.isFinite(channels) && channels > 0 ? channels : 32,
    site: document.getElementById('deployStandaloneRecorderSite')?.value.trim() || '',
    name: document.getElementById('deployStandaloneRecorderName')?.value.trim() || '',
  };
}

function deployStandaloneRecorderConnectorValue() {
  return document.getElementById('deployStandaloneRecorderConnector')?.value || '';
}

function deployStandaloneRecorderSelectedConnectorId() {
  const value = deployStandaloneRecorderConnectorValue();
  return value && value !== DEPLOY_LOCAL_ORIGIN ? value : '';
}

function deployStandaloneRecorderSelectedConnector() {
  const id = deployStandaloneRecorderSelectedConnectorId();
  return _deployConnectors.find(c => deployConnectorKey(c) === id) || null;
}

function deployStandaloneRecorderOriginReady() {
  const value = deployStandaloneRecorderConnectorValue();
  if (value === DEPLOY_LOCAL_ORIGIN) return true;
  const connector = deployStandaloneRecorderSelectedConnector();
  return Boolean(connector && deployConnectorOnline(connector) && deployConnectorVpnReady(connector));
}

function deployStandaloneRecorderUpdateConnectorGate() {
  const ready = deployStandaloneRecorderOriginReady();
  const connector = deployStandaloneRecorderSelectedConnector();
  const site = document.getElementById('deployStandaloneRecorderSite');
  document.querySelectorAll('[data-recorder-needs-connector="1"]').forEach(el => {
    el.disabled = !ready;
    el.classList.toggle('is-disabled', !ready);
  });
  if (site) {
    site.readOnly = false;
    if (connector) site.value = deployConnectorSite(connector);
    if (!deployStandaloneRecorderConnectorValue()) site.value = '';
  }
}

function deployStandaloneRecorderRenderConnectorStatus() {
  const box = document.getElementById('deployStandaloneRecorderConnectorStatus');
  if (!box) return;
  const value = deployStandaloneRecorderConnectorValue();
  if (!value) {
    box.innerHTML = 'Escolha Local/VPN do servidor ou um conector online com VPN. Os dados do gravador ficam bloqueados ate definir a origem.';
    box.classList.add('error');
  } else if (value === DEPLOY_LOCAL_ORIGIN) {
    box.innerHTML = '<b style="color:var(--primary)">Modo local</b> -- usando o servidor/VPN ja roteada. Informe o site correto do gravador.';
    box.classList.remove('error');
  } else {
    const connector = deployStandaloneRecorderSelectedConnector();
    const online = deployConnectorOnline(connector);
    const tunnel = deployConnectorVpnReady(connector);
    box.innerHTML = online
      ? `<b style="color:var(--primary)">Conector online</b> -- ${esc(deployConnectorLabel(connector))}${tunnel ? ' -- VPN ativa para acessar o gravador.' : ' -- sem VPN configurada: o acesso fica bloqueado.'}`
      : `<b style="color:var(--danger)">Conector offline</b> -- ${esc(deployConnectorLabel(connector) || 'conector indisponivel')}.`;
    box.classList.toggle('error', !online || !tunnel);
  }
  deployStandaloneRecorderUpdateConnectorGate();
}

async function deployStandaloneRecorderLoadConnectors() {
  const select = document.getElementById('deployStandaloneRecorderConnector');
  if (!select) return;
  const data = await apiJson('/api/connectors');
  _deployConnectors = (Array.isArray(data?.connectors) ? data.connectors : [])
    .filter(c => _connectorNorm(c.type) === 'routeros');
  select.innerHTML = `<option value="">Escolha a origem de acesso</option><option value="${DEPLOY_LOCAL_ORIGIN}">Local / VPN do servidor</option>` + _deployConnectors.map(c => {
    const online = deployConnectorOnline(c);
    const tunnel = deployConnectorVpnReady(c);
    const disabled = online && tunnel ? '' : 'disabled';
    const suffix = !online ? ' (offline)' : (!tunnel ? ' (sem VPN)' : ' + VPN');
    return `<option value="${esc(deployConnectorKey(c))}" ${disabled}>${esc(deployConnectorLabel(c))}${suffix}</option>`;
  }).join('');
  select.value = '';
  deployStandaloneRecorderRenderConnectorStatus();
}

async function deployStandaloneRecorderLoadOlts() {
  const select = document.getElementById('deployStandaloneRecorderOlt');
  if (!select) return;
  const data = await apiJson('/api/olt/registry');
  const rows = (Array.isArray(data?.items) ? data.items : []).filter(row => row?.active);
  _deployOltRows = rows;
  deployStandaloneRecorderRenderOltsForOrigin();
}

function deployStandaloneRecorderRenderOltsForOrigin() {
  const select = document.getElementById('deployStandaloneRecorderOlt');
  if (!select) return;
  const origin = deployStandaloneRecorderConnectorValue();
  const rows = deployOltOptionsForOrigin(origin);
  select.disabled = !origin;
  select.innerHTML = !origin
    ? '<option value="">Escolha primeiro o conector</option>'
    : '<option value="">Sem OLT vinculada / informar manualmente</option>'
      + rows.map(row => `<option value="${esc(row.id)}">${esc(row.name)} - ${esc(row.site || 'sem site')} - ${esc(row.host)}</option>`).join('');
  deployStandaloneRecorderRenderSaved();
}

function deployStandaloneRecorderApplyOlt() {
  const id = document.getElementById('deployStandaloneRecorderOlt')?.value || '';
  const olt = _deployOltRows.find(row => String(row.id) === String(id));
  const site = document.getElementById('deployStandaloneRecorderSite');
  if (site) site.value = olt ? (olt.site || '') : '';
  deployStandaloneRecorderClearRecorderFields({ keepConnector: true, keepOlt: true, keepSite: true });
  deployStandaloneRecorderRenderSaved();
  deployStandaloneRecorderRenderProbe(_deployStandaloneRecorderProbe);
}

function deployStandaloneRecorderChannelsFromProbe(data = null) {
  const channels = Array.isArray(data?.channels) ? data.channels : [];
  return channels.map((item, idx) => {
    const channel = Number(item.channel || item.ch || idx + 1);
    const cameraIp = item.camera_ip || item.ip || item.remote_ip || '';
    const title = item.title || item.name || item.channel_name || '';
    const used = item.used !== undefined ? !!item.used : !!(cameraIp || title || item.enabled);
    return { ...item, channel, camera_ip: cameraIp, title, used };
  });
}

function deployRecorderChannelSnapshotUrl(item) {
  const raw = String(item?.snapshot_url || item?.imgbb_url || '').trim();
  if (!raw) return '';
  return /^https?:\/\//i.test(raw) ? raw : `${API_BASE}${raw}`;
}

function deployRenderRecorderChannelDetail(item = null) {
  const detail = document.getElementById('deployRecorderChannelDetail');
  const drawer = document.getElementById('deployRecorderChannelDrawer');
  const backdrop = document.getElementById('deployRecorderChannelDrawerBackdrop');
  if (!detail || !drawer || !backdrop) return;
  if (!item) {
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    backdrop.classList.remove('open');
    detail.innerHTML = '';
    return;
  }
  const snapshot = deployRecorderChannelSnapshotUrl(item);
  const channel = String(item.channel || '').padStart(2, '0');
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  backdrop.classList.add('open');
  detail.innerHTML = `
    <div class="recorder-channel-detail-head"><div><span>CANAL ${esc(channel)}</span><h4>${esc(item.title || (item.used ? 'Canal ocupado' : 'Canal livre'))}</h4></div><button type="button" data-recorder-channel-action="close" aria-label="Fechar"><i data-lucide="x"></i></button></div>
    <div class="recorder-channel-preview">${snapshot ? `<img src="${esc(snapshot)}" alt="Snapshot do canal ${esc(channel)}">` : '<div><i data-lucide="image-off"></i><span>Sem snapshot</span></div>'}</div>
    <div class="recorder-channel-fields">
      <div><span>Status</span><b>${item.used ? 'Ocupado' : 'Livre'}</b></div>
      <div><span>IP da camera</span><b class="monospace">${esc(item.camera_ip || '-')}</b></div>
      <div><span>Modelo</span><b>${esc(item.camera_model || item.model || '-')}</b></div>
      <div><span>MAC</span><b class="monospace">${esc(item.camera_mac || item.mac || '-')}</b></div>
    </div>
    <div class="recorder-channel-detail-actions">
      ${item.used ? '<button type="button" class="secondary-action" data-recorder-channel-action="refresh"><i data-lucide="refresh-cw"></i> Atualizar</button><button type="button" class="secondary-action" data-recorder-channel-action="web"><i data-lucide="globe"></i> Web</button><button type="button" class="primary-action" data-recorder-channel-action="ping"><i data-lucide="activity"></i> Ping</button><button type="button" class="secondary-action danger-action" data-recorder-channel-action="delete"><i data-lucide="trash-2"></i> Excluir canal</button>' : '<button type="button" class="primary-action" data-recorder-channel-action="add"><i data-lucide="plus-circle"></i> Adicionar camera</button>'}
    </div>`;
  lucide.createIcons();
}

function deployRenderStandaloneRecorderChannels() {
  const grid = document.getElementById('deployStandaloneRecorderChannels');
  const counters = document.getElementById('deployRecorderChannelCounters');
  if (!grid || !counters) return;
  const channels = deployStandaloneRecorderChannelsFromProbe(_deployStandaloneRecorderProbe);
  const search = (document.getElementById('deployRecorderChannelSearch')?.value || '').trim().toLowerCase();
  const filter = document.getElementById('deployRecorderChannelFilter')?.value || 'all';
  const used = channels.filter(item => item.used).length;
  const free = channels.length - used;
  const missingModel = channels.filter(item => item.used && !(item.camera_model || item.model)).length;
  const missingSnapshot = channels.filter(item => item.used && !deployRecorderChannelSnapshotUrl(item)).length;
  counters.innerHTML = `<span><b>${esc(channels.length)}</b> total</span><span class="online"><b>${esc(used)}</b> ocupados</span><span class="free"><b>${esc(free)}</b> livres</span><span class="warning"><b>${esc(missingModel)}</b> sem modelo</span><span class="warning"><b>${esc(missingSnapshot)}</b> sem snapshot</span>`;
  const visible = channels.filter(item => {
    if (filter === 'used' && !item.used) return false;
    if (filter === 'free' && item.used) return false;
    if (filter === 'no_model' && (!item.used || item.camera_model || item.model)) return false;
    if (filter === 'no_snapshot' && (!item.used || deployRecorderChannelSnapshotUrl(item))) return false;
    if (search && ![item.channel, item.title, item.camera_ip, item.camera_model, item.model, item.camera_mac].some(value => String(value || '').toLowerCase().includes(search))) return false;
    return true;
  });
  if (!visible.length) {
    grid.innerHTML = '<div class="recorder-deploy-empty">Nenhum canal encontrado neste filtro.</div>';
  } else {
    grid.innerHTML = visible.map(item => {
      const snapshot = deployRecorderChannelSnapshotUrl(item);
      const ch = String(item.channel || '').padStart(2, '0');
      return `<button type="button" class="recorder-deploy-channel ${item.used ? 'used' : 'free'} ${Number(item.channel) === _deployRecorderSelectedChannel ? 'selected' : ''}" data-recorder-channel="${esc(item.channel)}">
        <div class="recorder-channel-thumb">${snapshot ? `<img src="${esc(snapshot)}" alt="">` : `<i data-lucide="${item.used ? 'image-off' : 'plus'}"></i>`}</div>
        <div class="recorder-channel-card-body"><div><b>CH ${esc(ch)}</b><span>${item.used ? 'ocupado' : 'livre'}</span></div><strong title="${esc(item.title || '')}">${esc(item.title || (item.used ? 'Sem titulo' : 'Disponivel'))}</strong><small class="monospace">${esc(item.camera_ip || '')}</small><small>${esc(item.camera_model || item.model || (item.used ? 'Modelo nao informado' : 'Pronto para adicionar'))}</small></div>
      </button>`;
    }).join('');
  }
  const selected = channels.find(item => Number(item.channel) === _deployRecorderSelectedChannel) || null;
  deployRenderRecorderChannelDetail(selected);
  lucide.createIcons();
}

function deployStandaloneRecorderSetResult(html, isError = false) {
  const box = document.getElementById('deployStandaloneRecorderResult');
  if (!box) return;
  box.innerHTML = html || 'Entre no gravador para validar modelo, serial e canais.';
  box.classList.toggle('error', !!isError);
}

function deployStandaloneRecorderSetQuickResult(html, isError = false) {
  const box = document.getElementById('deployStandaloneRecorderQuickResult');
  if (!box) return;
  box.innerHTML = html || 'Entre no gravador para liberar as configuracoes rapidas.';
  box.classList.toggle('error', !!isError);
}

function deployStandaloneRecorderRenderLoginProgress(payload) {
  const summary = document.getElementById('deployStandaloneRecorderSummary');
  const headStatus = document.getElementById('deployRecorderDiscoveryStatus');
  if (headStatus) {
    headStatus.className = 'recorder-head-status loading';
    headStatus.innerHTML = '<i data-lucide="loader"></i><span>Conectando</span>';
  }
  if (summary) {
    summary.innerHTML = `
      <div class="recorder-discovery-empty recorder-discovery-loading">
        <i data-lucide="loader"></i>
        <div>
          <b>Entrando no gravador ${esc(payload.recorder_host)}</b>
          <span>Validando credenciais, coletando modelo, serial e canais. Isso pode levar alguns segundos pela VPN.</span>
        </div>
      </div>
      <div class="recorder-discovery-checklist">
        <div class="done"><i data-lucide="check"></i><span><b>1. Conector</b><small>Origem de acesso definida</small></span></div>
        <div class="done active"><i data-lucide="loader"></i><span><b>2. Login</b><small>Aguardando resposta do gravador</small></span></div>
        <div><i data-lucide="circle"></i><span><b>3. Canais</b><small>Proxima coleta</small></span></div>
        <div><i data-lucide="circle"></i><span><b>4. Inventario</b><small>Revisar e salvar</small></span></div>
      </div>
    `;
  }
  deployStandaloneRecorderSetQuickResult(`Aguardando resposta de ${esc(payload.recorder_host)} antes de liberar as acoes.`);
  lucide.createIcons();
}

function deployStandaloneRecorderSetModalMode(mode = 'create') {
  _deployStandaloneRecorderModalMode = mode === 'entry' ? 'entry' : 'create';
  const title = document.getElementById('deployStandaloneRecorderModalTitle');
  const subtitle = document.getElementById('deployStandaloneRecorderModalSubtitle');
  const action = document.getElementById('btnDeployStandaloneRecorderLoginModal');
  const isEntry = _deployStandaloneRecorderModalMode === 'entry';
  if (title) title.textContent = isEntry ? 'Entrar em gravador' : 'Cadastrar gravador';
  if (subtitle) {
    subtitle.textContent = isEntry
      ? 'Escolha um gravador cadastrado por site e use a credencial salva para abrir o console.'
      : 'Informe site, acesso e quantidade de canais para registrar no inventario.';
  }
  if (action) {
    action.innerHTML = isEntry ? '<i data-lucide="log-in"></i> Entrar' : '<i data-lucide="radar"></i> Validar gravador';
  }
  document.querySelectorAll('[data-recorder-modal-mode]').forEach(el => {
    const modes = String(el.dataset.recorderModalMode || '').split(/\s+/).filter(Boolean);
    el.classList.toggle('hidden', !modes.includes(_deployStandaloneRecorderModalMode));
  });
  lucide.createIcons();
}

function openDeployStandaloneRecorderModal(mode = 'create') {
  deployStandaloneRecorderSetModalMode(mode);
  document.getElementById('modalDeployStandaloneRecorder')?.classList.remove('hidden');
  document.body.classList.add('modal-open');
  deployStandaloneRecorderRenderSaved();
  lucide.createIcons();
}

function openDeployStandaloneRecorderEntryModal() {
  openDeployStandaloneRecorderModal('entry');
}

function closeDeployStandaloneRecorderModal() {
  document.getElementById('modalDeployStandaloneRecorder')?.classList.add('hidden');
  document.body.classList.remove('modal-open');
}

function deployStandaloneRecorderUpdateQuickActions() {
  const enabled = !!_deployStandaloneRecorderProbe;
  [
    'btnDeployStandaloneRecorderOpenWeb',
    'btnDeployStandaloneRecorderRefreshChannels',
    'btnDeployStandaloneRecorderPlayback',
    'btnDeployStandaloneRecorderSetNtp',
    'btnDeployStandaloneRecorderReboot',
    'btnDeployStandaloneRecorderFicha',
  ].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !enabled;
  });
}

function deployStandaloneRecorderSelectConfigTab(tab = 'overview') {
  const target = tab || 'overview';
  document.querySelectorAll('.recorder-config-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.recorderConfigTab === target);
  });
  document.querySelectorAll('.recorder-config-panel').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.recorderConfigPanel === target);
  });
}

function deployStandaloneRecorderRenderProbe(data = null) {
  const summary = document.getElementById('deployStandaloneRecorderSummary');
  const channelsBox = document.getElementById('deployStandaloneRecorderChannels');
  const headStatus = document.getElementById('deployRecorderDiscoveryStatus');
  if (!summary || !channelsBox) return;
  if (!data) {
    const originReady = deployStandaloneRecorderOriginReady();
    summary.innerHTML = `
      <div class="recorder-discovery-empty">
        <i data-lucide="${originReady ? 'log-in' : 'plug-zap'}"></i>
        <div><b>${originReady ? 'Pronto para validar o gravador' : 'Escolha primeiro o conector'}</b><span>${originReady ? 'Abra Novo gravador, informe host e credenciais e clique em Entrar.' : 'Os dados e as acoes serao liberados quando a origem estiver definida.'}</span></div>
      </div>
      <div class="recorder-discovery-checklist">
        <div class="${originReady ? 'done' : ''}"><i data-lucide="${originReady ? 'check' : 'circle'}"></i><span><b>1. Conector</b><small>${originReady ? 'Origem de acesso definida' : 'Aguardando origem'}</small></span></div>
        <div><i data-lucide="circle"></i><span><b>2. Login</b><small>Validar modelo e serial</small></span></div>
        <div><i data-lucide="circle"></i><span><b>3. Canais</b><small>Identificar ocupados e livres</small></span></div>
        <div><i data-lucide="circle"></i><span><b>4. Inventario</b><small>Revisar e salvar</small></span></div>
      </div>
    `;
    _deployRecorderSelectedChannel = 0;
    deployRenderStandaloneRecorderChannels();
    if (headStatus) {
      headStatus.className = 'recorder-head-status waiting';
      headStatus.innerHTML = '<i data-lucide="circle"></i><span>Aguardando login</span>';
    }
    deployStandaloneRecorderUpdateQuickActions();
    lucide.createIcons();
    return;
  }
  const channels = deployStandaloneRecorderChannelsFromProbe(data);
  const used = channels.filter(ch => !!ch.used).length;
  const free = channels.length ? channels.length - used : 0;
  const total = Number(data.channel_total || channels.length || 0);
  const payload = deployStandaloneRecorderPayload();
  const brand = data.brand || 'Fabricante nao informado';
  const model = data.model || 'Modelo nao informado';
  const modeLabel = { basic: 'Basico', olt: 'Via OLT', switch: 'Via Switch' }[payload.inventory_mode] || 'Basico';
  if (headStatus) {
    headStatus.className = 'recorder-head-status online';
    headStatus.innerHTML = '<i data-lucide="circle-check"></i><span>Conectado</span>';
  }
  summary.innerHTML = `
    <div class="recorder-discovery-identity">
      <div><span>Fabricante</span><b>${esc(brand)}</b></div>
      <div><span>Modelo</span><b>${esc(model)}</b></div>
      <div class="identity-serial"><span>Serial</span><b class="monospace">${esc(data.serial || 'nao informado')}</b></div>
    </div>
    <div class="recorder-discovery-metrics">
      <div><span>Total</span><b>${esc(total)}</b><small>canais</small></div>
      <div class="metric-online"><span>Ocupados</span><b>${esc(used)}</b><small>com camera</small></div>
      <div class="metric-free"><span>Livres</span><b>${esc(free)}</b><small>disponiveis</small></div>
    </div>
    <div class="recorder-discovery-context">
      <div><span>Host</span><b class="monospace">${esc(payload.recorder_host)}</b></div>
      <div><span>Site / local</span><b>${esc(payload.site || '-')}</b></div>
      <div><span>Tipo</span><b>${esc(payload.recorder_type.toUpperCase())}</b></div>
      <div><span>Destino</span><b>${esc(modeLabel)}</b></div>
    </div>
    <div class="recorder-discovery-checklist compact">
      <div class="done"><i data-lucide="check"></i><span><b>Conector</b><small>Rota pronta</small></span></div>
      <div class="done"><i data-lucide="check"></i><span><b>Login</b><small>Credenciais OK</small></span></div>
      <div class="done"><i data-lucide="check"></i><span><b>Canais</b><small>${esc(used)} detectados</small></span></div>
      <div class="${_deployStandaloneRecorderSaved ? 'done' : ''}"><i data-lucide="${_deployStandaloneRecorderSaved ? 'check' : 'circle'}"></i><span><b>Inventario</b><small>${_deployStandaloneRecorderSaved ? 'Salvo' : 'Falta salvar'}</small></span></div>
    </div>
    <div class="recorder-discovery-actions">
      <button type="button" class="secondary-action" data-recorder-overview-action="refresh"><i data-lucide="refresh-cw"></i> Revalidar</button>
      <button type="button" class="secondary-action" data-recorder-overview-action="channels"><i data-lucide="layout-grid"></i> Ver canais</button>
      <button type="button" class="primary-action" id="btnDeployStandaloneRecorderSaveOverview" data-recorder-overview-action="save"><i data-lucide="${_deployStandaloneRecorderSaved ? 'check' : 'save'}"></i> ${_deployStandaloneRecorderSaved ? 'Salvo no inventario' : 'Salvar no inventario'}</button>
    </div>
  `;
  deployRenderStandaloneRecorderChannels();
  deployStandaloneRecorderSetQuickResult('Login confirmado. Escolha uma configuracao rapida para executar.');
  deployStandaloneRecorderUpdateQuickActions();
  lucide.createIcons();
}

async function loadDeployRecorderSites() {
  const list = document.getElementById('deployStandaloneRecorderSiteList');
  if (!list) return;
  const sites = new Set(_deploySites || []);
  const cameraModes = ['olt', 'basico', 'switch'];
  const cameraResults = await Promise.all(cameraModes.map(mode => apiJson(`/api/cameras?mode=${encodeURIComponent(mode)}`).catch(() => null)));
  cameraResults.forEach(data => {
    const rows = Array.isArray(data?.cameras) ? data.cameras : (Array.isArray(data?.rows) ? data.rows : []);
    rows.forEach(row => {
      const site = String(row.site || row.site_name || row.local || '').trim();
      if (site) sites.add(site);
    });
  });
  const recorderResults = await Promise.all([
    apiJson('/api/nvr/inventory?site=').catch(() => null),
    apiJson('/api/dvr/inventory?site=').catch(() => null),
  ]);
  recorderResults.forEach(data => {
    const rows = deployStandaloneRecorderInventoryRows(data);
    rows.forEach(row => {
      const site = String(row.site || row.site_name || row.local || '').trim();
      if (site) sites.add(site);
    });
  });
  _deploySites = [...sites].sort((a, b) => a.localeCompare(b, 'pt-BR'));
  list.innerHTML = _deploySites.map(site => `<option value="${esc(site)}"></option>`).join('');
}

function deployStandaloneRecorderInventoryRows(data) {
  if (Array.isArray(data?.inventory)) return data.inventory;
  if (Array.isArray(data?.rows)) return data.rows;
  if (Array.isArray(data?.recorders)) return data.recorders;
  if (Array.isArray(data)) return data;
  return [];
}

function deployStandaloneRecorderRowHost(row) {
  return String(row?.host || row?.recorder_host || row?.nvr_host || row?.dvr_host || row?.ip || '').trim();
}

function deployStandaloneRecorderRowPort(row) {
  const value = Number(row?.http_port || row?.recorder_http_port || row?.port || 80);
  return value > 0 ? value : 80;
}

function deployStandaloneRecorderRowMode(row) {
  const raw = String(row?.inventory_mode || row?.mode || '').trim().toLowerCase();
  if (raw === 'basico' || raw === 'basic') return 'basic';
  if (raw === 'switch') return 'switch';
  if (raw === 'olt') return 'olt';
  return 'basic';
}

function deployStandaloneRecorderModeLabel(mode) {
  if (mode === 'olt') return 'Via OLT';
  if (mode === 'switch') return 'Via Switch';
  return 'Basico';
}

function deployStandaloneRecorderTypeLabel(type) {
  return type === 'dvr' ? 'DVR analogico' : 'NVR IP';
}

function deployStandaloneRecorderSortIp(value) {
  return String(value || '').split('.').map(part => Number(part) || 0);
}

function deployStandaloneRecorderCompareIp(a, b) {
  const pa = deployStandaloneRecorderSortIp(a);
  const pb = deployStandaloneRecorderSortIp(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const diff = (pa[i] || 0) - (pb[i] || 0);
    if (diff) return diff;
  }
  return String(a || '').localeCompare(String(b || ''), 'pt-BR', { numeric: true });
}

function deployStandaloneRecorderGroupRows(rows, type) {
  const groups = new Map();
  rows.forEach(row => {
    if (!row || typeof row !== 'object') return;
    const host = deployStandaloneRecorderRowHost(row);
    if (!host) return;
    const port = deployStandaloneRecorderRowPort(row);
    const mode = deployStandaloneRecorderRowMode(row);
    const connectorId = deployRecorderRowConnectorId(row);
    const site = deployRecorderRowSite(row);
    const key = [type, mode, connectorId, site.toLowerCase(), host, port].join('|');
    const item = groups.get(key) || {
      key,
      type,
      mode,
      connectorId,
      site,
      host,
      port,
      user: '',
      password: '',
      name: '',
      model: '',
      serial: '',
      totalChannels: 0,
      usedChannels: 0,
      rows: [],
    };
    item.user = item.user || String(row.recorder_user || row.user || 'admin').trim();
    item.password = item.password || String(row.recorder_password || row.password || '').trim();
    item.name = item.name || String(row.name || row.recorder_name || row.nvr_name || row.dvr_name || '').trim();
    item.model = item.model || String(row.nvr_model || row.dvr_model || row.modelo || row.model || '').trim();
    item.serial = item.serial || String(row.equip_serial || row.serial || row.nvr_serial || row.dvr_serial || '').trim();
    const channel = Number(row.channel || row.ch || 0);
    if (channel > item.totalChannels) item.totalChannels = channel;
    const status = String(row.status || '').trim().toLowerCase();
    if (status !== 'offline') item.usedChannels += 1;
    item.rows.push(row);
    groups.set(key, item);
  });
  return [...groups.values()].map(item => ({
    ...item,
    user: item.user || 'admin',
    totalChannels: item.totalChannels || item.rows.length || 32,
  }));
}

function deployStandaloneRecorderRenderSaved() {
  const select = document.getElementById('deployStandaloneRecorderSavedSelect');
  if (!select) return;
  const origin = deployStandaloneRecorderConnectorValue();
  const selectedOltId = document.getElementById('deployStandaloneRecorderOlt')?.value || '';
  const selectedOlt = _deployOltRows.find(row => String(row.id) === String(selectedOltId));
  const selectedSite = String(selectedOlt?.site || '').trim();
  const selectedConnectorId = deployStandaloneRecorderSelectedConnectorId();
  if (!origin) {
    select.innerHTML = '<option value="">Escolha primeiro o conector</option>';
    select.disabled = true;
    return;
  }
  if (!selectedOltId || !selectedSite) {
    select.innerHTML = '<option value="">Escolha uma OLT cadastrada</option>';
    select.disabled = true;
    return;
  }
  const items = _deployStandaloneRecorderSavedItems.filter(item => {
    const sameSite = String(item.site || '').trim().toLowerCase() === selectedSite.toLowerCase();
    if (!sameSite) return false;
    if (selectedConnectorId && item.connectorId) return String(item.connectorId) === String(selectedConnectorId);
    return true;
  });
  if (!items.length) {
    select.innerHTML = `<option value="">Nenhum gravador cadastrado em ${esc(selectedSite)}</option>`;
    select.disabled = true;
    return;
  }
  const bySite = new Map();
  items.forEach(item => {
    const site = item.site || 'Sem site';
    if (!bySite.has(site)) bySite.set(site, []);
    bySite.get(site).push(item);
  });
  select.disabled = false;
  select.innerHTML = '<option value="">Escolha um gravador cadastrado</option>' + [...bySite.entries()].map(([site, items]) => `
    <optgroup label="${esc(site)}">
      ${items.map(item => {
        const title = item.name || item.host;
        const label = [
          title,
          item.host,
          deployStandaloneRecorderTypeLabel(item.type),
          `${item.usedChannels}/${item.totalChannels} canais`,
          deployStandaloneRecorderModeLabel(item.mode),
        ].filter(Boolean).join(' - ');
        return `<option value="${esc(item.key)}">${esc(label)}</option>`;
      }).join('')}
    </optgroup>
  `).join('');
}

async function deployStandaloneRecorderLoadSaved() {
  const select = document.getElementById('deployStandaloneRecorderSavedSelect');
  if (select) {
    select.disabled = true;
    select.innerHTML = '<option value="">Carregando gravadores cadastrados...</option>';
  }
  const [nvrData, dvrData] = await Promise.all([
    apiJson('/api/nvr/inventory?site=').catch(() => null),
    apiJson('/api/dvr/inventory?site=').catch(() => null),
  ]);
  _deployStandaloneRecorderSavedItems = [
    ...deployStandaloneRecorderGroupRows(deployStandaloneRecorderInventoryRows(nvrData), 'nvr'),
    ...deployStandaloneRecorderGroupRows(deployStandaloneRecorderInventoryRows(dvrData), 'dvr'),
  ].sort((a, b) => {
    const siteDiff = String(a.site || '').localeCompare(String(b.site || ''), 'pt-BR', { numeric: true });
    if (siteDiff) return siteDiff;
    const hostDiff = deployStandaloneRecorderCompareIp(a.host, b.host);
    if (hostDiff) return hostDiff;
    return a.type.localeCompare(b.type);
  });
  deployStandaloneRecorderRenderSaved();
}

function deployStandaloneRecorderUseSaved(key) {
  const item = _deployStandaloneRecorderSavedItems.find(row => row.key === key);
  if (!item) return;
  const setValue = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.value = value ?? '';
  };
  const connector = document.getElementById('deployStandaloneRecorderConnector');
  if (connector) {
    const target = item.connectorId || DEPLOY_LOCAL_ORIGIN;
    connector.value = [...connector.options].some(opt => opt.value === target) ? target : DEPLOY_LOCAL_ORIGIN;
  }
  deployStandaloneRecorderRenderOltsForOrigin();
  deployStandaloneRecorderRenderConnectorStatus();
  setValue('deployStandaloneRecorderSite', item.site);
  setValue('deployStandaloneRecorderType', item.type);
  setValue('deployStandaloneRecorderInventoryMode', item.mode);
  setValue('deployStandaloneRecorderHost', item.host);
  setValue('deployStandaloneRecorderPort', item.port);
  setValue('deployStandaloneRecorderChannelTotal', item.totalChannels || 32);
  setValue('deployStandaloneRecorderUser', item.user || 'admin');
  setValue('deployStandaloneRecorderPassword', item.password || '');
  setValue('deployStandaloneRecorderName', item.name || item.site || '');
  _deployStandaloneRecorderProbe = null;
  _deployStandaloneRecorderSaved = false;
  _deployRecorderSelectedChannel = 0;
  deployStandaloneRecorderRenderProbe(null);
  deployStandaloneRecorderSetResult('Gravador carregado do inventario. Informe a senha e clique em Entrar para abrir o console.');
  deployStandaloneRecorderSetQuickResult('Entre no gravador para liberar as configuracoes rapidas.');
  const savedSelect = document.getElementById('deployStandaloneRecorderSavedSelect');
  if (savedSelect) savedSelect.value = '';
  deployStandaloneRecorderSelectConfigTab('overview');
  if (item.password) {
    closeDeployStandaloneRecorderModal();
    deployStandaloneRecorderSetResult(`Gravador ${esc(item.host)} carregado. Entrando com a credencial salva...`);
    deployStandaloneRecorderLogin();
    showToast(`Gravador ${item.host} carregado.`);
  } else {
    openDeployStandaloneRecorderModal('entry');
    const pass = document.getElementById('deployStandaloneRecorderPassword');
    if (pass) pass.focus();
    deployStandaloneRecorderSetResult('Senha nao encontrada no cadastro salvo. Informe a senha uma vez e salve o gravador novamente.', true);
    showToast('Senha nao encontrada no cadastro salvo.', true);
  }
  lucide.createIcons();
}

function deployStandaloneRecorderClearRecorderFields({ keepConnector = false, keepOlt = false, keepSite = false } = {}) {
  _deployStandaloneRecorderProbe = null;
  _deployStandaloneRecorderSaved = false;
  _deployRecorderSelectedChannel = 0;
  const ids = [
    'deployStandaloneRecorderHost',
    'deployStandaloneRecorderPassword',
    'deployStandaloneRecorderName',
  ];
  if (!keepSite) ids.unshift('deployStandaloneRecorderSite');
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const port = document.getElementById('deployStandaloneRecorderPort');
  const channels = document.getElementById('deployStandaloneRecorderChannelTotal');
  const user = document.getElementById('deployStandaloneRecorderUser');
  if (port) port.value = '80';
  if (channels) channels.value = '32';
  if (user) user.value = 'admin';
  const connector = document.getElementById('deployStandaloneRecorderConnector');
  const olt = document.getElementById('deployStandaloneRecorderOlt');
  if (!keepConnector && connector) connector.value = '';
  if (!keepOlt && olt) olt.value = '';
  deployStandaloneRecorderRenderProbe(null);
  deployStandaloneRecorderSetResult('Entre no gravador para validar modelo, serial e canais.');
  deployStandaloneRecorderSetQuickResult('Entre no gravador para liberar as configuracoes rapidas.');
}

async function loadDeployRecorder() {
  deploymentApplyPreferredInventoryMode();
  await Promise.all([loadDeployRecorderSites(), deployStandaloneRecorderLoadConnectors(), deployStandaloneRecorderLoadOlts(), deployStandaloneRecorderLoadSaved()]);
  deployStandaloneRecorderRenderOltsForOrigin();
  deployStandaloneRecorderRenderProbe(_deployStandaloneRecorderProbe);
  deployStandaloneRecorderSelectConfigTab(
    document.querySelector('.recorder-config-tab.active')?.dataset.recorderConfigTab || 'overview'
  );
  lucide.createIcons();
}

async function deployStandaloneRecorderLogin() {
  _deployStandaloneRecorderSaved = false;
  const payload = deployStandaloneRecorderPayload();
  if (!payload.site) {
    deployStandaloneRecorderSetResult('Informe o site/local antes de cadastrar o gravador.', true);
    showToast('Informe o site/local.', true);
    return;
  }
  if (!payload.recorder_host || !payload.recorder_user || !payload.recorder_password) {
    deployStandaloneRecorderSetResult('Informe host, usuario e senha do gravador.', true);
    showToast('Informe host, usuario e senha do gravador.', true);
    return;
  }
  const btn = document.getElementById('btnDeployStandaloneRecorderLoginModal');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Entrando'; lucide.createIcons(); }
  deployStandaloneRecorderSetResult(`<span class="inline-loading"><i data-lucide="loader"></i> Conectando em ${esc(payload.recorder_host)}...</span>`);
  deployStandaloneRecorderRenderLoginProgress(payload);
  showToast(`Conectando em ${payload.recorder_host}...`);
  try {
    const res = await api('/api/deployments/recorder-login', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      const detail = data?.detail || data?.message || 'Falha ao entrar no gravador.';
      _deployStandaloneRecorderProbe = null;
      deployStandaloneRecorderRenderProbe(null);
      deployStandaloneRecorderSetResult(esc(detail), true);
      showToast(detail, true);
      return;
    }
    _deployStandaloneRecorderProbe = data;
    deployStandaloneRecorderRenderProbe(data);
    const label = [data.brand, data.model, data.serial].filter(Boolean).join(' / ');
    deployStandaloneRecorderSetResult(`Login confirmado em ${esc(payload.recorder_host)}${label ? ` - ${esc(label)}` : ''}. Agora pode salvar no inventario.`);
    closeDeployStandaloneRecorderModal();
    showToast('Login do gravador confirmado.');
  } catch (err) {
    const detail = err?.detail || err?.message || 'Falha ao entrar no gravador.';
    _deployStandaloneRecorderProbe = null;
    deployStandaloneRecorderRenderProbe(null);
    deployStandaloneRecorderSetResult(esc(detail), true);
    showToast(detail, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = _deployStandaloneRecorderModalMode === 'entry'
        ? '<i data-lucide="log-in"></i> Entrar'
        : '<i data-lucide="radar"></i> Validar gravador';
      lucide.createIcons();
    }
  }
}

function deployStandaloneRecorderRows(payload, probe) {
  const channels = deployStandaloneRecorderChannelsFromProbe(probe);
  const byChannel = new Map(channels.map(item => [Number(item.channel || 0), item]));
  const total = Number(probe?.channel_total || payload.recorder_channel_total || channels.length || 32);
  const model = probe?.model || '';
  const serial = probe?.serial || '';
  return Array.from({ length: total }, (_, idx) => {
    const channel = idx + 1;
    const live = byChannel.get(channel) || {};
    const used = !!live.used;
    const title = live.title || `${String(channel).padStart(2, '0')} - LIVRE`;
    const row = {
      remote: Boolean(payload.connector_id),
      remote_connector_id: payload.connector_id || '',
      inventory_mode: payload.inventory_mode || 'basic',
      host: payload.recorder_host,
      http_port: payload.recorder_http_port,
      recorder_user: payload.recorder_user,
      recorder_password: payload.recorder_password,
      name: payload.name,
      channel,
      title,
      local: payload.site,
      site: payload.site,
      status: used ? 'online' : 'offline',
      video_loss: used ? 'nao' : 'sim',
      equip_serial: serial,
      snapshot_url: live.snapshot_url || '',
      imgbb_url: live.imgbb_url || '',
      imgbb_thumb_url: live.imgbb_thumb_url || '',
    };
    if (payload.recorder_type === 'dvr') {
      row.modelo = model;
      row.model = model;
      row.mac = live.mac || '';
    } else {
      row.nvr_model = model;
      row.modelo = model;
      row.camera_ip = live.camera_ip || '';
      row.camera_model = live.camera_model || live.model || '';
      row.camera_mac = live.mac || live.camera_mac || '';
      row.mac = live.mac || live.camera_mac || '';
    }
    return row;
  }).filter(row => row.status === 'online');
}

async function deployStandaloneRecorderSave() {
  const payload = deployStandaloneRecorderPayload();
  if (!payload.site || !payload.recorder_host) {
    deployStandaloneRecorderSetResult('Site/local e host do gravador sao obrigatorios.', true);
    showToast('Informe site e host do gravador.', true);
    return;
  }
  if (!_deployStandaloneRecorderProbe) {
    deployStandaloneRecorderSetResult('Entre no gravador antes de salvar no inventario.', true);
    showToast('Entre no gravador antes de salvar.', true);
    return;
  }
  const rows = deployStandaloneRecorderRows(payload, _deployStandaloneRecorderProbe);
  if (!rows.length) {
    deployStandaloneRecorderSetResult('Nenhum canal encontrado para salvar.', true);
    showToast('Nenhum canal encontrado.', true);
    return;
  }
  const btn = document.getElementById('btnDeployStandaloneRecorderSaveOverview');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Salvando'; lucide.createIcons(); }
  try {
    const endpoint = payload.recorder_type === 'dvr' ? '/api/dvr/save' : '/api/nvr/save';
    const res = await api(endpoint, { method: 'POST', body: JSON.stringify({ recorders: rows }) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      const detail = data?.detail || data?.message || 'Falha ao salvar gravador no inventario.';
      deployStandaloneRecorderSetResult(esc(detail), true);
      showToast(detail, true);
      return;
    }
    deployStandaloneRecorderSetResult(`${rows.length} canal(is) salvos no inventario de ${payload.recorder_type.toUpperCase()} para ${esc(payload.site)}.`);
    _deployStandaloneRecorderSaved = true;
    await deployStandaloneRecorderLoadSaved();
    deployStandaloneRecorderRenderProbe(_deployStandaloneRecorderProbe);
    showToast('Gravador salvo no inventario.');
  } catch (err) {
    const detail = err?.detail || err?.message || 'Falha ao salvar gravador no inventario.';
    deployStandaloneRecorderSetResult(esc(detail), true);
    showToast(detail, true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="save"></i> Salvar no inventario'; lucide.createIcons(); }
  }
}

function deployStandaloneRecorderClear() {
  deployStandaloneRecorderClearRecorderFields();
  deployStandaloneRecorderRenderOltsForOrigin();
  deploymentApplyPreferredInventoryMode();
  deployStandaloneRecorderRenderConnectorStatus();
  lucide.createIcons();
}

function deployStandaloneRecorderEndpointBase(payload = deployStandaloneRecorderPayload()) {
  return payload.recorder_type === 'dvr' ? '/api/dvr' : '/api/nvr';
}

function deployStandaloneRecorderCommonPayload(payload = deployStandaloneRecorderPayload()) {
  return {
    ip: payload.recorder_host,
    http_port: payload.recorder_http_port,
    user: payload.recorder_user,
    password: payload.recorder_password,
    timeout_sec: 10,
  };
}

function deployStandaloneRecorderWebUrl(payload = deployStandaloneRecorderPayload()) {
  const port = Number(payload.recorder_http_port || 80);
  const suffix = port && port !== 80 ? `:${port}` : '';
  return `http://${payload.recorder_host}${suffix}`;
}

function deployStandaloneRecorderFirstUsedChannel() {
  const channels = deployStandaloneRecorderChannelsFromProbe(_deployStandaloneRecorderProbe);
  return channels.find(ch => ch.used)?.channel || 1;
}

function deployStandaloneRecorderRequireLogin() {
  if (_deployStandaloneRecorderProbe) return true;
  deployStandaloneRecorderSetQuickResult('Entre no gravador antes de executar esta acao.', true);
  showToast('Entre no gravador antes.', true);
  return false;
}

async function deployStandaloneRecorderDeleteChannel(item) {
  if (!deployStandaloneRecorderRequireLogin()) return;
  const payload = deployStandaloneRecorderPayload();
  const channel = Number(item?.channel || 0);
  if (!channel) {
    showToast('Selecione um canal para excluir.', true);
    return;
  }
  const title = item?.title || item?.camera_ip || `Canal ${String(channel).padStart(2, '0')}`;
  const ok = await showConfirm({
    eyebrow: 'Gravador',
    title: `Excluir canal ${String(channel).padStart(2, '0')}`,
    msg: `Remover "${title}" do gravador ${payload.recorder_host}? Isso limpa a camera configurada neste canal e atualiza o inventario salvo.`,
    label: 'Excluir canal',
    danger: true,
  });
  if (!ok) return;

  deployStandaloneRecorderSetQuickResult(`Excluindo canal ${String(channel).padStart(2, '0')} de ${esc(payload.recorder_host)}...`);
  showToast(`Excluindo canal ${String(channel).padStart(2, '0')}...`);
  try {
    const res = await api('/api/deployments/recorder-remove-camera', {
      method: 'POST',
      body: JSON.stringify({ ...payload, recorder_channel: channel, channel }),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      const detail = data?.detail || data?.message || 'Falha ao excluir canal.';
      deployStandaloneRecorderSetQuickResult(esc(detail), true);
      showToast(detail, true);
      return;
    }
    _deployStandaloneRecorderProbe = { ..._deployStandaloneRecorderProbe, ...data, channels: data.channels || [] };
    _deployRecorderSelectedChannel = 0;
    deployStandaloneRecorderRenderProbe(_deployStandaloneRecorderProbe);
    await deployStandaloneRecorderLoadSaved();
    deployStandaloneRecorderSetQuickResult(`Canal ${String(channel).padStart(2, '0')} excluido do gravador.`);
    showToast('Canal excluido.');
  } catch (err) {
    const detail = err?.detail || err?.message || 'Falha ao excluir canal.';
    deployStandaloneRecorderSetQuickResult(esc(detail), true);
    showToast(detail, true);
  }
}

function deployStandaloneRecorderOpenWeb() {
  if (!deployStandaloneRecorderRequireLogin()) return;
  window.open(deployStandaloneRecorderWebUrl(), '_blank', 'noopener');
}

function deployStandaloneRecorderPlayback() {
  if (!deployStandaloneRecorderRequireLogin()) return;
  const payload = deployStandaloneRecorderPayload();
  navigateTo('playback');
  setTimeout(() => {
    const host = document.getElementById('playbackHost');
    const user = document.getElementById('playbackUser');
    const pass = document.getElementById('playbackPassword');
    const channel = document.getElementById('playbackChannel');
    if (host) host.value = `${payload.recorder_host}${payload.recorder_http_port && payload.recorder_http_port !== 80 ? `:${payload.recorder_http_port}` : ''}`;
    if (user) user.value = payload.recorder_user;
    if (pass) pass.value = payload.recorder_password;
    if (channel) channel.value = deployStandaloneRecorderFirstUsedChannel();
  }, 60);
}

function deployStandaloneRecorderFicha() {
  if (!deployStandaloneRecorderRequireLogin()) return;
  const payload = deployStandaloneRecorderPayload();
  const channels = deployStandaloneRecorderChannelsFromProbe(_deployStandaloneRecorderProbe);
  const data = {
    site: payload.site,
    type: payload.recorder_type,
    host: payload.recorder_host,
    http_port: payload.recorder_http_port,
    name: payload.name,
    brand: _deployStandaloneRecorderProbe?.brand || '',
    model: _deployStandaloneRecorderProbe?.model || '',
    serial: _deployStandaloneRecorderProbe?.serial || '',
    channel_total: channels.length || payload.recorder_channel_total,
    used_channels: channels.filter(ch => ch.used).length,
    free_channels: channels.filter(ch => !ch.used).length,
    channels,
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `sightops-gravador-${payload.recorder_host || 'novo'}.json`;
  a.click();
  URL.revokeObjectURL(url);
  deployStandaloneRecorderSetQuickResult('Ficha tecnica gerada com os dados da descoberta.');
}

async function deployStandaloneRecorderSetNtp() {
  if (!deployStandaloneRecorderRequireLogin()) return;
  const payload = deployStandaloneRecorderPayload();
  const ntp = document.getElementById('deployStandaloneRecorderNtpServer')?.value.trim() || 'time.cloudflare.com';
  const btn = document.getElementById('btnDeployStandaloneRecorderSetNtp');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Ajustando'; lucide.createIcons(); }
  deployStandaloneRecorderSetQuickResult(`Enviando NTP ${esc(ntp)} para ${esc(payload.recorder_host)}...`);
  try {
    const res = await api(`${deployStandaloneRecorderEndpointBase(payload)}/ntp`, {
      method: 'POST',
      body: JSON.stringify({ ...deployStandaloneRecorderCommonPayload(payload), address: ntp }),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      const detail = data?.detail || data?.message || 'Falha ao acertar NTP.';
      deployStandaloneRecorderSetQuickResult(esc(detail), true);
      showToast(detail, true);
      return;
    }
    deployStandaloneRecorderSetQuickResult(`NTP aplicado em ${esc(payload.recorder_host)}.`);
    showToast('NTP aplicado no gravador.');
  } catch (err) {
    const detail = err?.detail || err?.message || 'Falha ao acertar NTP.';
    deployStandaloneRecorderSetQuickResult(esc(detail), true);
    showToast(detail, true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="clock"></i> Acertar NTP'; lucide.createIcons(); }
    deployStandaloneRecorderUpdateQuickActions();
  }
}

async function deployStandaloneRecorderReboot() {
  if (!deployStandaloneRecorderRequireLogin()) return;
  const payload = deployStandaloneRecorderPayload();
  if (!confirm(`Reiniciar o gravador ${payload.recorder_host}?`)) return;
  const btn = document.getElementById('btnDeployStandaloneRecorderReboot');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Reiniciando'; lucide.createIcons(); }
  deployStandaloneRecorderSetQuickResult(`Enviando reboot para ${esc(payload.recorder_host)}...`);
  try {
    const res = await api(`${deployStandaloneRecorderEndpointBase(payload)}/reboot`, {
      method: 'POST',
      body: JSON.stringify(deployStandaloneRecorderCommonPayload(payload)),
    });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      const detail = data?.detail || data?.message || 'Falha ao reiniciar gravador.';
      deployStandaloneRecorderSetQuickResult(esc(detail), true);
      showToast(detail, true);
      return;
    }
    deployStandaloneRecorderSetQuickResult(`Comando de reboot enviado para ${esc(payload.recorder_host)}.`);
    showToast('Reboot enviado ao gravador.');
  } catch (err) {
    const detail = err?.detail || err?.message || 'Falha ao reiniciar gravador.';
    deployStandaloneRecorderSetQuickResult(esc(detail), true);
    showToast(detail, true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="power"></i> Reboot'; lucide.createIcons(); }
    deployStandaloneRecorderUpdateQuickActions();
  }
}

//  Implantacao - ONU (pagina dedicada: descobrir/autorizar/consultar/excluir)
let _onuSelectedDiscovered = null; // {pon, serno_id, serial, model, vendor}
let _onuLastAddContext = null; // {olt, pon, slot, services, tagMode, terminal} -- p/ "tentar bridge de novo"
let _oltInventoryRows = null; // cache: linhas de /api/olt/rows (IP/site/PON ja conhecidos)

function onuInferOltContext(oltIp) {
  const ip = (oltIp || '').trim();
  const rows = Array.isArray(_oltInventoryRows) ? _oltInventoryRows : [];
  const same = rows.filter(r => (r.olt_ip || '').trim() === ip);
  if (!same.length) return { site: '', olt_name: '' };
  const bySite = same.reduce((acc, r) => {
    const site = (r.site || r.local || '').trim();
    if (!site) return acc;
    acc[site] = (acc[site] || 0) + 1;
    return acc;
  }, {});
  const site = Object.entries(bySite).sort((a, b) => b[1] - a[1])[0]?.[0] || '';
  const oltName = same.map(r => (r.olt_name || '').trim()).find(Boolean) || '';
  return { site, olt_name: oltName };
}

async function populateOltIpDatalist(inputId, listId, ponInputId) {
  const listEl = document.getElementById(listId);
  if (!listEl) return;
  if (!_oltInventoryRows) {
    const data = await apiJson('/api/olt/rows').catch(() => null);
    _oltInventoryRows = data?.rows || (Array.isArray(data) ? data : []) || [];
  }
  const byIp = new Map();
  _oltInventoryRows.forEach(r => {
    const ip = (r.olt_ip || '').trim();
    if (!ip) return;
    if (!byIp.has(ip)) byIp.set(ip, { ip, name: r.olt_name || '', sites: new Set(), pons: new Set() });
    const entry = byIp.get(ip);
    if (r.site) entry.sites.add(r.site);
    if (r.pon) entry.pons.add(String(r.pon));
  });
  listEl.innerHTML = [...byIp.values()].map(e => {
    const label = [e.name, [...e.sites].join('/')].filter(Boolean).join(' - ');
    return `<option value="${esc(e.ip)}">${esc(label)}</option>`;
  }).join('');

  const inputEl = document.getElementById(inputId);
  const ponEl = ponInputId ? document.getElementById(ponInputId) : null;
  if (inputEl && ponEl && !inputEl.dataset.oltAutofillBound) {
    inputEl.dataset.oltAutofillBound = '1';
    inputEl.addEventListener('change', () => {
      const entry = byIp.get(inputEl.value.trim());
      if (entry && entry.pons.size === 1 && !ponEl.value) {
        ponEl.value = [...entry.pons][0];
      }
    });
  }
}

async function refreshOnuConnectors() {
  const sel = document.getElementById('onuConnector');
  if (!sel) return;
  try {
    const data = await apiJson('/api/connectors');
    _connectors = Array.isArray(data?.connectors) ? data.connectors : [];
  } catch {
    _connectors = _connectors || [];
  }
  const current = sel.value;
  const rows = _routerConnectors();
  sel.innerHTML = '<option value="">Escolha a origem de acesso</option><option value="__local__">Local / VPN do servidor</option>' + rows.map(c => {
    const online = _connectorIsOnline(c);
    const tunnel = _connectorHasTunnel(c) ? ' + VPN' : '';
    const disabled = online && _connectorHasTunnel(c) ? '' : 'disabled';
    const suffix = !online ? ' (offline)' : (!_connectorHasTunnel(c) ? ' (sem VPN)' : '');
    return `<option value="${esc(c.id || c.connector_id || '')}" ${disabled}>${esc(_connectorLabel(c))}${tunnel}${suffix}</option>`;
  }).join('');
  if (current === '__local__' || (current && rows.some(c => String(c.id || c.connector_id || '') === current))) {
    sel.value = current;
  }
}

let _onuRegistryRows = [];

const ONU_CAPABILITY_BUTTONS = {
  discover_onus: ['btnOnuDiscover'],
  add_onu: ['btnOnuAdd', 'btnOnuAddVlanRow'],
  onu_signal: ['btnOnuQuery'],
  reboot_onu: ['btnOnuReboot'],
  delete_onu: ['btnOnuDelete', 'confirmOnuDelete'],
};

const ONU_CAPABILITY_STEPS = {
  discover_onus: 'onuStepDiscover',
  add_onu: 'onuStepAdd',
  onu_signal: 'onuStepQuery',
  reboot_onu: 'onuStepReboot',
  delete_onu: 'onuStepDelete',
};

function onuSelectedRegistryRow() {
  const value = document.getElementById('onuOltRegistry')?.value || '';
  return _onuRegistryRows.find(item => String(item.id) === String(value)) || null;
}

function onuCapabilityInfo() {
  const row = onuSelectedRegistryRow();
  if (!row) return null;
  return {
    row,
    caps: row.capabilities || {},
    label: row.capability_label || row.driver || [row.vendor, row.model].filter(Boolean).join(' ') || 'OLT',
    notes: row.capability_notes || '',
  };
}

function onuHasCapability(capability) {
  const info = onuCapabilityInfo();
  return !info || !!info.caps[capability];
}

function onuCapabilityMessage(capability) {
  const info = onuCapabilityInfo();
  if (!info) return '';
  return `${info.label} ainda nao suporta esta acao no SightOps. ${info.notes || ''}`.trim();
}

function onuPonCountForRow(row) {
  const model = String(row?.model || row?.olt_model || '').trim().toLowerCase();
  const driver = String(row?.driver || '').trim().toLowerCase();
  if (driver === 'intelbras_4840e' || model.includes('4840e') || model === '4840') return 4;
  return 8;
}

function onuRenderPonSelectOptions(select, count, includeAll = false) {
  if (!select) return;
  const previous = select.value;
  const total = Number.isInteger(Number(count)) && Number(count) > 0 ? Number(count) : 8;
  const options = [];
  if (includeAll) options.push(`<option value="all">Todas (1-${total})</option>`);
  else options.push('<option value="">Escolha</option>');
  for (let i = 1; i <= total; i += 1) {
    options.push(`<option value="${i}">PON ${i}</option>`);
  }
  select.innerHTML = options.join('');
  if ([...select.options].some(opt => opt.value === previous)) select.value = previous;
  else select.value = includeAll ? 'all' : '';
}

function onuUpdatePonSelectors() {
  const row = onuSelectedRegistryRow();
  const count = onuPonCountForRow(row);
  onuRenderPonSelectOptions(document.getElementById('onuOltPon'), count, true);
  onuRenderPonSelectOptions(document.getElementById('onuQueryPon'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuRebootPon'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuDeletePon'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuQueryPonEpon'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuRebootPonEpon'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuDeletePonEpon'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuAddPonVsol'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuQueryPonVsol'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuRebootPonVsol'), count, false);
  onuRenderPonSelectOptions(document.getElementById('onuDeletePonVsol'), count, false);
}

function onuUpdateCapabilities() {
  const status = document.getElementById('onuCapabilityStatus');
  const info = onuCapabilityInfo();
  if (status) {
    if (!info) {
      status.innerHTML = 'Escolha uma OLT cadastrada para ver quais acoes esse modelo suporta.';
      status.classList.remove('error');
    } else {
      const supported = [
        info.caps.discover_onus ? 'descobrir' : '',
        info.caps.add_onu ? 'autorizar' : '',
        info.caps.onu_signal ? 'consultar sinal/MACs' : '',
        info.caps.reboot_onu ? 'reiniciar' : '',
        info.caps.delete_onu ? 'excluir' : '',
        info.caps.collect_macs ? 'sincronizar inventario' : '',
      ].filter(Boolean).join(', ') || 'nenhuma acao operacional';
      const blocked = [
        !info.caps.discover_onus ? 'descoberta' : '',
        !info.caps.add_onu ? 'autorizacao' : '',
        !info.caps.onu_signal ? 'sinal/MACs' : '',
        !info.caps.reboot_onu ? 'reinicio' : '',
        !info.caps.delete_onu ? 'exclusao' : '',
      ].filter(Boolean).join(', ');
      status.innerHTML = `<b style="color:var(--primary)">${esc(info.label)}</b> -- suporta: ${esc(supported)}.${blocked ? ` Bloqueado: ${esc(blocked)}.` : ''} ${esc(info.notes || '')}`;
      status.classList.toggle('error', !!blocked && !info.caps.add_onu);
    }
  }
  Object.entries(ONU_CAPABILITY_BUTTONS).forEach(([capability, ids]) => {
    const allowed = onuHasCapability(capability);
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.disabled = !allowed;
      el.classList.toggle('is-disabled', !allowed);
      el.title = allowed ? '' : onuCapabilityMessage(capability);
    });
  });
  Object.entries(ONU_CAPABILITY_STEPS).forEach(([capability, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    const allowed = onuHasCapability(capability);
    el.classList.toggle('onu-step-unsupported', !allowed);
    if (!allowed) el.open = false;
  });
}

async function refreshOnuRegistry() {
  const sel = document.getElementById('onuOltRegistry');
  if (!sel) return;
  try {
    const data = await apiJson('/api/olt/registry');
    _onuRegistryRows = (Array.isArray(data?.items) ? data.items : []).filter(row => row?.active);
  } catch {
    _onuRegistryRows = [];
  }
  onuRenderRegistryForOrigin();
}

function onuRenderRegistryForOrigin() {
  const sel = document.getElementById('onuOltRegistry');
  if (!sel) return;
  const origin = document.getElementById('onuConnector')?.value || '';
  const rows = _onuRegistryRows.filter(row => deployOltMatchesOrigin(row, origin));
  sel.disabled = !onuConnectorGateOk();
  sel.innerHTML = !origin ? '<option value="">Escolha primeiro o conector</option>'
    : '<option value="">Escolha uma OLT cadastrada</option>'
    + rows.map(row => {
      const access = row.connector_id ? 'conector' : 'acesso direto';
      return `<option value="${esc(row.id)}">${esc(row.name)} - ${esc(row.site || 'sem site')} - ${esc(row.host)} (${access})</option>`;
    }).join('')
    + (origin === '__local__' ? '<option value="__manual__">Acesso manual / instalacao local</option>' : '');
}

function onuApplyRegisteredOlt() {
  const value = document.getElementById('onuOltRegistry')?.value || '';
  const row = onuSelectedRegistryRow();
  const manual = value === '__manual__';
  const setValue = (id, value) => { const el = document.getElementById(id); if (el) el.value = value || ''; };
  if (row) {
    setValue('onuOltIp', row.host);
    setValue('onuOltUser', row.username || 'admin');
    setValue('onuOltPassword', '');
  } else if (!manual) {
    setValue('onuOltIp', '');
    setValue('onuOltUser', '');
    setValue('onuOltPassword', '');
  }
  ['onuOltIp', 'onuOltUser', 'onuOltPassword'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = !!row;
  });
  const password = document.getElementById('onuOltPassword');
  if (password) password.placeholder = row ? 'Credencial salva no servidor' : 'Senha';
  onuUpdatePonSelectors();
  onuUpdateServiceOptions();
  onuToggleDriverFields('onuAddFieldsGpon', 'onuAddFieldsEpon', 'onuAddFieldsVsol');
  onuToggleDriverFields(null, 'onuAddHintEpon', null);
  onuPlaceInlineButton('btnOnuQuery', 'onuQueryBtnWrapGpon', 'onuQueryBtnSlotEpon', 'onuQueryBtnSlotVsol',
    onuToggleDriverFields('onuQueryFieldsGpon', 'onuQueryFieldsEpon', 'onuQueryFieldsVsol'));
  onuPlaceInlineButton('btnOnuReboot', 'onuRebootBtnWrapGpon', 'onuRebootBtnSlotEpon', 'onuRebootBtnSlotVsol',
    onuToggleDriverFields('onuRebootFieldsGpon', 'onuRebootFieldsEpon', 'onuRebootFieldsVsol'));
  onuPlaceInlineButton('btnOnuDelete', 'onuDeleteBtnWrapGpon', 'onuDeleteBtnSlotEpon', 'onuDeleteBtnSlotVsol',
    onuToggleDriverFields('onuDeleteFieldsGpon', 'onuDeleteFieldsEpon', 'onuDeleteFieldsVsol'));
  onuToggleDriverFields('onuDiscoverResult', 'onuDiscoverResultEpon', null);
  updateOnuConnectorStatus();
  onuUpdateCapabilities();
  onuUpdateStepsLock();
}

function updateOnuConnectorStatus() {
  const status = document.getElementById('onuConnectorStatus');
  const connectorId = document.getElementById('onuConnector')?.value || '';
  const connector = connectorId ? _connectorById(connectorId) : null;
  if (!status) return;
  if (!connectorId) {
    status.innerHTML = 'Escolha Local/VPN do servidor ou um conector online com VPN. Os dados da OLT ficam bloqueados ate definir a origem.';
    status.classList.add('error');
    onuUpdateConnectorGate();
    return;
  }
  if (connectorId === '__local__') {
    status.innerHTML = '<b style="color:var(--primary)">Modo local</b> -- usando o servidor/VPN ja roteada. Confirme manualmente que a OLT pertence ao site correto.';
    status.classList.remove('error');
    onuUpdateConnectorGate();
    return;
  }
  const online = _connectorIsOnline(connector);
  const tunnel = _connectorHasTunnel(connector);
  status.innerHTML = online
    ? `<b style="color:var(--primary)">Conector online</b> -- ${esc(_connectorLabel(connector))}${tunnel ? ' -- VPN ativa para acessar a OLT.' : ' -- sem VPN configurada: a acao sera bloqueada.'}`
    : `<b style="color:var(--danger)">Conector offline</b> -- ${esc(_connectorLabel(connector) || 'conector indisponivel')}.`;
  status.classList.toggle('error', !online || !tunnel);
  onuUpdateConnectorGate();
}

function onuConnectorGateOk() {
  const connectorId = document.getElementById('onuConnector')?.value || '';
  if (!connectorId) return false;
  if (connectorId === '__local__') return true;
  const connector = _connectorById(connectorId);
  return _connectorIsOnline(connector) && _connectorHasTunnel(connector);
}

function onuUpdateConnectorGate() {
  const unlocked = onuConnectorGateOk();
  const registered = /^\d+$/.test(document.getElementById('onuOltRegistry')?.value || '');
  document.querySelectorAll('[data-onu-needs-connector="1"]').forEach(el => {
    const registeredCredential = registered && ['onuOltIp', 'onuOltUser', 'onuOltPassword'].includes(el.id);
    el.disabled = !unlocked || registeredCredential;
    el.classList.toggle('is-disabled', !unlocked);
  });
  const registry = document.getElementById('onuOltRegistry');
  if (registry) registry.disabled = !unlocked;
  onuUpdateCapabilities();
  onuUpdateStepsLock();
}

function onuConnectorReady(olt) {
  if (olt.access_mode === 'local') return true;
  if (!olt.connector_id) {
    showToast('Escolha Local/VPN do servidor ou um conector antes de acessar a OLT.', true);
    updateOnuConnectorStatus();
    return false;
  }
  const connector = _connectorById(olt.connector_id);
  if (!_connectorIsOnline(connector)) {
    showToast('O conector selecionado esta offline.', true);
    updateOnuConnectorStatus();
    return false;
  }
  if (!_connectorHasTunnel(connector)) {
    showToast('Esse conector ainda nao tem VPN ativa para acessar a OLT.', true);
    updateOnuConnectorStatus();
    return false;
  }
  return true;
}

// Acordeao generico ("sanfonado"): dentro de containerSelector, so um
// <details class="onu-step"> fica aberto por vez (fallback via JS para
// navegadores sem suporte nativo a details[name]).
function bindAccordionExclusive(containerSelector) {
  document.querySelectorAll(`${containerSelector} details.onu-step`).forEach(d => {
    if (d.dataset.accordionBound) return;
    d.dataset.accordionBound = '1';
    d.addEventListener('toggle', () => {
      if (!d.open) return;
      document.querySelectorAll(`${containerSelector} details.onu-step`).forEach(other => {
        if (other !== d) other.open = false;
      });
    });
  });
}

function onuResetTransientResults() {
  // A tela nunca e recriada ao navegar (navigateTo so esconde/mostra com
  // CSS) -- sem isso, resultado de consulta/exclusao/etc de uma sessao
  // anterior, e ate a sanfona aberta, ficavam grudados indefinidamente ate
  // dar F5. Roda toda vez que a tela e aberta, sem mexer na OLT/conector
  // selecionados (isso o usuario quer manter).
  _onuSelectedDiscovered = null;
  _onuLastAddContext = null;
  _onuDeleteTarget = null;
  onuSetResult('onuDiscoverResult', 'Informe IP/PON/usuario/senha da OLT e clique em Descobrir.');
  onuSetResult('onuAddResult', 'Nenhuma ONU autorizada ainda nesta sessao.');
  onuSetResult('onuQueryResult', 'Informe a posicao (PON + numero) ou o serial e clique em Consultar.');
  onuSetResult('onuRebootResult', 'Nenhum reinicio realizado nesta sessao.');
  onuSetResult('onuDeleteResult', 'Nenhuma exclusao realizada nesta sessao.');
  document.querySelectorAll('#viewDeployOnu details.onu-step').forEach(d => { d.open = false; });
  loadOnuHistory();
}

function loadDeployOnu() {
  onuResetTransientResults();
  deploymentApplyPreferredInventoryMode();
  populateOltIpDatalist('onuOltIp', 'onuOltIpList', 'onuOltPon');
  Promise.all([refreshOnuConnectors(), refreshOnuRegistry()]).finally(() => {
    onuRenderRegistryForOrigin();
    onuApplyRegisteredOlt();
    updateOnuConnectorStatus();
    onuUpdateConnectorGate();
  });
  bindAccordionExclusive('#viewDeployOnu');
  bindOnuStepLockGuards();
  onuUpdateConnectorGate();
  ['onuOltIp', 'onuOltUser', 'onuOltPassword'].forEach(id => {
    const el = document.getElementById(id);
    if (el && !el.dataset.lockWatchBound) {
      el.dataset.lockWatchBound = '1';
      el.addEventListener('input', () => {
        onuUpdateStepsLock();
      });
    }
  });
  const connectorEl = document.getElementById('onuConnector');
  if (connectorEl && !connectorEl.dataset.onuConnectorBound) {
    connectorEl.dataset.onuConnectorBound = '1';
    connectorEl.addEventListener('change', () => {
      onuRenderRegistryForOrigin();
      onuApplyRegisteredOlt();
      updateOnuConnectorStatus();
      onuUpdateConnectorGate();
    });
  }
  const registryEl = document.getElementById('onuOltRegistry');
  if (registryEl && !registryEl.dataset.onuRegistryBound) {
    registryEl.dataset.onuRegistryBound = '1';
    registryEl.addEventListener('change', () => {
      onuApplyRegisteredOlt();
      loadOnuHistory();
    });
  }
  onuUpdateTerminalUI();
  loadOnuHistory();
  lucide.createIcons();
}

function onuAccordionOpen(stepId) {
  const el = document.getElementById(stepId);
  if (el && !el.open) el.open = true;
}

// Etapas abaixo da conexao ficam travadas ate IP + senha da OLT serem preenchidos.
function onuLockedStepIds() {
  return ['onuStepDiscover', 'onuStepAdd', 'onuStepQuery', 'onuStepReboot', 'onuStepDelete'];
}

function onuUpdateStepsLock() {
  const registryValue = document.getElementById('onuOltRegistry')?.value || '';
  const registered = /^\d+$/.test(registryValue);
  const ip = document.getElementById('onuOltIp')?.value.trim();
  const pass = document.getElementById('onuOltPassword')?.value;
  const connectorOk = onuConnectorGateOk();
  const locked = !connectorOk || (registered ? !ip : (!ip || !pass));
  onuLockedStepIds().forEach(id => {
    const details = document.getElementById(id);
    if (!details) return;
    details.classList.toggle('onu-step-locked', locked);
    if (locked) details.open = false;
  });
}

function bindOnuStepLockGuards() {
  onuLockedStepIds().forEach(id => {
    const details = document.getElementById(id);
    const summary = details?.querySelector('summary');
    if (!summary || summary.dataset.lockGuardBound) return;
    summary.dataset.lockGuardBound = '1';
    summary.addEventListener('click', (e) => {
      if (details.classList.contains('onu-step-locked')) {
        e.preventDefault();
        showToast(onuConnectorGateOk() ? 'Informe o IP e a senha da OLT primeiro.' : 'Escolha Local/VPN do servidor ou um conector online com VPN primeiro.', true);
      } else if (details.classList.contains('onu-step-unsupported')) {
        e.preventDefault();
        const cap = Object.entries(ONU_CAPABILITY_STEPS).find(([, stepId]) => stepId === id)?.[0] || '';
        showToast(onuCapabilityMessage(cap) || 'Esta OLT ainda nao suporta essa acao.', true);
      }
    });
  });
}

// Etapa "Autorizar" -- ONT permite mais de uma VLAN, ONU fica com uma so.
const ONU_SERVICE_OPTIONS_DEFAULT = `
  <option value="downlink">Internet / dados</option>
  <option value="iptv">IPTV / multicast</option>
  <option value="tls">TLS / transparente</option>
  <option value="management">Gerencia</option>
  <option value="voice">Voz</option>
  <option value="other">Outro servico</option>
`;

// A 8820i so aceita 'downlink' ou 'tls' no 'bridge add' (confirmado contra
// OLT real com 'bridge add gpon <pon> onu <onu> ?') -- as outras opcoes do
// dropdown generico (iptv/management/voice/other) nem existem nesse CLI.
const ONU_SERVICE_OPTIONS_8820I = `
  <option value="downlink">Downlink</option>
  <option value="tls">TLS / transparente</option>
`;

function onuIsEpon(row) {
  return String(row?.driver || '').trim().toLowerCase() === 'intelbras_4840e';
}

function onuIsVsol(row) {
  return String(row?.driver || '').trim().toLowerCase() === 'vsol_epon';
}

function onuActiveOltKind(row) {
  if (onuIsEpon(row)) return 'epon';
  if (onuIsVsol(row)) return 'vsol';
  return 'gpon';
}

function onuToggleDriverFields(gponId, eponId, vsolId) {
  const kind = onuActiveOltKind(onuSelectedRegistryRow());
  const gponEl = gponId ? document.getElementById(gponId) : null;
  const eponEl = eponId ? document.getElementById(eponId) : null;
  const vsolEl = vsolId ? document.getElementById(vsolId) : null;
  if (gponEl) gponEl.classList.toggle('hidden', kind !== 'gpon');
  if (eponEl) eponEl.classList.toggle('hidden', kind !== 'epon');
  if (vsolEl) vsolEl.classList.toggle('hidden', kind !== 'vsol');
  return kind;
}

function onuPlaceInlineButton(buttonId, gponWrapId, eponRowId, vsolRowId, kind) {
  const btn = document.getElementById(buttonId);
  const targetId = kind === 'epon' ? eponRowId : (kind === 'vsol' ? vsolRowId : gponWrapId);
  const target = document.getElementById(targetId);
  if (btn && target && btn.parentElement !== target) target.appendChild(btn);
  document.getElementById(gponWrapId)?.classList.toggle('hidden', kind !== 'gpon');
}

function onuServiceOptionsHtmlForDriver(driver) {
  return String(driver || '').trim().toLowerCase() === 'intelbras_8820i'
    ? ONU_SERVICE_OPTIONS_8820I
    : ONU_SERVICE_OPTIONS_DEFAULT;
}

function onuUpdateServiceOptions() {
  const row = onuSelectedRegistryRow();
  const optionsHtml = onuServiceOptionsHtmlForDriver(row?.driver);
  document.querySelectorAll('#onuAddServiceRows .onuAddServiceSel').forEach(sel => {
    const previous = sel.value;
    sel.innerHTML = optionsHtml;
    if ([...sel.options].some(opt => opt.value === previous)) sel.value = previous;
  });
}

function onuServiceRowHtml() {
  const row = onuSelectedRegistryRow();
  return `
    <div class="form-row onu-service-row">
      <div class="form-group">
        <label>Servico</label>
        <select class="onuAddServiceSel">
          ${onuServiceOptionsHtmlForDriver(row?.driver)}
        </select>
      </div>
      <div class="form-group">
        <label>VLAN</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input class="onuAddVlanInput" type="number" placeholder="Ex: 3000" style="flex:1">
          <button type="button" class="icon-button onu-service-row-remove" title="Remover VLAN"><i data-lucide="x"></i></button>
        </div>
      </div>
    </div>`;
}

function onuAddVlanRow() {
  const container = document.getElementById('onuAddServiceRows');
  if (!container) return;
  container.insertAdjacentHTML('beforeend', onuServiceRowHtml());
  lucide.createIcons();
  container.querySelectorAll('.onu-service-row-remove').forEach(btn => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => btn.closest('.onu-service-row')?.remove());
  });
}

function onuPortRowEponHtml() {
  return `
    <div class="onu-service-row-epon form-row">
      <div class="form-group">
        <label>Porta ethernet
          <input type="number" min="1" value="1" class="onu-eth-port-epon">
        </label>
      </div>
      <div class="form-group">
        <label>VLAN
          <div style="display:flex;gap:6px;align-items:center">
            <input type="number" min="1" class="onu-eth-vlan-epon" style="flex:1">
            <button type="button" class="icon-button onu-service-row-epon-remove" title="Remover porta"><i data-lucide="x"></i></button>
          </div>
        </label>
      </div>
    </div>`;
}

function onuAddPortRowEpon() {
  const container = document.getElementById('onuAddPortRowsEpon');
  if (!container) return;
  container.insertAdjacentHTML('beforeend', onuPortRowEponHtml());
  lucide.createIcons();
  container.querySelectorAll('.onu-service-row-epon-remove').forEach(btn => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => btn.closest('.onu-service-row-epon')?.remove());
  });
}

function onuUpdateTerminalUI() {
  const wrap = document.getElementById('onuAddVlanAddWrap');
  if (wrap) wrap.style.display = '';
}

function onuOltPayload() {
  const registryValue = document.getElementById('onuOltRegistry')?.value || '';
  const registered = _onuRegistryRows.find(item => String(item.id) === String(registryValue));
  const oltIp = document.getElementById('onuOltIp')?.value.trim() || '';
  const ctx = onuInferOltContext(oltIp);
  const selectedOrigin = document.getElementById('onuConnector')?.value || '';
  const localMode = selectedOrigin === '__local__';
  const connectorId = localMode ? '' : selectedOrigin;
  const connector = connectorId ? _connectorById(connectorId) : null;
  const site = connector ? (connector.site || connector.client || ctx.site || '') : ctx.site;
  return {
    olt_id: registered ? Number(registered.id) : null,
    access_mode: localMode ? 'local' : 'connector',
    olt_ip: oltIp,
    user: document.getElementById('onuOltUser')?.value.trim() || 'admin',
    password: document.getElementById('onuOltPassword')?.value || '',
    pon: document.getElementById('onuOltPon')?.value.trim() || 'all',
    site: registered?.site || site,
    olt_name: registered?.name || ctx.olt_name,
    connector_id: connectorId,
    remote_connector_id: connectorId,
    connector_name: connector ? (connector.name || connector.client || '') : '',
    inventory_mode: document.getElementById('onuInventoryMode')?.value || 'basic',
  };
}

// PON especifica (1-8) escolhida na conexao, ou 0 se estiver em "Todas".
function onuOltPonNumber(olt) {
  const n = Number(olt.pon);
  return Number.isInteger(n) && n >= 1 && n <= 8 ? n : 0;
}

function onuSetResult(boxId, html, isError = false) {
  const box = document.getElementById(boxId);
  if (!box) return;
  box.innerHTML = html;
  box.classList.toggle('error', !!isError);
}

// Contador de segundos "ainda trabalhando" para as chamadas na OLT (7-20s+
// cada, sem log em tempo real possivel) -- mesma ideia ja usada no Coletar
// MACs do Inventario > OLT: nao inventa progresso falso, so mostra que o
// pedido continua vivo enquanto espera.
function onuStartTicker(boxId, baseText) {
  let tick = 0;
  const paint = () => {
    const el = document.getElementById(boxId);
    if (el) el.textContent = `${baseText}... (${tick}s)`;
  };
  paint();
  return setInterval(() => { tick += 1; paint(); }, 1000);
}

function onuStopTicker(timer) {
  if (timer) clearInterval(timer);
}

function onuMacLine(m) {
  const ip = m?.ip ? ` - <b>${esc(m.ip)}</b>` : '';
  return `<li><code>${esc(m?.mac || '')}</code>${ip} - ${esc(m?.interface || '')}</li>`;
}

function onuVlanSummaryFromMacs(macs) {
  const vlans = [];
  (macs || []).forEach(m => {
    const text = String(m?.interface || m?.vlan || '');
    const match = text.match(/vlan\s+(\d+)/i);
    if (match && !vlans.includes(match[1])) vlans.push(match[1]);
  });
  return vlans.join(',');
}

function onuHistoryDate(value) {
  if (!value) return 'data nao informada';
  try { return new Date(value).toLocaleString('pt-BR'); } catch { return String(value); }
}

const ONU_ACTION_DONE = {
  add_onu: 'autorizada',
  add_onu_bridge: 'com o servico/VLAN reaplicado',
  reboot_onu: 'reiniciada',
  delete_onu: 'excluida',
  onu_signal: 'consultada',
};
const ONU_ACTION_VERB = {
  add_onu: 'autorizar',
  add_onu_bridge: 'aplicar o servico/VLAN',
  reboot_onu: 'reiniciar',
  delete_onu: 'excluir',
  onu_signal: 'consultar',
};

async function loadOnuHistory() {
  const box = document.getElementById('onuHistory');
  if (!box) return;
  const selectedRegistryId = document.getElementById('onuOltRegistry')?.value || '';
  const selectedOlt = _onuRegistryRows.find(item => String(item.id) === String(selectedRegistryId));
  const manualMode = selectedRegistryId === '__manual__';
  const manualIp = manualMode ? (document.getElementById('onuOltIp')?.value.trim() || '') : '';
  if (!selectedOlt && !manualIp) {
    box.innerHTML = '<div class="deployment-history-empty">Escolha uma OLT para ver o historico dela.</div>';
    return;
  }
  box.innerHTML = '<div class="deployment-history-empty">Atualizando historico...</div>';
  try {
    const selectedIp = String(selectedOlt?.host || manualIp).trim();
    const data = await apiJson(`/api/olt/onu-actions?olt_ip=${encodeURIComponent(selectedIp)}&limit=30`);
    const actions = Array.isArray(data?.actions) ? data.actions : [];
    if (!actions.length) {
      box.innerHTML = '<div class="deployment-history-empty">Nenhuma acao registrada ainda nesta OLT.</div>';
      return;
    }
    box.innerHTML = actions.map(a => {
      const ok = a.ok !== 0 && a.ok !== false;
      const serial = a.serial ? ` (${esc(a.serial)})` : '';
      const vlan = a.vlan ? ` - VLAN ${esc(a.vlan)}` : '';
      const detail = a.detail ? ` -- ${esc(a.detail)}` : '';
      const text = ok
        ? `foi ${ONU_ACTION_DONE[a.action] || esc(a.action)}`
        : `FALHOU ao ${ONU_ACTION_VERB[a.action] || esc(a.action)}`;
      return `<div class="deployment-history-item${ok ? '' : ' error'}">
        <b>PON ${esc(a.pon)} / ONU ${esc(a.onu)}${serial}</b>
        <span>${text}${vlan}${detail}</span>
        <small>${esc(a.site || a.olt_name || 'sem site')} - ${esc(onuHistoryDate(a.created_at))}</small>
      </div>`;
    }).join('');
  } catch (err) {
    box.innerHTML = `<div class="deployment-history-empty">Falha ao carregar: ${esc(err?.message || err)}</div>`;
  }
}

function onuClear() {
  document.querySelectorAll('#viewDeployOnu .onu-accordion input').forEach(input => { input.value = ''; });
  document.querySelectorAll('#viewDeployOnu .onu-accordion select').forEach(select => { select.selectedIndex = 0; });
  document.querySelectorAll('#onuAddServiceRows .onu-service-row').forEach((row, index) => { if (index > 0) row.remove(); });
  const connector = document.getElementById('onuConnector');
  if (connector) connector.value = '';
  onuRenderRegistryForOrigin();
  onuApplyRegisteredOlt();
  deploymentApplyPreferredInventoryMode();
  onuUpdateTerminalUI();
  onuResetTransientResults();
  updateOnuConnectorStatus();
  onuUpdateConnectorGate();
  onuAccordionOpen('onuStepConn');
  showToast('Campos da implantacao ONU limpos. O historico foi mantido.');
}

async function onuDiscoverEpon(olt) {
  const ticker = onuStartTicker('onuDiscoverResultEpon', 'Consultando OLT');
  const res = await api('/api/olt/discover-onus', { method: 'POST', body: JSON.stringify(olt) });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuDiscoverResultEpon', esc(data?.detail || 'Falha ao consultar a OLT.'), true);
    return;
  }
  const pons = data.pons || {};
  const allDiscovered = [];
  Object.keys(pons).forEach(k => (pons[k]?.discovered || []).forEach(d => allDiscovered.push(d)));
  if (!allDiscovered.length) {
    onuSetResult('onuDiscoverResultEpon', 'Nenhuma ONU nao autorizada encontrada.');
    return;
  }
  onuSetResult('onuDiscoverResultEpon', allDiscovered.map(d => `
    <div class="deploy-match deploy-onu-pick-epon" data-mac="${esc(d.mac)}" data-pon="${esc(d.pon)}" style="cursor:pointer">
      <b>${esc(d.mac)}</b>
      <span>PON ${esc(d.pon)}</span>
      <small>clique para selecionar</small>
    </div>
  `).join(''));
  document.querySelectorAll('#onuDiscoverResultEpon .deploy-onu-pick-epon').forEach(el => {
    el.addEventListener('click', () => {
      const macEl = document.getElementById('onuAddMacEpon');
      const ponEl = document.getElementById('onuOltPon');
      if (macEl) macEl.value = el.dataset.mac;
      if (ponEl) ponEl.value = el.dataset.pon || '';
      showToast(`ONU ${el.dataset.mac} selecionada (PON ${el.dataset.pon}).`);
      onuAccordionOpen('onuStepAdd');
    });
  });
}

async function onuDiscover() {
  if (!onuHasCapability('discover_onus')) { showToast(onuCapabilityMessage('discover_onus'), true); return; }
  const olt = onuOltPayload();
  if (!olt.olt_ip) { showToast('Informe o IP da OLT.', true); return; }
  if (!olt.olt_id && !olt.password) { showToast('Informe a senha da OLT.', true); return; }
  if (!onuConnectorReady(olt)) return;
  if (onuIsEpon(onuSelectedRegistryRow())) { return onuDiscoverEpon(olt); }
  const ticker = onuStartTicker('onuDiscoverResult', 'Consultando OLT');
  const res = await api('/api/olt/discover-onus', { method: 'POST', body: JSON.stringify(olt) });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuDiscoverResult', esc(data?.detail || 'Falha ao consultar a OLT.'), true);
    return;
  }
  const pons = data.pons || {};
  const ponKeys = Object.keys(pons).sort((a, b) => Number(a) - Number(b));
  const allDiscovered = [];
  const freeSummary = [];
  ponKeys.forEach(k => {
    const p = pons[k] || {};
    (p.discovered || []).forEach(d => allDiscovered.push(d));
    freeSummary.push(`PON ${k}: ${(p.free_slots || []).length} livres`);
  });
  if (!allDiscovered.length) {
    onuSetResult('onuDiscoverResult', `Nenhuma ONU nao autorizada encontrada. ${freeSummary.join(' | ')}`);
    return;
  }
  onuSetResult('onuDiscoverResult', allDiscovered.map(d => `
    <div class="deploy-match deploy-onu-pick" data-pon="${esc(d.pon)}" data-serno="${esc(d.serno_id)}" data-serial="${esc(d.serial)}" data-serial-raw="${esc(d.serial_raw || d.serial)}" data-model="${esc(d.model)}" data-vendor="${esc(d.vendor)}" style="cursor:pointer">
      <b>${esc(d.serial)}</b>
      <span>PON ${esc(d.pon)} - ${esc(d.vendor)} ${esc(d.model)}</span>
      <small>descoberta ${esc(d.time_discovered || '')} - clique para selecionar</small>
    </div>
  `).join(''));
  document.querySelectorAll('#onuDiscoverResult .deploy-onu-pick').forEach(el => {
    el.addEventListener('click', () => {
      _onuSelectedDiscovered = {
        pon: Number(el.dataset.pon),
        serno_id: Number(el.dataset.serno),
        serial: el.dataset.serial,
        serialRaw: el.dataset.serialRaw || el.dataset.serial,
        model: el.dataset.model,
        vendor: el.dataset.vendor,
      };
      const sernoEl = document.getElementById('onuAddSernoId');
      const modelEl = document.getElementById('onuAddModel');
      const queryPonEl = document.getElementById('onuQueryPon');
      if (sernoEl) sernoEl.value = el.dataset.serno;
      if (modelEl) modelEl.value = `${el.dataset.vendor} ${el.dataset.model}`;
      if (queryPonEl) queryPonEl.value = el.dataset.pon || '';
      showToast(`ONU ${el.dataset.serial} selecionada (PON ${el.dataset.pon}).`);
      onuAccordionOpen('onuStepAdd');
    });
  });
}

async function onuAddEpon(olt) {
  const mac = document.getElementById('onuAddMacEpon')?.value.trim() || '';
  if (!mac) { showToast('Informe o MAC da ONU.', true); return; }
  const description = document.getElementById('onuAddDescriptionEpon')?.value.trim() || '';
  const rows = [...document.querySelectorAll('#onuAddPortRowsEpon .onu-service-row-epon')];
  const services = rows.map(row => ({
    port: Number(row.querySelector('.onu-eth-port-epon')?.value || 1),
    vlan: Number(row.querySelector('.onu-eth-vlan-epon')?.value || 0),
  })).filter(s => s.vlan > 0);
  const ponSelect = document.getElementById('onuOltPon');
  const pon = Number(ponSelect?.value || '0');
  if (!pon) { showToast('Escolha a PON.', true); return; }

  const ticker = onuStartTicker('onuAddResult', 'Autorizando ONU na OLT');
  const res = await api('/api/olt/add-onu', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, serno_id: 0, serial: mac, description,
      vlan: services[0]?.vlan || 0,
      services: services.map(s => ({ service: 'downlink', vlan: s.vlan, port: s.port })),
      site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuAddResult', esc(data?.error || data?.detail || 'Falha ao autorizar ONU.'), true);
    return;
  }
  onuSetResult('onuAddResult', `ONU autorizada: PON ${esc(pon)} / posicao ${esc(data.onu)} (MAC ${esc(mac)}).`);
  showToast('ONU autorizada na OLT.');
  loadOnuHistory();
}

async function onuAddVsol(olt) {
  const pon = Number(document.getElementById('onuAddPonVsol')?.value || '0');
  const mac = document.getElementById('onuAddMacVsol')?.value.trim() || '';
  if (!pon || !mac) { showToast('Informe PON e MAC da ONU.', true); return; }

  const ticker = onuStartTicker('onuAddResult', 'Autorizando ONU na OLT');
  const res = await api('/api/olt/add-onu', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, serno_id: 0, vlan: 0, serial: mac, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuAddResult', esc(data?.error || 'Falha ao autorizar ONU.'), true);
    return;
  }
  loadOnuHistory();
  const posicao = data.pending
    ? 'aguardando a OLT registrar a posicao (atualize o historico em alguns segundos)'
    : `posicao atribuida: ONU ${esc(data.onu_id)}`;
  onuSetResult('onuAddResult', `
    <div><b>PON ${esc(pon)}</b> - MAC ${esc(mac)} autorizado</div>
    <div style="margin-top:4px">${posicao}</div>
  `);
}

async function onuAdd() {
  if (!onuHasCapability('add_onu')) { showToast(onuCapabilityMessage('add_onu'), true); return; }
  const olt = onuOltPayload();
  if (onuIsVsol(onuSelectedRegistryRow())) { return onuAddVsol(olt); }
  if (onuIsEpon(onuSelectedRegistryRow())) { return onuAddEpon(olt); }
  if (!olt.olt_ip || (!olt.olt_id && !olt.password)) { showToast('Escolha uma OLT cadastrada ou informe IP e senha.', true); return; }
  if (!onuConnectorReady(olt)) return;
  const sernoId = Number(document.getElementById('onuAddSernoId')?.value.trim() || '0');
  if (!sernoId) { showToast('Descubra e selecione uma ONU primeiro (ou digite o serno_id).', true); return; }

  let pon = 0;
  let model = '';
  if (_onuSelectedDiscovered && _onuSelectedDiscovered.serno_id === sernoId) {
    pon = _onuSelectedDiscovered.pon;
    model = _onuSelectedDiscovered.model;
  } else {
    pon = onuOltPonNumber(olt);
  }
  if (!pon) { showToast('Escolha uma PON especifica (nao "Todas") na conexao, ou descubra e selecione uma ONU.', true); return; }

  const tagMode = document.getElementById('onuAddTagMode')?.value || 'tagged';
  const terminal = document.getElementById('onuAddTerminal')?.value || 'onu';

  const rows = [...document.querySelectorAll('#onuAddServiceRows .onu-service-row')];
  const services = rows.map(row => ({
    service: row.querySelector('.onuAddServiceSel')?.value || 'downlink',
    vlan: Number(row.querySelector('.onuAddVlanInput')?.value.trim() || '0'),
  })).filter(e => e.vlan > 0);
  if (!services.length) { showToast('Informe pelo menos uma VLAN.', true); return; }

  const payload = {
    olt_id: olt.olt_id || null,
    olt_ip: olt.olt_ip,
    user: olt.user,
    password: olt.password,
    pon,
    serno_id: sernoId,
    onu_model: model,
    serial: _onuSelectedDiscovered?.serial || '',
    serial_raw: _onuSelectedDiscovered?.serialRaw || _onuSelectedDiscovered?.serial || '',
    vendor: _onuSelectedDiscovered?.vendor || '',
    site: olt.site || '',
    olt_name: olt.olt_name || '',
    description: document.getElementById('onuAddDescription')?.value.trim() || '',
    service: services[0].service,
    vlan: services[0].vlan,
    services,
    tag_mode: tagMode,
    terminal,
    connector_id: olt.connector_id || '',
    remote_connector_id: olt.remote_connector_id || '',
    connector_name: olt.connector_name || '',
  };
  const ticker = onuStartTicker('onuAddResult', 'Autorizando ONU na OLT (equipamento vivo)');
  const res = await api('/api/olt/add-onu', { method: 'POST', body: JSON.stringify(payload) });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok) {
    onuSetResult('onuAddResult', esc(data?.detail || 'Falha ao autorizar ONU.'), true);
    return;
  }
  if (data.ok === false) {
    // A ONU pode ja ter sido autorizada na OLT mesmo com o resto falhando
    // (o 'onu set' e o primeiro passo) -- se `slot` veio preenchido, so o
    // servico/VLAN falhou, e da pra tentar de novo SO essa parte (sem
    // reautorizar) em vez de o tecnico ter que ir na OLT direto.
    const canRetryBridge = Number.isInteger(data.slot);
    _onuLastAddContext = canRetryBridge
      ? { olt, pon: data.pon || pon, slot: data.slot, services, tagMode, terminal }
      : null;
    const retryNote = canRetryBridge
      ? '<br><small>A ONU ja foi autorizada na OLT -- so falta o servico/VLAN.</small>'
      : '';
    const retryBtn = canRetryBridge
      ? '<div class="deploy-inline-button" style="margin-top:8px"><button class="secondary-action" id="btnOnuRetryBridge" type="button"><i data-lucide="refresh-cw"></i> Tentar aplicar servico/VLAN de novo</button></div>'
      : '';
    onuSetResult('onuAddResult', `Falhou em: <code>${esc(data.failed_at || '-')}</code><br>${esc(data.error || '')}<br>Comandos ja aplicados: ${data.commands_run?.length || 0}${retryNote}${retryBtn}`, true);
    if (canRetryBridge) {
      lucide.createIcons();
      document.getElementById('btnOnuRetryBridge')?.addEventListener('click', onuRetryBridge);
    }
    showToast('Falha ao autorizar ONU -- confira o detalhe.', true);
    return;
  }
  _oltInventoryRows = null;
  const syncedMacs = Number(data.device_sync?.macs || 0);
  const invMsg = syncedMacs > 0
    ? ` Inventario atualizado com ${syncedMacs} dispositivo${syncedMacs !== 1 ? 's' : ''} encontrado${syncedMacs !== 1 ? 's' : ''} atras da ONU.`
    : (data.inventory?.updated ? ' ONU adicionada ao inventario; nenhum dispositivo foi aprendido ainda.' : ' Inventario OLT nao foi alterado.');
  onuSetResult('onuAddResult', `ONU autorizada na PON ${esc(data.pon)}, posicao ${esc(data.slot)}.${esc(invMsg)}`);
  showToast(syncedMacs > 0
    ? `ONU autorizada e ${syncedMacs} dispositivo${syncedMacs !== 1 ? 's' : ''} sincronizado${syncedMacs !== 1 ? 's' : ''}.`
    : 'ONU autorizada. Ainda nao havia MAC aprendido; use Consultar sinal / MACs para atualizar.');
  const targetEl = document.getElementById('onuTargetNum');
  if (targetEl) targetEl.value = data.slot;
  const queryPonEl = document.getElementById('onuQueryPon');
  if (queryPonEl) queryPonEl.value = String(data.pon || pon || '');
  loadOnuHistory();
  onuAccordionOpen('onuStepQuery');
}

async function onuRetryBridge() {
  if (!_onuLastAddContext) return;
  const { olt, pon, slot, services, tagMode, terminal } = _onuLastAddContext;
  const payload = {
    olt_id: olt.olt_id || null,
    olt_ip: olt.olt_ip,
    user: olt.user,
    password: olt.password,
    pon,
    onu: slot,
    site: olt.site || '',
    olt_name: olt.olt_name || '',
    service: services[0].service,
    vlan: services[0].vlan,
    services,
    tag_mode: tagMode,
    terminal,
    connector_id: olt.connector_id || '',
    remote_connector_id: olt.remote_connector_id || '',
    connector_name: olt.connector_name || '',
  };
  const ticker = onuStartTicker('onuAddResult', 'Tentando aplicar servico/VLAN de novo (equipamento vivo)');
  const res = await api('/api/olt/add-onu-bridge', { method: 'POST', body: JSON.stringify(payload) });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuAddResult', `Falhou de novo em: <code>${esc(data?.failed_at || '-')}</code><br>${esc(data?.detail || data?.error || 'Falha ao aplicar servico/VLAN.')}`, true);
    showToast('Ainda nao consegui aplicar o servico/VLAN.', true);
    return;
  }
  _onuLastAddContext = null;
  _oltInventoryRows = null;
  onuSetResult('onuAddResult', `Servico/VLAN aplicado na PON ${esc(data.pon)}, posicao ${esc(data.slot)}.`);
  showToast('Servico/VLAN aplicado -- ONU deve voltar a passar trafego.');
  const targetEl = document.getElementById('onuTargetNum');
  if (targetEl) targetEl.value = data.slot;
  const queryPonEl = document.getElementById('onuQueryPon');
  if (queryPonEl) queryPonEl.value = String(data.pon || pon || '');
  loadOnuHistory();
  onuAccordionOpen('onuStepQuery');
}

async function onuQueryEpon(olt) {
  const pon = Number(document.getElementById('onuQueryPonEpon')?.value || '0');
  const onuNum = Number(document.getElementById('onuQueryOnuNumEpon')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  const ticker = onuStartTicker('onuQueryResult', 'Consultando sinal da ONU');
  const res = await api('/api/olt/onu-signal', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuQueryResult', esc(data?.error || 'Falha ao consultar sinal.'), true);
    return;
  }
  const macsHtml = (data.macs || []).length
    ? `<ul style="margin:6px 0 0;padding-left:18px">${data.macs.map(onuMacLine).join('')}</ul>`
    : '<p style="margin:6px 0 0">Nenhum MAC aprendido atras dessa ONU ainda.</p>';

  onuSetResult('onuQueryResult', `
    <div><b>PON ${esc(data.pon)} / ONU ${esc(data.onu)}</b> - MAC ${esc(data.mac)}</div>
    <div>Estado: ${esc(data.state || '-')} / Distancia: ${esc(data.distance_m ?? '-')} m</div>
    <div>RX: ${esc(data.rx_power_dbm ?? '-')} dBm / TX: ${esc(data.tx_power_dbm ?? '-')} dBm</div>
    <div>Temperatura: ${esc(data.temperature_c ?? '-')} C / Tensao: ${esc(data.voltage_v ?? '-')} V</div>
    <div style="margin-top:6px"><b>MACs aprendidos:</b>${macsHtml}</div>
  `);
}

async function onuQueryVsol(olt) {
  const pon = Number(document.getElementById('onuQueryPonVsol')?.value || '0');
  const onuNum = Number(document.getElementById('onuQueryOnuNumVsol')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  const ticker = onuStartTicker('onuQueryResult', 'Consultando sinal da ONU');
  const res = await api('/api/olt/onu-signal', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuQueryResult', esc(data?.error || 'Falha ao consultar sinal.'), true);
    return;
  }
  onuSetResult('onuQueryResult', `
    <div><b>PON ${esc(data.pon)} / ONU ${esc(data.onu_id)}</b> - MAC ${esc(data.onu_mac || '-')}</div>
    <div>Estado: ${esc(data.oper_status || '-')} / Distancia: ${esc(data.distance_km ?? '-')} km</div>
    <div>RX: ${esc(data.onu_rx ?? '-')} dBm</div>
  `);
}

async function onuQuery() {
  if (!onuHasCapability('onu_signal')) { showToast(onuCapabilityMessage('onu_signal'), true); return; }
  const olt = onuOltPayload();
  if (!olt.olt_ip || (!olt.olt_id && !olt.password)) { showToast('Escolha uma OLT cadastrada ou informe IP e senha.', true); return; }
  if (!onuConnectorReady(olt)) return;
  if (onuIsVsol(onuSelectedRegistryRow())) { return onuQueryVsol(olt); }
  if (onuIsEpon(onuSelectedRegistryRow())) { return onuQueryEpon(olt); }
  const onuNum = Number(document.getElementById('onuTargetNum')?.value.trim() || '0');
  const serial = document.getElementById('onuQuerySerial')?.value.trim() || '';
  if (!onuNum && !serial) { showToast('Informe o numero da ONU ou o serial.', true); return; }
  const queryPon = Number(document.getElementById('onuQueryPon')?.value || 0);
  const ponNum = queryPon || onuOltPonNumber(olt);
  if (onuNum && !ponNum) { showToast('Informe a PON para consultar pelo numero da ONU, ou use o serial.', true); return; }

  const payload = {
    olt_id: olt.olt_id || null,
    olt_ip: olt.olt_ip,
    user: olt.user,
    password: olt.password,
    pon: ponNum,
    onu: onuNum || 0,
    serial,
    site: olt.site || '',
    olt_name: olt.olt_name || '',
    connector_id: olt.connector_id || '',
    remote_connector_id: olt.remote_connector_id || '',
    connector_name: olt.connector_name || '',
  };
  const ticker = onuStartTicker('onuQueryResult', 'Consultando sinal e MACs na OLT');
  const res = await api('/api/olt/onu-signal', { method: 'POST', body: JSON.stringify(payload) });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuQueryResult', esc(data?.detail || data?.error || 'Falha ao consultar a ONU.'), true);
    return;
  }

  const targetEl = document.getElementById('onuTargetNum');
  if (targetEl) targetEl.value = data.onu;
  const queryPonEl = document.getElementById('onuQueryPon');
  if (queryPonEl && data.pon) queryPonEl.value = String(data.pon);
  loadOnuHistory();

  const macsHtml = (data.macs || []).length
    ? `<ul style="margin:6px 0 0;padding-left:18px">${data.macs.map(onuMacLine).join('')}</ul>`
    : '<p style="margin:6px 0 0">Nenhum MAC aprendido atras dessa ONU ainda.</p>';
  _oltInventoryRows = null;
  const invSync = data.inventory?.updated
    ? `<div style="margin-top:6px;color:var(--primary)">Inventario OLT atualizado com ${esc(String(data.inventory.macs || 0))} MAC(s).</div>`
    : '';

  onuSetResult('onuQueryResult', `
    <div><b>PON ${esc(data.pon)} / ONU ${esc(data.onu)}</b> - ${esc(data.serial)} (${esc(data.model)})</div>
    <div style="margin-top:4px">Status: ${esc(data.oper_status || '-')} / OMCI ${esc(data.omci_status || '-')}</div>
    <div>OLT RX: ${esc(data.olt_rx || '-')} &nbsp; ONU RX: ${esc(data.onu_rx || '-')} &nbsp; Distancia: ${esc(data.distance_km || '-')} km</div>
    <div style="margin-top:6px"><b>MACs aprendidos:</b>${macsHtml}</div>
    ${invSync}
  `);
}

async function onuRebootEpon(olt) {
  const pon = Number(document.getElementById('onuRebootPonEpon')?.value || '0');
  const onuNum = Number(document.getElementById('onuRebootOnuNumEpon')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  const ticker = onuStartTicker('onuRebootResult', 'Reiniciando ONU na OLT');
  const res = await api('/api/olt/reboot-onu', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuRebootResult', esc(data?.error || data?.detail || 'Falha ao reiniciar ONU.'), true);
    return;
  }
  onuSetResult('onuRebootResult', `ONU da PON ${esc(pon)} / posicao ${esc(onuNum)} reiniciada.`);
  showToast('Comando de reinicio enviado a ONU.');
  loadOnuHistory();
}

async function onuRebootVsol(olt) {
  const pon = Number(document.getElementById('onuRebootPonVsol')?.value || '0');
  const onuNum = Number(document.getElementById('onuRebootOnuNumVsol')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  const ticker = onuStartTicker('onuRebootResult', 'Reiniciando ONU na OLT (equipamento vivo)');
  const res = await api('/api/olt/reboot-onu', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  loadOnuHistory();
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuRebootResult', esc(data?.error || 'Falha ao reiniciar ONU.'), true);
    return;
  }
  onuSetResult('onuRebootResult', `<div><b>PON ${esc(pon)} / ONU ${esc(onuNum)}</b> reiniciada.</div>`);
}

async function onuReboot() {
  if (!onuHasCapability('reboot_onu')) { showToast(onuCapabilityMessage('reboot_onu'), true); return; }
  const olt = onuOltPayload();
  if (onuIsVsol(onuSelectedRegistryRow())) { return onuRebootVsol(olt); }
  if (onuIsEpon(onuSelectedRegistryRow())) { return onuRebootEpon(olt); }
  if (!olt.olt_ip || (!olt.olt_id && !olt.password)) { showToast('Escolha uma OLT cadastrada ou informe IP e senha.', true); return; }
  if (!onuConnectorReady(olt)) return;
  const ponNum = Number(document.getElementById('onuRebootPon')?.value.trim() || '0');
  if (!ponNum) { showToast('Escolha a PON da ONU a reiniciar.', true); return; }
  const onuNum = Number(document.getElementById('onuRebootOnuNum')?.value.trim() || '0');
  if (!onuNum) { showToast('Informe o numero da ONU (posicao) a reiniciar.', true); return; }

  const payload = {
    olt_id: olt.olt_id || null,
    olt_ip: olt.olt_ip,
    user: olt.user,
    password: olt.password,
    pon: ponNum,
    onu: onuNum,
    site: olt.site || '',
    olt_name: olt.olt_name || '',
    connector_id: olt.connector_id || '',
    remote_connector_id: olt.remote_connector_id || '',
    connector_name: olt.connector_name || '',
  };
  const ticker = onuStartTicker('onuRebootResult', 'Reiniciando ONU na OLT (equipamento vivo)');
  const res = await api('/api/olt/reboot-onu', { method: 'POST', body: JSON.stringify(payload) });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuRebootResult', esc(data?.detail || data?.error || 'Falha ao reiniciar ONU (confira se a posicao esta correta).'), true);
    return;
  }
  onuSetResult('onuRebootResult', `ONU da PON ${esc(data.pon)} / posicao ${esc(data.onu)} reiniciada. Aguarde alguns instantes para ela voltar a responder.`);
  showToast('Comando de reinicio enviado a ONU.');
  loadOnuHistory();
}

let _onuDeleteTarget = null; // {olt, pon, onu}

function openOnuDeleteModal() { document.getElementById('modalOnuDelete')?.classList.remove('hidden'); }
function closeOnuDeleteModal() { document.getElementById('modalOnuDelete')?.classList.add('hidden'); }

async function onuDeleteEpon(olt) {
  const pon = Number(document.getElementById('onuDeletePonEpon')?.value || '0');
  const onuNum = Number(document.getElementById('onuDeleteOnuNumEpon')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  _onuDeleteTarget = { olt, pon, onu: onuNum, vlanHint: '' };
  const panoramaEl = document.getElementById('onuDeletePanorama');
  const confirmBtn = document.getElementById('confirmOnuDelete');
  if (confirmBtn) confirmBtn.disabled = true;
  openOnuDeleteModal();

  const ticker = onuStartTicker('onuDeletePanorama', 'Consultando dados da ONU na OLT');
  const res = await api('/api/olt/onu-signal', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!panoramaEl) return;
  if (!res?.ok || data?.ok === false) {
    // Consulta previa falhou -- o MAC alvo continua desconhecido. NAO
    // reabilita o botao de confirmar: clicar sem MAC manda `serial: ''`
    // pro backend, que desvincula a posicao mas falha em tirar da
    // whitelist (comando incompleto) -- exatamente o meio-estado que
    // este fluxo de dois passos existe pra evitar.
    panoramaEl.innerHTML = `<p>Sem informacoes para essa ONU (PON ${esc(pon)} / posicao ${esc(onuNum)}) -- ${esc(data?.error || 'nao respondeu')}.</p>`;
    return;
  }
  _onuDeleteTarget.mac = data.mac || '';
  _onuDeleteTarget.vlanHint = onuVlanSummaryFromMacs(data.macs);
  if (confirmBtn) confirmBtn.disabled = false;
  const macsHtml = (data.macs || []).length
    ? `<ul style="margin:6px 0 0;padding-left:18px">${data.macs.map(onuMacLine).join('')}</ul>`
    : '<p style="margin:6px 0 0">Nenhum MAC aprendido atras dessa ONU.</p>';
  panoramaEl.innerHTML = `
    <p>Voce esta prestes a excluir:</p>
    <div style="margin:8px 0;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-soft)">
      <div><b>PON ${esc(pon)} / ONU ${esc(onuNum)}</b> - MAC ${esc(data.mac || '-')}</div>
      <div style="margin-top:4px">Estado: ${esc(data.state || '-')}</div>
      <div style="margin-top:6px"><b>${(data.macs || []).length} MAC(s) que vao perder conexao:</b>${macsHtml}</div>
    </div>
    <p style="color:var(--danger);font-size:13px;margin:0">Isso remove o cadastro e desliga o servico dela AGORA na OLT.</p>
  `;
}

async function onuDeleteVsol(olt) {
  const pon = Number(document.getElementById('onuDeletePonVsol')?.value || '0');
  const onuNum = Number(document.getElementById('onuDeleteOnuNumVsol')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  _onuDeleteTarget = { olt, pon, onu: onuNum, vlanHint: '' };
  const panoramaEl = document.getElementById('onuDeletePanorama');
  const confirmBtn = document.getElementById('confirmOnuDelete');
  if (confirmBtn) confirmBtn.disabled = true;
  openOnuDeleteModal();

  const ticker = onuStartTicker('onuDeletePanorama', 'Consultando dados da ONU na OLT');
  const res = await api('/api/olt/onu-signal', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!panoramaEl) return;
  if (!res?.ok || data?.ok === false) {
    panoramaEl.innerHTML = `<p>Sem informacoes para essa ONU (PON ${esc(pon)} / posicao ${esc(onuNum)}) -- ${esc(data?.error || 'nao respondeu')}.</p>`;
    return;
  }
  _onuDeleteTarget.mac = data.onu_mac || '';
  if (confirmBtn) confirmBtn.disabled = false;
  panoramaEl.innerHTML = `
    <p>Voce esta prestes a excluir:</p>
    <div style="margin:8px 0;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-soft)">
      <div><b>PON ${esc(pon)} / ONU ${esc(onuNum)}</b> - MAC ${esc(data.onu_mac || '-')}</div>
      <div style="margin-top:4px">Estado: ${esc(data.oper_status || '-')}</div>
    </div>
    <p style="color:var(--danger);font-size:13px;margin:0">Isso remove a autorizacao e desliga o servico dela AGORA na OLT.</p>
  `;
}

async function onuDelete() {
  if (!onuHasCapability('delete_onu')) { showToast(onuCapabilityMessage('delete_onu'), true); return; }
  const olt = onuOltPayload();
  if (onuIsVsol(onuSelectedRegistryRow())) { return onuDeleteVsol(olt); }
  if (onuIsEpon(onuSelectedRegistryRow())) { return onuDeleteEpon(olt); }
  if (!olt.olt_ip || (!olt.olt_id && !olt.password)) { showToast('Escolha uma OLT cadastrada ou informe IP e senha.', true); return; }
  if (!onuConnectorReady(olt)) return;
  const ponNum = Number(document.getElementById('onuDeletePon')?.value.trim() || '0');
  if (!ponNum) { showToast('Escolha a PON da ONU a excluir.', true); return; }
  const onuNum = Number(document.getElementById('onuDeleteOnuNum')?.value.trim() || '0');
  if (!onuNum) { showToast('Informe o numero da ONU (posicao) a excluir.', true); return; }

  _onuDeleteTarget = { olt, pon: ponNum, onu: onuNum };
  const panoramaEl = document.getElementById('onuDeletePanorama');
  const confirmBtn = document.getElementById('confirmOnuDelete');
  if (confirmBtn) confirmBtn.disabled = true;
  openOnuDeleteModal();

  const ticker = onuStartTicker('onuDeletePanorama', 'Consultando dados da ONU na OLT');
  const res = await api('/api/olt/onu-signal', { method: 'POST', body: JSON.stringify({ olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password, pon: ponNum, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '', connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '' }) });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (confirmBtn) confirmBtn.disabled = false;
  if (!panoramaEl) return;
  if (!res?.ok || data?.ok === false) {
    panoramaEl.innerHTML = `<p>Sem informacoes para essa ONU (PON ${esc(ponNum)} / posicao ${esc(onuNum)}) -- ${esc(data?.detail || data?.error || 'nao respondeu')}.</p>`;
    return;
  }
  const macsHtml = (data.macs || []).length
    ? `<ul style="margin:6px 0 0;padding-left:18px">${data.macs.map(onuMacLine).join('')}</ul>`
    : '<p style="margin:6px 0 0">Nenhum MAC aprendido atras dessa ONU.</p>';
  if (_onuDeleteTarget) _onuDeleteTarget.vlanHint = onuVlanSummaryFromMacs(data.macs);
  panoramaEl.innerHTML = `
    <p>Voce esta prestes a excluir:</p>
    <div style="margin:8px 0;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-soft)">
      <div><b>PON ${esc(data.pon)} / ONU ${esc(data.onu)}</b> - ${esc(data.serial)} (${esc(data.model)})</div>
      <div style="margin-top:4px">Status: ${esc(data.oper_status || '-')} / OMCI ${esc(data.omci_status || '-')}</div>
      <div style="margin-top:6px"><b>${(data.macs || []).length} MAC(s) que vao perder conexao:</b>${macsHtml}</div>
    </div>
    <p style="color:var(--danger);font-size:13px;margin:0">Isso remove o cadastro e desliga o servico dela AGORA na OLT.</p>
  `;
}

async function onuConfirmDelete() {
  if (!_onuDeleteTarget) { closeOnuDeleteModal(); return; }
  const { olt, pon, onu, vlanHint, mac } = _onuDeleteTarget;
  const panoramaEl = document.getElementById('onuDeletePanorama');
  const confirmBtn = document.getElementById('confirmOnuDelete');
  if (confirmBtn) confirmBtn.disabled = true;
  if (panoramaEl) panoramaEl.insertAdjacentHTML('beforeend', '<p id="onuDeleteTickerLine" style="margin-top:10px">Excluindo ONU na OLT (equipamento vivo)... (0s)</p>');
  let onuDeleteTick = 0;
  const onuDeleteTicker = setInterval(() => {
    onuDeleteTick += 1;
    const line = document.getElementById('onuDeleteTickerLine');
    if (line) line.textContent = `Excluindo ONU na OLT (equipamento vivo)... (${onuDeleteTick}s)`;
  }, 1000);

  const isEpon = onuIsEpon(onuSelectedRegistryRow());
  const payload = isEpon
    ? { olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
        olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
        pon, onu, serial: mac || '', vlan_hint: vlanHint || '', site: olt.site || '',
        connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '' }
    : { olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password, pon, onu,
        vlan_hint: vlanHint || '', site: olt.site || '', connector_id: olt.connector_id || '',
        remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '' };
  const res = await api('/api/olt/delete-onu', { method: 'POST', body: JSON.stringify(payload) });
  clearInterval(onuDeleteTicker);
  const data = await res?.json().catch(() => ({}));
    closeOnuDeleteModal();
    _onuDeleteTarget = null;
    if (!res?.ok || data?.ok === false) {
      onuSetResult('onuDeleteResult', esc(data?.detail || data?.error || 'Falha ao excluir ONU (confira se a posicao esta correta).'), true);
      return;
    }
  _oltInventoryRows = null;
  const removed = Number(data.inventory?.removed || 0);
  const invMsg = removed > 0 ? ` Removida do inventario OLT (${removed} registro${removed !== 1 ? 's' : ''}).` : ' Nenhum registro correspondente no inventario OLT.';
  onuSetResult('onuDeleteResult', `ONU excluida da PON ${esc(data.pon)} / posicao ${esc(data.onu)}.${esc(invMsg)}`);
  showToast(removed > 0 ? 'ONU excluida e removida do inventario.' : 'ONU excluida; nao havia registro no inventario.');
  loadOnuHistory();
}


async function deployLookupMac() {
  if (!deployEnsureStepUnlocked('cftvStep2', 'Conclua a etapa 1 antes de procurar a camera.')) return;
  const p = deployPayload();
  if (!p.connector_id) {
    deploySetResult('Busca por MAC exige um conector MikroTik. No modo Local, preencha o IP da camera e clique em Entrar.', true);
    showToast('Busca por MAC exige conector MikroTik.', true);
    return;
  }
  if (!p.camera_mac) { showToast('Digite o MAC ou IP da camera.', true); return; }
  deploySetResult('Consultando DHCP, ARP e neighbors do MikroTik...');
  const data = await apiJson(`/api/deployments/lookup?connector_id=${encodeURIComponent(p.connector_id)}&query=${encodeURIComponent(p.camera_mac)}`);
  const matches = Array.isArray(data?.matches) ? data.matches : [];
  if (!matches.length) {
    deploySetResult('Nenhum dispositivo encontrado no conector para esse MAC/IP.', true);
    return;
  }
  const first = matches[0];
  if (first.mac && !document.getElementById('deployCameraMac')?.value) document.getElementById('deployCameraMac').value = first.mac;
  deploySetResult(matches.slice(0, 5).map(m => `
    <div class="deploy-match deploy-cam-pick" data-ip="${esc(m.ip || '')}" data-mac="${esc(m.mac || '')}" role="button" tabindex="0" aria-label="Selecionar IP ${esc(m.ip || '')}">
      <b>${esc(m.ip || '-')}</b>
      <span>${esc(m.mac || '-')}</span>
      <small><span class="deploy-pick-source">${esc(m.source || '-')} ${m.host ? `- ${esc(m.host)}` : ''}</span><strong class="deploy-pick-action">Clique para selecionar</strong></small>
    </div>
  `).join(''));
  document.querySelectorAll('#deployLookupResult .deploy-cam-pick').forEach(el => {
    const selectMatch = () => {
      document.querySelectorAll('#deployLookupResult .deploy-cam-pick').forEach(row => row.classList.remove('selected'));
      el.classList.add('selected');
      const action = el.querySelector('.deploy-pick-action');
      if (action) action.textContent = 'Selecionado';
      // So guarda o IP encontrado no Mikrotik pra usar na conexao de "puxar
      // dados" -- o campo visivel "IP da camera" so preenche com o que vier
      // da propria camera (pull), nao com o achado aqui no MAC/ARP.
      _deployPullTargetIp = el.dataset.ip || '';
      if (el.dataset.mac) document.getElementById('deployCameraMac').value = el.dataset.mac;
      showToast(`Selecionado: ${el.dataset.ip || el.dataset.mac}`);
      deployRenderSummary();
      const userEl = document.getElementById('deployCameraUser');
      const passEl = document.getElementById('deployCameraPassword');
      if (userEl?.value && passEl?.value) {
        deployPullCameraInfo();
      } else {
        const box = document.getElementById('deployPullCameraResult');
        if (box) box.innerHTML = `IP ${esc(el.dataset.ip || '')} selecionado (via Mikrotik). Preencha usuario/senha da camera e clique em "Puxar dados da camera" pra confirmar.`;
      }
    };
    el.addEventListener('click', selectMatch);
    el.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectMatch();
      }
    });
  });
  deployRenderSummary();
}

async function deployPullCameraInfo() {
  if (!deployEnsureStepUnlocked('cftvStep2', 'Conclua a etapa 1 antes de entrar na camera.')) return;
  // IP pra conectar vem do que foi selecionado na busca de MAC (Mikrotik),
  // ou do valor ja confirmado no campo (de um pull anterior) -- nunca de
  // digitacao manual, ja que o campo visivel fica travado.
  const ip = _deployPullTargetIp || document.getElementById('deployCameraIp')?.value.trim() || '';
  const user = document.getElementById('deployCameraUser')?.value.trim() || '';
  const pass = document.getElementById('deployCameraPassword')?.value || '';
  const box = document.getElementById('deployPullCameraResult');
  if (!ip) { showToast('Descubra o IP da camera pelo MAC primeiro (etapa acima).', true); return; }
  if (!user || !pass) { showToast('Informe usuario e senha da camera primeiro.', true); return; }
  if (box) box.innerHTML = 'Conectando na camera e trazendo os dados (pode levar alguns segundos)...';
  const res = await api('/api/rescan-single-ip', {
    method: 'POST',
    body: JSON.stringify({
      ip,
      usuario: user,
      senha: pass,
      inventory_mode: document.getElementById('deployInventoryMode')?.value || 'basic',
      capture_snapshot: true,
    }),
  });
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false || data?.success === false) {
    if (box) box.innerHTML = esc(data?.message || data?.stderr || 'Falha ao conectar na camera. Confira IP/usuario/senha.');
    box?.classList.add('error');
    return;
  }
  box?.classList.remove('error');
  const rows = Array.isArray(data.inventory) ? data.inventory : [];
  const cam = rows.find(r => (r.ip || '').trim() === ip) || null;
  if (!cam) {
    if (box) box.innerHTML = 'Conectou, mas nao consegui identificar os dados da camera na resposta.';
    return;
  }
  const fabEl = document.getElementById('deployCameraManufacturer');
  const modEl = document.getElementById('deployCameraModel');
  const titleEl = document.getElementById('deployCameraTitle');
  const ipEl = document.getElementById('deployCameraIp');
  if (fabEl && cam.fabricante) fabEl.value = cam.fabricante;
  if (modEl && cam.modelo) modEl.value = cam.modelo;
  if (titleEl && cam.titulo) titleEl.value = cam.titulo;
  if (ipEl) ipEl.value = cam.ip || ip;
  _deployConfirmedCameraIp = cam.ip || ip;
  if (box) box.innerHTML = 'Dados da camera trazidos com sucesso.';
  showToast(`Camera encontrada: ${cam.fabricante || ''} ${cam.modelo || ''}`.trim());
  deployRenderSummary();
  deployUpdateStepLocks({ autoAdvance: true });
}

// Grava o titulo direto na camera (best-effort). Chamado como parte do
// "Registrar camera" no rodape -- se a camera nao responder, so avisa via
// toast e segue com o registro no inventario mesmo assim.
async function deployPushTitleToCamera(title) {
  const ip = _deployConfirmedCameraIp || document.getElementById('deployCameraIp')?.value.trim() || '';
  const user = document.getElementById('deployCameraUser')?.value.trim() || '';
  const pass = document.getElementById('deployCameraPassword')?.value || '';
  if (!ip || !user || !pass) return { ok: false, skipped: true };
  const res = await api('/api/deployments/save-camera-title', {
    method: 'POST',
    body: JSON.stringify({ ip, usuario: user, senha: pass, title }),
  });
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    return { ok: false, detail: data?.detail || 'Falha ao gravar titulo na camera.' };
  }
  return { ok: true };
}

function deploySetCheckIpResult(html, isError = false) {
  const box = document.getElementById('deployCheckIpResult');
  if (!box) return;
  box.innerHTML = html || 'Se trocar o IP e clicar em "Checar novo IP", confiro disponibilidade e pergunto se quer aplicar na camera.';
  box.classList.toggle('error', !!isError);
}

async function deployCheckNewIp() {
  if (!deployEnsureStepUnlocked('cftvStep2', 'Conclua a etapa 1 antes de checar IP.')) return;
  const p = deployPayload();
  const newIp = document.getElementById('deployCameraIp')?.value.trim() || '';
  if (!newIp) { showToast('Informe o IP a checar.', true); return; }
  deploySetCheckIpResult('Checando disponibilidade do IP...');
  const data = await apiJson(`/api/deployments/ip-check?ip=${encodeURIComponent(newIp)}&connector_id=${encodeURIComponent(p.connector_id)}&site=${encodeURIComponent(p.site)}`);
  if (!data) { showToast('Nao foi possivel checar o IP.', true); return; }
  if (data.in_use) {
    const places = (data.matches || []).map(m => `${m.source || 'inventario'}: ${m.title || m.mac || m.host || '-'}`).join('<br>');
    deploySetCheckIpResult(`IP ${esc(newIp)} ja aparece em uso.<br>${places}`, true);
    return;
  }
  if (!_deployConfirmedCameraIp || newIp === _deployConfirmedCameraIp) {
    deploySetCheckIpResult(`IP ${esc(newIp)} livre no inventario.`);
    return;
  }
  const user = document.getElementById('deployCameraUser')?.value.trim() || '';
  const pass = document.getElementById('deployCameraPassword')?.value || '';
  if (!user || !pass) {
    deploySetCheckIpResult(`IP ${esc(newIp)} livre. Preencha usuario/senha da camera pra eu poder aplicar direto nela.`);
    return;
  }
  if (!confirm(`IP ${newIp} esta livre.\n\nTrocar o IP da camera (atualmente em ${_deployConfirmedCameraIp}) pra esse novo IP agora?\n\nIsso muda a rede real do equipamento -- se a mascara/gateway herdados estiverem errados, a camera pode ficar inalcancavel.`)) {
    deploySetCheckIpResult(`IP ${esc(newIp)} livre no inventario. Troca cancelada.`);
    return;
  }
  deploySetCheckIpResult('Aplicando novo IP na camera (equipamento vivo, aguarde)...');
  const res = await api('/api/deployments/apply-camera-ip', {
    method: 'POST',
    body: JSON.stringify({ ip: _deployConfirmedCameraIp, usuario: user, senha: pass, new_ip: newIp }),
  });
  const result = await res?.json().catch(() => ({}));
  if (!res?.ok || result?.ok === false) {
    deploySetCheckIpResult(esc(result?.detail || 'Falha ao aplicar o novo IP na camera.'), true);
    return;
  }
  deploySetCheckIpResult(`IP aplicado na camera: ${esc(result.new_ip)} (mascara ${esc(result.subnet_mask || '-')}${result.gateway ? `, gateway ${esc(result.gateway)}` : ''}).`);
  showToast(`Novo IP aplicado na camera: ${result.new_ip}`);
  _deployConfirmedCameraIp = newIp;
  deployRenderSummary();
}

async function deploySaveDraft() {
  const payload = deployPayload();
  const res = await api('/api/deployments', { method: 'POST', body: JSON.stringify(payload) });
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    showToast(data?.detail || 'Falha ao salvar rascunho.', true);
    return;
  }
  _deployCurrentId = data.deployment?.id || _deployCurrentId;
  showToast('Rascunho de implantacao salvo.');
  await loadDeployHistory();
  deployRenderSummary();
}

async function deploySaveCameraInventory() {
  if (!deployEnsureStepUnlocked('cftvStep2', 'Conclua a etapa 1 antes de salvar a camera.')) return;
  const payload = deployPayload();
  if (!payload.camera_title || !payload.camera_ip) {
    showToast('IP e titulo da camera sao obrigatorios para salvar no inventario.', true);
    return;
  }
  if (!payload.site) {
    showToast('Site/local e obrigatorio. Escolha do inventario ou digite um novo.', true);
    return;
  }
  const btn = document.getElementById('btnDeploySaveCameraInventory');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Salvando'; lucide.createIcons(); }
  try {
    const res = await api('/api/deployments/commit-camera', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      showToast(data?.detail || 'Falha ao salvar camera no inventario.', true);
      return;
    }
    _deployCurrentId = data.deployment?.id || _deployCurrentId;
    const rec = data.recorder_link || {};
    const recMsg = rec.ok ? ` Gravador ${rec.source?.toUpperCase() || ''} ${rec.host} canal ${rec.channel} -> ${rec.camera_ip}.` : '';
    showToast(`Camera salva no inventario: ${payload.camera_title}`);
    deploySetResult(`Camera salva no inventario (${esc(data.inventory_mode || payload.inventory_mode)}). Coordenada: ${esc(payload.location || '-')}.${esc(recMsg)}`);
    const savedMode = data.inventory_mode || payload.inventory_mode || 'basic';
    const camMode = savedMode === 'basic' ? 'basico' : savedMode;
    if (_invCam[camMode]) {
      await _loadCamForMode(camMode);
      updateCamTabs();
      populateCamSiteFilter();
      if (_currentView === 'inv-olt' && _invOltView === camMode) applyInvOltFilters();
    }
    await loadDeployHistory();
    deployRenderSummary();
    deployUpdateStepLocks({ autoAdvance: true });
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="save"></i> Salvar'; lucide.createIcons(); }
  }
}

async function deployRecorderLogin() {
  if (!deployEnsureStepUnlocked('cftvStep3', 'Conclua a etapa 2 antes de entrar no gravador.')) return;
  const payload = deployPayload();
  if (!payload.recorder_type) {
    deploySetRecorderLoginResult('Escolha o tipo do gravador antes de entrar.', true);
    showToast('Escolha NVR IP ou DVR analogico.', true);
    return;
  }
  if (!payload.recorder_host || !payload.recorder_user || !payload.recorder_password) {
    deploySetRecorderLoginResult('Informe host, usuario e senha do gravador.', true);
    showToast('Informe host, usuario e senha do gravador.', true);
    return;
  }
  const btn = document.getElementById('btnDeployRecorderLogin');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Entrando'; lucide.createIcons(); }
  deploySetRecorderLoginResult(`Conectando em ${esc(payload.recorder_host)}...`);
  try {
    const res = await api('/api/deployments/recorder-login', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      const detail = data?.detail || data?.message || 'Falha ao entrar no gravador. Confira host, usuario e senha.';
      deploySetRecorderLoginResult(esc(detail), true);
      showToast(detail, true);
      return;
    }
    const label = [data.brand, data.model, data.name].filter(Boolean).join(' / ');
    const msg = `Login confirmado em ${esc(payload.recorder_host)}${label ? ` - ${esc(label)}` : ''}.`;
    deployRenderRecorderChannels(Array.isArray(data.channels) ? data.channels : []);
    deploySetRecorderLoginResult(msg);
    showToast('Login do gravador confirmado.');
  } catch (err) {
    const detail = err?.detail || err?.message || 'Falha ao entrar no gravador.';
    deploySetRecorderLoginResult(esc(detail), true);
    showToast(detail, true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="log-in"></i> Entrar'; lucide.createIcons(); }
  }
}

async function deployRecorderAddCamera() {
  if (!deployEnsureStepUnlocked('cftvStep3', 'Conclua a etapa 2 antes de adicionar no gravador.')) return;
  const payload = deployPayload();
  if (!payload.recorder_type || !payload.recorder_host || !payload.recorder_user || !payload.recorder_password) {
    deploySetRecorderLoginResult('Entre no gravador antes de adicionar a camera.', true);
    showToast('Entre no gravador antes de adicionar a camera.', true);
    return;
  }
  if (!payload.recorder_channel) {
    deploySetRecorderLoginResult('Selecione um canal livre antes de adicionar.', true);
    showToast('Selecione um canal livre.', true);
    return;
  }
  if (!payload.recorder_camera_ip || !payload.recorder_title) {
    deploySetRecorderLoginResult('IP da camera e titulo no gravador sao obrigatorios.', true);
    showToast('IP da camera e titulo no gravador sao obrigatorios.', true);
    return;
  }
  if (!payload.camera_user || !payload.camera_password) {
    deploySetRecorderLoginResult('Usuario e senha da camera sao obrigatorios para adicionar no NVR.', true);
    showToast('Informe usuario e senha da camera.', true);
    return;
  }
  const btn = document.getElementById('btnDeployRecorderAddCamera');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Adicionando'; lucide.createIcons(); }
  deploySetRecorderLoginResult(`Adicionando ${esc(payload.recorder_camera_ip)} no canal ${esc(payload.recorder_channel)}...`);
  try {
    const res = await api('/api/deployments/recorder-add-camera', { method: 'POST', body: JSON.stringify(payload) });
    const data = await res?.json().catch(() => ({}));
    if (!res?.ok || data?.ok === false) {
      const detail = data?.detail || 'Falha ao adicionar camera no gravador.';
      deploySetRecorderLoginResult(esc(detail), true);
      showToast(detail, true);
      return;
    }
    deployRenderRecorderChannels(Array.isArray(data.channels) ? data.channels : []);
    deploySetRecorderLoginResult(`Camera adicionada no ${esc(payload.recorder_type.toUpperCase())} ${esc(payload.recorder_host)} canal ${String(data.channel || payload.recorder_channel).padStart(2, '0')}.`);
    showToast('Camera adicionada no gravador.');
    await loadDeployHistory();
    deployRenderSummary();
  } catch (err) {
    const detail = err?.detail || err?.message || 'Falha ao adicionar camera no gravador.';
    deploySetRecorderLoginResult(esc(detail), true);
    showToast(detail, true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="plus-circle"></i> Adicionar no NVR'; lucide.createIcons(); }
  }
}

async function deployCommitCamera(e) {
  e?.preventDefault();
  if (!deployEnsureStepUnlocked('cftvStep2', 'Conclua a etapa 1 antes de registrar a camera.')) return;
  const payload = deployPayload();
  if (!payload.camera_title || !payload.camera_ip) {
    showToast('IP e titulo da camera sao obrigatorios.', true);
    return;
  }
  if (!payload.site) {
    showToast('Site/local e obrigatorio. Escolha do inventario ou digite um novo.', true);
    return;
  }

  const titlePush = await deployPushTitleToCamera(payload.camera_title);
  if (titlePush.ok) {
    showToast('Titulo gravado na camera.');
  } else if (!titlePush.skipped) {
    showToast(`Titulo NAO gravado na camera: ${titlePush.detail}`, true);
  }

  const res = await api('/api/deployments/commit-camera', { method: 'POST', body: JSON.stringify(payload) });
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    showToast(data?.detail || 'Falha ao registrar camera.', true);
    return;
  }
  _deployCurrentId = data.deployment?.id || _deployCurrentId;
  const rec = data.recorder_link || {};
  const recMsg = rec.ok ? ` Gravador ${rec.source?.toUpperCase() || ''} ${rec.host} canal ${rec.channel} -> ${rec.camera_ip}.` : '';
  showToast(`Camera registrada: ${payload.camera_title}`);
  deploySetResult(`Camera registrada no inventario (${esc(data.inventory_mode || payload.inventory_mode)}). Chave: ${esc(data.inventory_key || '-')}.${esc(recMsg)}`);
  await loadDeployHistory();
  deployRenderSummary();
  deployUpdateStepLocks({ autoAdvance: true });
}

function deployClear() {
  _deployCurrentId = '';
  _deployPullTargetIp = '';
  _deployConfirmedCameraIp = '';
  document.getElementById('deployForm')?.reset();
  const connEl = document.getElementById('deployConnector');
  if (connEl) connEl.value = '';
  deployRenderOltContextForOrigin();
  deployApplyOriginFields();
  deploySetResult('Aguardando consulta no conector.');
  deployRenderConnectorStatus();
  const pullBox = document.getElementById('deployPullCameraResult');
  if (pullBox) {
    pullBox.innerHTML = 'Descubra o IP pelo MAC (acima), preencha usuario/senha, depois clique para trazer os dados reais da camera.';
    pullBox.classList.remove('error');
  }
  deploySetCheckIpResult();
  deploySetRecorderLoginResult();
  deployRenderRecorderChannels();
  deployRenderSummary();
  deployOpenStep('cftvStep1');
}

//  Conectores SaaS 
let _connectors = [];
let _lastCreatedConnectorId = '';
let _lastCreatedConnectorType = '';

function formatDateTimeShort(value) {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleString('pt-BR');
  } catch {
    return value;
  }
}

function connectorHostLabel(host) {
  if (!host || typeof host !== 'object') return '-';
  const name = host.hostname || host.identity || '';
  const model = host.model || '';
  const ips = Array.isArray(host.ips) ? host.ips.filter(Boolean).slice(0, 2).join(', ') : '';
  return [name, model, ips].filter(Boolean).join(' / ') || '-';
}

function connectorInventoryLabel(row) {
  const inv = row?.inventory || {};
  const items = [];
  if (String(row?.type || '').toLowerCase() === 'ruijie') {
    if (inv.count) items.push(`${inv.count} dispositivos`);
    return items.join(' / ');
  }
  if (inv.dhcp_leases) items.push(`DHCP ${inv.dhcp_leases}`);
  if (inv.arp_entries) items.push(`ARP ${inv.arp_entries}`);
  if (inv.neighbors) items.push(`Neighbors ${inv.neighbors}`);
  return items.join(' / ');
}

function connectorTypeLabel(type) {
  const t = String(type || 'routeros').toLowerCase();
  if (t === 'routeros') return 'MikroTik';
  return 'Windows';
}

function connectorById(connectorId) {
  return _connectors.find(row => String(row.id || '') === String(connectorId || '')) || null;
}

function private24FromIp(value) {
  const parts = String(value || '').trim().split('.');
  if (parts.length !== 4) return '';
  const nums = parts.map(part => Number(part));
  if (nums.some(num => !Number.isInteger(num) || num < 0 || num > 255)) return '';
  const [a, b, c] = nums;
  const isPrivate = a === 10
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && b === 168)
    || (a === 100 && b >= 64 && b <= 127);
  if (!isPrivate) return '';
  return `${a}.${b}.${c}.0/24`;
}

function ipToNumber(ip) {
  const nums = String(ip || '').split('.').map(part => Number(part));
  if (nums.length !== 4 || nums.some(num => !Number.isInteger(num) || num < 0 || num > 255)) return null;
  return nums.reduce((acc, num) => ((acc << 8) + num) >>> 0, 0);
}

function numberToIp(num) {
  return [24, 16, 8, 0].map(shift => ((num >>> shift) & 255)).join('.');
}

function normalizePrivateCidr(value) {
  const raw = String(value || '').trim();
  const match = raw.match(/^(\d{1,3}(?:\.\d{1,3}){3})\/(\d{1,2})$/);
  if (!match) return '';
  const ipNum = ipToNumber(match[1]);
  const prefix = Number(match[2]);
  if (ipNum === null || !Number.isInteger(prefix) || prefix < 1 || prefix >= 32) return '';
  const parts = match[1].split('.').map(part => Number(part));
  const [a, b] = parts;
  const privateLan = a === 10
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && b === 168)
    || (a === 100 && b >= 64 && b <= 127);
  if (!privateLan) return '';
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  const network = numberToIp((ipNum & mask) >>> 0);
  const cidr = `${network}/${prefix}`;
  if (cidr === '10.250.0.0/24') return '';
  return cidr;
}

function parseCidrInfo(cidr) {
  const raw = normalizePrivateCidr(cidr);
  if (!raw) return null;
  const [ip, prefixRaw] = raw.split('/');
  const networkNum = ipToNumber(ip);
  const prefix = Number(prefixRaw);
  if (networkNum === null || !Number.isInteger(prefix)) return null;
  const size = 2 ** (32 - prefix);
  return { cidr: raw, networkNum, prefix, start: networkNum, end: networkNum + size - 1 };
}
