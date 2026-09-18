// Implantacao > Projetos: parque planejado, separado do inventario real.
let _planningProjects = [];
let _planningCurrent = null;
let _planningMap = null;
let _planningMapLayers = null;
let _planningMarkers = {};
let _planningCatalog = null;
let _planningExpandedRows = new Set();

// Overlay de progresso pra operacoes longas (ex: duplicar caixa em lote) --
// sem isso, a tela fica parada varios segundos sem nenhum sinal de que
// esta fazendo algo.
function planningShowProgress(text) {
  let el = document.getElementById('planningProgressOverlay');
  if (!el) {
    el = document.createElement('div');
    el.id = 'planningProgressOverlay';
    el.className = 'planning-progress-overlay';
    el.innerHTML = `<div class="planning-progress-card">
      <div class="planning-progress-spinner"></div>
      <strong id="planningProgressText"></strong>
      <div class="planning-progress-bar"><div class="planning-progress-bar-fill" id="planningProgressFill"></div></div>
    </div>`;
    document.body.appendChild(el);
  }
  el.classList.remove('hidden');
  planningUpdateProgress(text, 0);
}

function planningUpdateProgress(text, pct) {
  const textEl = document.getElementById('planningProgressText');
  const fillEl = document.getElementById('planningProgressFill');
  if (textEl && text) textEl.textContent = text;
  if (fillEl) fillEl.style.width = `${Math.max(0, Math.min(100, pct))}%`;
}

function planningHideProgress() {
  document.getElementById('planningProgressOverlay')?.classList.add('hidden');
}

const PLANNING_TYPES = {
  camera: 'Camera', onu: 'ONU', ont: 'ONT', olt: 'OLT', switch: 'Switch',
  injector: 'Injetor PoE', cto: 'CTO', recorder: 'Gravador', box: 'Caixa de CFTV', pole: 'Poste',
  rack: 'Rack', dio: 'DIO', other: 'Outro',
  // "emenda" nao e um device_type de verdade (o backend so conhece "cto") --
  // e uma opcao de Tipo so pra escolha inicial mais clara. Vira 'cto' com
  // metadata.papel='splitter_primario' assim que o formulario e lido (ver
  // planningEffectiveType/planningTypeOptionValue).
  emenda: 'Caixa de Emenda',
};
// Ordem ABNT do cabo optico -- usada tanto na entrada quanto nas saidas do
// splitter (Caixa de Emenda/CTO), limitada ao total de FO do cabo escolhido.
const PLANNING_FIBER_COLORS = ['Verde', 'Amarelo', 'Branco', 'Azul', 'Vermelho', 'Lilas', 'Marrom', 'Rosa', 'Preto', 'Cinza', 'Laranja', 'Agua'];

function planningPoeCapacity(metadata) {
  const poe = Number(metadata?.poe_port_capacity);
  return poe > 0 ? poe : Number(metadata?.port_capacity || 0);
}

const PLANNING_STATUS = {
  draft: 'Rascunho', planned: 'Planejado', approved: 'Aprovado',
  deploying: 'Em implantacao', completed: 'Concluido',
};

function planningEscape(value) {
  return typeof esc === 'function' ? esc(value ?? '') : String(value ?? '').replace(/[&<>"']/g, '');
}

function planningNaturalCompare(left, right) {
  return String(left || '').localeCompare(String(right || ''), 'pt-BR', { numeric: true, sensitivity: 'base' });
}

// Valor clicavel-pra-copiar, usado onde o texto pode ser longo (coordenada,
// serial...) e cortar com "..." esconderia parte que a pessoa precisa colar
// em outro lugar (Google Maps, etc.).
function planningCopyableValue(value, title = 'Copiar') {
  const text = String(value || '').trim();
  if (!text) return '<strong>A definir</strong>';
  return `<button type="button" class="planning-copy-value" data-copy-value="${planningEscape(text)}" onclick="planningCopyFromButton(event)" title="${planningEscape(title)}"><strong>${planningEscape(text)}</strong><i data-lucide="copy"></i></button>`;
}

async function planningCopyFromButton(event) {
  const btn = event.currentTarget;
  const value = btn?.dataset.copyValue || '';
  if (!value) return;
  if (await planningCopyText(value)) showToast('Copiado para a area de transferencia.');
  else showToast('Nao foi possivel copiar automaticamente. Selecione o texto e copie manualmente.', true);
}

// navigator.clipboard exige contexto seguro (https) -- em http (ou se o
// navegador negar a permissao) cai pro jeito antigo via textarea oculta,
// que funciona em qualquer contexto.
async function planningCopyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) { /* segue pro fallback */ }
  }
  try {
    const el = document.createElement('textarea');
    el.value = text;
    el.setAttribute('readonly', '');
    el.style.position = 'fixed';
    el.style.opacity = '0';
    el.style.left = '-9999px';
    document.body.appendChild(el);
    el.select();
    el.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(el);
    return ok;
  } catch (err) {
    return false;
  }
}

// Monta "CAIXA - 01 > SWITCH-01 > Porta 1 > 01 - CAMERA" subindo a cadeia de
// pais -- util pra colar num chamado/mensagem pro tecnico de campo achar
// fisicamente onde a camera esta ligada, sem precisar abrir o app.
function planningDevicePath(item) {
  const devices = _planningCurrent?.devices || [];
  const chainUp = [];
  const seen = new Set();
  let current = item;
  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    chainUp.push(current);
    current = current.parent_id ? devices.find(d => Number(d.id) === Number(current.parent_id)) : null;
  }
  const parts = [];
  chainUp.reverse().forEach(node => {
    if (node.metadata?.port_number) parts.push(`Porta ${node.metadata.port_number}`);
    parts.push(node.name);
  });
  return parts.join(' > ');
}

async function planningCopyDevicePath(id) {
  const item = (_planningCurrent?.devices || []).find(row => Number(row.id) === Number(id));
  if (!item) return;
  const path = planningDevicePath(item);
  if (await planningCopyText(path)) showToast(`Caminho copiado: ${path}`);
  else showToast('Nao foi possivel copiar.', true);
}

async function planningRequest(path, options = {}) {
  const res = await api(path, options);
  return jsonOrReadableError(res, 'Nao foi possivel concluir a operacao do projeto.');
}

async function planningMultipart(path, formData) {
  const headers = {};
  if (_token) headers.Authorization = `Bearer ${_token}`;
  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', body: formData, headers, credentials: 'same-origin' });
  if (res.status === 401) {
    _token = null;
    showLoginScreen();
  }
  return jsonOrReadableError(res, 'Nao foi possivel importar o arquivo.');
}

async function loadPlanning(force = false) {
  const data = await apiJson('/api/planning/projects', { forceRefresh: force, cacheTtl: 0 });
  _planningProjects = data?.items || [];
  renderPlanningProjects();
  const currentId = Number(_planningCurrent?.id || 0);
  const next = _planningProjects.find(item => Number(item.id) === currentId) || _planningProjects[0];
  if (next) await selectPlanningProject(next.id);
  else showPlanningEmpty();
}

function renderPlanningProjects() {
  const box = document.getElementById('planningProjectList');
  const count = document.getElementById('planningProjectCount');
  if (!box || !count) return;
  count.textContent = `${_planningProjects.length} projeto(s).`;
  if (!_planningProjects.length) {
    box.innerHTML = '<div class="planning-list-empty">Nenhum projeto cadastrado.</div>';
    return;
  }
  box.innerHTML = _planningProjects.map(item => `
    <button class="planning-project-card ${Number(item.id) === Number(_planningCurrent?.id) ? 'active' : ''}" onclick="selectPlanningProject(${Number(item.id)})">
      <span class="planning-project-card-top"><strong>${planningEscape(item.name)}</strong><span>${planningEscape(PLANNING_STATUS[item.status] || item.status)}</span></span>
      <small>${planningEscape(item.client_name || 'Cliente nao informado')}</small>
      <span class="planning-project-card-stats">${Number(item.sites_count || 0)} sites · ${Number(item.cameras_count || 0)} cameras · ${Number(item.onus_count || 0)} ONUs</span>
    </button>`).join('');
}

function showPlanningEmpty() {
  _planningCurrent = null;
  document.getElementById('planningEmpty')?.classList.remove('hidden');
  document.getElementById('planningDetail')?.classList.add('hidden');
  renderPlanningProjects();
}

async function selectPlanningProject(projectId) {
  const data = await apiJson(`/api/planning/projects/${Number(projectId)}`, { forceRefresh: true, cacheTtl: 0 });
  if (!data?.item) return false;
  _planningCurrent = data.item;
  document.getElementById('planningEmpty')?.classList.add('hidden');
  document.getElementById('planningDetail')?.classList.remove('hidden');
  renderPlanningProjects();
  renderPlanningDetail();
  setTimeout(() => renderPlanningMap(), 50);
  return true;
}

// selectPlanningProject falha em silencio se a resposta vier vazia (ex: pico
// de escrita apos criar muitos equipamentos de uma vez, tipo duplicar caixa).
// Tenta de novo algumas vezes antes de pedir pro usuario atualizar a pagina.
async function planningRefreshCurrentProject(retries = 2) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (await selectPlanningProject(_planningCurrent.id)) return true;
    if (attempt < retries) await new Promise(resolve => setTimeout(resolve, 500));
  }
  return false;
}

function renderPlanningDetail() {
  const project = _planningCurrent;
  if (!project) return;
  const devices = project.devices || [];
  const cameras = devices.filter(item => item.device_type === 'camera').length;
  const onus = devices.filter(item => ['onu', 'ont'].includes(item.device_type)).length;
  document.getElementById('planningStatus').textContent = PLANNING_STATUS[project.status] || project.status;
  document.getElementById('planningStatus').dataset.status = project.status;
  document.getElementById('planningTitle').textContent = project.name;
  document.getElementById('planningSubtitle').textContent = [project.client_name, project.description].filter(Boolean).join(' · ') || 'Projeto sem descricao.';
  document.getElementById('planningSitesKpi').textContent = (project.sites || []).length;
  document.getElementById('planningCamerasKpi').textContent = cameras;
  document.getElementById('planningOnusKpi').textContent = onus;
  document.getElementById('planningDevicesKpi').textContent = devices.length;
  fillPlanningFilters();
  renderPlanningDevices();
}

function fillPlanningFilters() {
  const select = document.getElementById('planningSiteFilter');
  const current = select?.value || '';
  if (select) {
    select.innerHTML = '<option value="">Todos os sites</option>' + (_planningCurrent?.sites || []).map(site =>
      `<option value="${Number(site.id)}">${planningEscape(site.name)}</option>`).join('');
    select.value = current;
  }
}

// Navegando sem busca/filtro de tipo: mostra so o topo da hierarquia --
// quem tem pai (ex: ONU dentro de uma caixa) so aparece ao expandir o pai,
// nao como linha solta na lista. Busca/filtro de tipo e "achar algo
// especifico", entao ai mostra tudo de forma plana, inclusive aninhados.
function planningIsBrowsingHierarchy() {
  const term = String(document.getElementById('planningSearch')?.value || '').trim();
  const type = document.getElementById('planningTypeFilter')?.value || '';
  return !term && !type;
}

function filteredPlanningDevices() {
  const term = String(document.getElementById('planningSearch')?.value || '').trim().toLowerCase();
  const type = document.getElementById('planningTypeFilter')?.value || '';
  const site = document.getElementById('planningSiteFilter')?.value || '';
  const isBrowsingHierarchy = planningIsBrowsingHierarchy();
  const devices = _planningCurrent?.devices || [];
  // Se o parent_id aponta pra um equipamento que nao existe mais (dado
  // antigo, importado, ou pai excluido), tratar como raiz -- senao o item
  // some da lista sem nenhum pai valido pra abrir e mostrar ele de volta.
  const deviceIds = new Set(devices.map(d => Number(d.id)));
  return devices.filter(item => {
    if (type && item.device_type !== type) return false;
    if (site && String(item.site_id || '') !== site) return false;
    if (isBrowsingHierarchy && item.parent_id && deviceIds.has(Number(item.parent_id))) return false;
    if (!term) return true;
    return [item.name, item.ip, item.model, item.manufacturer, item.site_name, item.parent_name]
      .some(value => String(value || '').toLowerCase().includes(term));
  });
}

function planningDescendantCount(itemId, allDevices) {
  let count = 0;
  for (const child of allDevices) {
    if (Number(child.parent_id) !== Number(itemId)) continue;
    count += 1 + planningDescendantCount(child.id, allDevices);
  }
  return count;
}

function togglePlanningRowExpand(id) {
  const key = Number(id);
  if (_planningExpandedRows.has(key)) _planningExpandedRows.delete(key);
  else _planningExpandedRows.add(key);
  renderPlanningDevices();
}

function planningClearDeviceFilters() {
  const search = document.getElementById('planningSearch'); if (search) search.value = '';
  const type = document.getElementById('planningTypeFilter'); if (type) type.value = '';
  const site = document.getElementById('planningSiteFilter'); if (site) site.value = '';
  renderPlanningDevices();
}

function renderPlanningDevices() {
  const box = document.getElementById('planningDeviceList');
  if (!box) return;
  const rows = filteredPlanningDevices();
  box.closest('.planning-devices-panel')?.classList.toggle('is-empty', rows.length === 0);
  if (!rows.length) {
    const term = document.getElementById('planningSearch')?.value || '';
    const type = document.getElementById('planningTypeFilter')?.value || '';
    const site = document.getElementById('planningSiteFilter')?.value || '';
    const totalDevices = (_planningCurrent?.devices || []).length;
    if (term || type || site) {
      box.innerHTML = '<div class="planning-list-empty"><strong>Nenhum equipamento bate com essa busca/filtro.</strong><span>Confira o que foi digitado ou o filtro selecionado.</span><button class="secondary-action" type="button" onclick="planningClearDeviceFilters()">Limpar busca e filtros</button></div>';
    } else if (totalDevices > 0) {
      box.innerHTML = '<div class="planning-list-empty"><strong>Tem equipamento cadastrado, mas nenhum aparece aqui.</strong><span>Provavelmente algum esta ligado a um "pai" que nao existe mais. Use a busca por nome pra encontrar e corrigir o campo "Ligado a".</span></div>';
    } else {
      box.innerHTML = '<div class="planning-list-empty"><strong>Nenhum equipamento encontrado.</strong><span>Adicione manualmente, importe um CSV ou gere cameras em lote.</span></div>';
    }
    return;
  }
  const allDevices = _planningCurrent?.devices || [];
  const canExpand = planningIsBrowsingHierarchy();
  const renderRow = (item, depth) => {
    const metadata = item.metadata || {};
    const children = allDevices.filter(child => Number(child.parent_id) === Number(item.id)).sort((a, b) => planningNaturalCompare(a.name, b.name));
    const childCount = children.length;
    const relation = ['switch', 'injector'].includes(item.device_type) && metadata.port_capacity
      ? `${childCount}/${planningPoeCapacity(metadata)} portas PoE usadas`
      : ['onu', 'ont'].includes(item.device_type) && metadata.eth_port_capacity
      ? `${childCount}/${Number(metadata.eth_port_capacity)} portas ETH usadas`
      : ['cto', 'dio'].includes(item.device_type) && metadata.port_capacity
      ? `${childCount}/${Number(metadata.port_capacity)} portas usadas`
      : item.device_type === 'olt' && metadata.pon_port_capacity
      ? `${childCount}/${Number(metadata.pon_port_capacity)} portas PON usadas`
      : item.device_type === 'box' ? `${planningDescendantCount(item.id, allDevices)} equipamento(s) dentro`
      : ['camera', 'switch', 'injector'].includes(item.device_type) && item.parent_name && metadata.port_number
      ? `${item.parent_name} · Porta ${metadata.port_number}`
      : (item.parent_name || (item.pon ? `PON ${item.pon}` : 'Sem vinculo'));
    const expandable = canExpand && childCount > 0;
    const expanded = expandable && _planningExpandedRows.has(Number(item.id));
    const toggle = expandable
      ? `<button class="planning-row-toggle" type="button" onclick="event.stopPropagation();togglePlanningRowExpand(${Number(item.id)})" aria-label="${expanded ? 'Recolher' : 'Expandir'}"><i data-lucide="${expanded ? 'chevron-down' : 'chevron-right'}"></i></button>`
      : '<span class="planning-row-toggle-spacer"></span>';
    const row = `
    <article class="planning-device-row${depth ? ' is-child' : ''}" data-device-id="${Number(item.id)}" style="${depth ? `grid-template-columns:${28 + depth * 20}px minmax(0, 1fr) auto` : ''}">
      ${toggle}
      <button class="planning-device-focus" onclick="openPlanningDeviceDetails(${Number(item.id)})" title="${item.device_type === 'box' ? 'Ver equipamentos e cameras desta caixa' : PLANNING_PORT_HOLDER_CONFIG[item.device_type] ? 'Ver e cadastrar equipamentos ligados por porta' : 'Abrir detalhes do equipamento'}">
        <span class="planning-device-icon ${planningEscape(item.device_type)}">${item.reference_image_url ? `<img src="${planningEscape(item.reference_image_url)}" alt="Imagem ilustrativa" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{innerHTML:'&bull;'}))">` : `<i data-lucide="${planningDeviceIcon(item.device_type)}"></i>`}</span>
        <span class="planning-device-primary"><strong title="${planningEscape(item.name)}">${planningEscape(item.name)}</strong><small>${planningEscape(PLANNING_TYPES[item.device_type] || item.device_type)} · ${planningEscape(item.site_name || 'Sem site')}</small></span>
        <span class="planning-device-ip">${planningItemHasNoIp(item) ? '' : planningEscape(item.ip || 'IP a definir')}</span>
        <span class="planning-device-model"><strong>${planningEscape(item.model || 'Modelo a definir')}</strong><small>${planningEscape(item.manufacturer || 'Fabricante nao informado')}</small></span>
        <span class="planning-device-parent">${planningEscape(relation)}</span>
      </button>
      <div class="planning-row-actions">
        ${['camera', 'switch', 'injector'].includes(item.device_type) ? `<button class="icon-button" onclick="planningCopyDevicePath(${Number(item.id)})" aria-label="Copiar caminho"><i data-lucide="copy"></i></button>` : ''}
        <button class="icon-button" onclick="openPlanningDeviceModal(${Number(item.id)})" aria-label="Editar"><i data-lucide="pencil"></i></button>
        <button class="icon-button danger" onclick="deletePlanningDevice(${Number(item.id)})" aria-label="Excluir"><i data-lucide="trash-2"></i></button>
      </div>
    </article>`;
    const childrenHtml = expanded ? children.map(child => renderRow(child, depth + 1)).join('') : '';
    return row + childrenHtml;
  };
  box.innerHTML = rows.map(item => renderRow(item, 0)).join('');
  lucide.createIcons();
}

function planningDeviceIcon(type) {
  return ({ camera: 'camera', onu: 'wifi', ont: 'wifi', olt: 'radio-tower', switch: 'server', injector: 'plug-zap', cto: 'git-branch', recorder: 'hard-drive', box: 'package', pole: 'utility-pole', rack: 'server-cog', dio: 'layout-grid', emenda: 'git-merge' })[type] || 'box';
}

function planningDescendants(parentId) {
  const devices = _planningCurrent?.devices || [];
  const found = [];
  const visit = id => devices.filter(item => Number(item.parent_id) === Number(id)).forEach(item => {
    found.push(item);
    visit(item.id);
  });
  visit(parentId);
  return found;
}

function openPlanningDeviceDetails(deviceId) {
  const item = (_planningCurrent?.devices || []).find(row => Number(row.id) === Number(deviceId));
  if (!item) return;
  if (item.device_type === 'box') { openPlanningBoxDetails(deviceId); return; }
  if (PLANNING_PORT_HOLDER_CONFIG[item.device_type]) { openPlanningPortHolderDetails(deviceId); return; }
  openPlanningDeviceModal(deviceId);
}

function openPlanningBoxDetails(deviceId) {
  const devices = _planningCurrent?.devices || [];
  const item = devices.find(row => Number(row.id) === Number(deviceId));
  if (!item) return;
  const direct = devices.filter(row => Number(row.parent_id) === Number(item.id));
  const descendants = planningDescendants(item.id);
  const internal = descendants.filter(row => row.device_type !== 'camera').sort((a, b) => planningNaturalCompare(a.name, b.name));
  const cameras = descendants.filter(row => row.device_type === 'camera').sort((a, b) => planningNaturalCompare(a.name, b.name));
  const distribution = internal.filter(row => ['switch', 'injector'].includes(row.device_type));
  const capacity = distribution.reduce((total, row) => total + planningPoeCapacity(row.metadata), 0);
  const usedPorts = cameras.length;
  const coordinate = [item.latitude, item.longitude].filter(value => value !== null && value !== undefined && value !== '').join(', ');
  const equipmentRows = internal.length ? internal.map(row => `
    <button class="planning-box-detail-row" type="button" onclick="closePlanningModal();openPlanningDeviceDetails(${Number(row.id)})">
      <span class="planning-device-icon ${planningEscape(row.device_type)}"><i data-lucide="${planningDeviceIcon(row.device_type)}"></i></span>
      <span><strong>${planningEscape(row.name)}</strong><small>${planningEscape(PLANNING_TYPES[row.device_type] || row.device_type)} &middot; ${planningEscape([row.manufacturer, row.model].filter(Boolean).join(' / ') || 'Modelo a definir')}</small></span>
      <i data-lucide="chevron-right"></i>
    </button>`).join('') : '<div class="planning-box-empty">Nenhum equipamento interno cadastrado.</div>';
  const cameraRows = cameras.length ? cameras.map(row => {
    const distance = Number(row.metadata?.distance_to_box_m);
    const parent = devices.find(parent => Number(parent.id) === Number(row.parent_id));
    return `
      <button class="planning-box-camera-row" type="button" onclick="closePlanningModal();openPlanningDeviceModal(${Number(row.id)})">
        <span class="planning-device-icon camera"><i data-lucide="camera"></i></span>
        <span class="planning-box-camera-main"><strong>${planningEscape(row.name)}</strong><small>${planningEscape(parent?.name || 'Ligacao a definir')}${row.metadata?.port_number ? ` · Porta ${planningEscape(row.metadata.port_number)}` : ''}</small></span>
        <span class="planning-box-camera-location"><strong>${planningEscape(row.ip || 'IP a definir')}</strong><small>${Number.isFinite(distance) ? `${distance.toFixed(1)} m da caixa` : 'Distancia a definir'}</small></span>
        <i data-lucide="chevron-right"></i>
      </button>`;
  }).join('') : '<div class="planning-box-empty">Nenhuma camera ligada a esta caixa.</div>';

  planningModal({
    eyebrow: 'Caixa de CFTV', title: item.name, wide: true, primary: 'Editar caixa',
    body: `<div class="planning-box-details">
      <div class="planning-box-summary-grid">
        <div><span>Site/local</span><strong>${planningEscape(item.site_name || 'Sem site')}</strong></div>
        <div><span>Coordenada da caixa</span>${planningCopyableValue(coordinate, 'Copiar coordenada')}</div>
        <div><span>Equipamentos internos</span><strong>${internal.length}</strong></div>
        <div><span>Cameras atendidas</span><strong>${cameras.length}</strong></div>
      </div>
      <div class="planning-box-duplicate-bar"><button class="secondary-action" type="button" onclick="openPlanningDuplicateModal(${Number(item.id)})"><i data-lucide="copy-plus"></i> Duplicar esta caixa (com tudo dentro)</button></div>
      <div class="planning-box-capacity ${capacity && usedPorts > capacity ? 'danger' : ''}">
        <span><i data-lucide="network"></i><strong>Distribuicao PoE</strong></span>
        <span>${capacity ? `${usedPorts} de ${capacity} portas planejadas` : `${usedPorts} camera(s), capacidade ainda nao informada`}</span>
      </div>
      <section class="planning-box-section"><div class="planning-box-section-head"><div><h3>Dentro da caixa</h3><p>ONU/ONT que recebe a fibra. Switch e injetor ligam na porta ETH dela, nao direto na caixa.</p></div><div class="planning-box-section-head-actions"><button class="secondary-action" type="button" onclick="openPlanningAddToBox(${Number(item.id)},'onu')"><i data-lucide="plus"></i> Adicionar ONU</button><span>${internal.length}</span></div></div>${equipmentRows}</section>
      <section class="planning-box-section"><div class="planning-box-section-head"><div><h3>Cameras ligadas</h3><p>As cameras permanecem nas coordenadas individuais e aparecem pelo vinculo com o switch ou injetor.</p></div><span>${cameras.length}</span></div>${cameraRows}</section>
    </div>`,
    onSave: async () => { closePlanningModal(); await openPlanningDeviceModal(item.id); },
  });
}

// Acha a ultima sequencia de digitos no nome (ex: "CAIXA - 01" -> "01") pra
// trocar so o numero em cada copia, mantendo o resto do nome igual.
function planningFindTrailingNumber(name) {
  const match = String(name || '').match(/(\d+)(?!.*\d)/);
  return match ? { token: match[1] } : null;
}

function planningCloneName(name, oldToken, newToken) {
  if (!oldToken) return name;
  const value = String(name || '');
  return value.includes(oldToken) ? value.split(oldToken).join(newToken) : value;
}

function openPlanningDuplicateModal(boxId) {
  const box = (_planningCurrent?.devices || []).find(row => Number(row.id) === Number(boxId));
  if (!box) return;
  const found = planningFindTrailingNumber(box.name);
  const digits = found ? found.token.length : 2;
  const defaultStart = found ? Number(found.token) + 1 : 2;
  planningModal({
    title: `Duplicar ${planningEscape(box.name)}`, primary: 'Duplicar',
    body: `<div class="planning-form-grid">
      ${planningField('Quantidade de copias', 'planDupCount', '1', 'type="number" min="1" max="60"')}
      ${planningField('Numero inicial', 'planDupStart', String(defaultStart), 'type="number" min="0"')}
      ${planningField('Digitos', 'planDupDigits', String(digits), 'type="number" min="1" max="4"')}
    </div><div class="planning-info"><i data-lucide="info"></i><span>Cria uma copia completa da caixa -- ONU, switch, injetor, camera, portas e tudo mais que estiver dentro dela${found ? `, trocando o numero "${planningEscape(found.token)}" pelo numero de cada copia` : ''}. A copia nao herda o vinculo com CTO/OLT nem a porta ocupada -- ajuste IP, site, vinculo e demais detalhes depois em cada uma.</span></div>`,
    onSave: async root => {
      const value = id => Number(root.querySelector(`#${id}`).value) || 0;
      const count = Math.max(1, Math.min(60, value('planDupCount') || 1));
      const start = value('planDupStart');
      const digitsCount = Math.max(1, value('planDupDigits') || 2);
      closePlanningModal();
      const nodesPerCopy = 1 + planningDescendants(box.id).length;
      const totalNodes = nodesPerCopy * count;
      let nodesDone = 0;
      planningShowProgress(`Duplicando caixa 1 de ${count}...`);
      try {
        for (let i = 0; i < count; i++) {
          planningUpdateProgress(`Duplicando caixa ${i + 1} de ${count}...`, (nodesDone / totalNodes) * 100);
          const newToken = String(start + i).padStart(digitsCount, '0');
          await planningCloneSubtree(box.id, found ? found.token : null, newToken, () => {
            nodesDone++;
            planningUpdateProgress(null, (nodesDone / totalNodes) * 100);
          });
        }
        planningHideProgress();
        _planningCatalog = null;
        showToast(`${count} caixa(s) criada(s).`);
        if (!(await planningRefreshCurrentProject())) {
          showToast('Caixas criadas, mas a lista nao atualizou sozinha -- de um Ctrl+Shift+R.', true);
        }
      } catch (err) {
        planningHideProgress();
        showToast(err.message || 'Nao foi possivel duplicar a caixa.', true);
        if (!(await planningRefreshCurrentProject())) {
          showToast('Atualize a pagina pra ver o que ja foi criado.', true);
        }
      }
    },
  });
}

function planningCloneItemPayload(item, name, parentId, isRoot) {
  const metadata = { ...(item.metadata || {}) };
  if (isRoot) delete metadata.port_number; // a copia ainda nao tem porta de CTO/OLT escolhida
  return {
    device_type: item.device_type, name, ip: item.ip || '',
    site_id: item.site_id || null, manufacturer: item.manufacturer || '', model: item.model || '',
    parent_id: parentId, pon: item.pon || '', onu_position: item.onu_position || '',
    latitude: item.latitude ?? null, longitude: item.longitude ?? null,
    reference_image_url: item.reference_image_url || '', notes: item.notes || '',
    metadata, status: 'planned',
  };
}

// Clona a caixa e toda a arvore dentro dela (ONU > switch/injetor > camera),
// nivel por nivel -- um filho so pode ser criado depois que o pai dele ja
// tiver o novo id, entao processa em ondas (BFS) a partir da raiz.
async function planningCloneSubtree(rootId, oldToken, newToken, onProgress) {
  const devices = _planningCurrent?.devices || [];
  const root = devices.find(d => Number(d.id) === Number(rootId));
  if (!root) return;
  const descendants = planningDescendants(rootId);
  const byParent = new Map();
  descendants.forEach(d => {
    const key = Number(d.parent_id);
    const list = byParent.get(key) || [];
    list.push(d);
    byParent.set(key, list);
  });

  const rootPayload = planningCloneItemPayload(root, planningCloneName(root.name, oldToken, newToken), null, true);
  const rootResult = await planningRequest(`/api/planning/projects/${_planningCurrent.id}/devices`, {
    method: 'POST', body: JSON.stringify(rootPayload),
  });
  const newRootId = rootResult?.item?.id;
  if (!newRootId) throw new Error('Nao foi possivel criar a copia da caixa.');
  onProgress?.();

  let currentOldIds = [rootId];
  let currentNewIds = [newRootId];
  while (currentOldIds.length) {
    const nextOldIds = [];
    const nextNewIds = [];
    for (let i = 0; i < currentOldIds.length; i++) {
      const children = byParent.get(Number(currentOldIds[i])) || [];
      for (const child of children) {
        const payload = planningCloneItemPayload(child, planningCloneName(child.name, oldToken, newToken), currentNewIds[i], false);
        const created = await planningRequest(`/api/planning/projects/${_planningCurrent.id}/devices`, {
          method: 'POST', body: JSON.stringify(payload),
        });
        const newId = created?.item?.id;
        if (newId) { nextOldIds.push(child.id); nextNewIds.push(newId); onProgress?.(); }
      }
    }
    currentOldIds = nextOldIds;
    currentNewIds = nextNewIds;
  }
}

function openPlanningAddToBox(boxId, deviceType = 'onu') {
  const box = (_planningCurrent?.devices || []).find(row => Number(row.id) === Number(boxId));
  if (!box) return;
  closePlanningModal();
  openPlanningDeviceModal(0, {
    parent_id: box.id, device_type: deviceType, site_id: box.site_id ?? null,
    latitude: box.latitude ?? null, longitude: box.longitude ?? null,
  });
}

function openPlanningAddPortChild(parentId, deviceType) {
  const parent = (_planningCurrent?.devices || []).find(row => Number(row.id) === Number(parentId));
  if (!parent) return;
  closePlanningModal();
  openPlanningDeviceModal(0, {
    parent_id: parent.id, device_type: deviceType, site_id: parent.site_id ?? null,
    latitude: parent.latitude ?? null, longitude: parent.longitude ?? null,
  });
}

// Quem tem porta pra ligar equipamento embaixo, e o que pode ser ligado:
// ONU/ONT tem portas ETH onde entra switch/injetor; switch/injetor tem
// portas PoE onde entra camera. Mesma tela de detalhe serve pros dois
// niveis, so muda a config.
const PLANNING_PORT_HOLDER_CONFIG = {
  olt: { childTypes: ['dio', 'cto', 'box'], addButtons: [['dio', 'Adicionar DIO'], ['cto', 'Adicionar CDO/CTO'], ['box', 'Adicionar Caixa']], portsLabel: 'Portas PON' },
  dio: { childTypes: ['cto'], addButtons: [['cto', 'Adicionar Splitter/CTO']], portsLabel: 'Portas' },
  cto: { childTypes: ['box'], addButtons: [['box', 'Adicionar Caixa']], portsLabel: 'Portas' },
  onu: { childTypes: ['switch', 'injector'], addButtons: [['switch', 'Adicionar Switch'], ['injector', 'Adicionar Injetor']], portsLabel: 'Portas ETH' },
  ont: { childTypes: ['switch', 'injector'], addButtons: [['switch', 'Adicionar Switch'], ['injector', 'Adicionar Injetor']], portsLabel: 'Portas ETH' },
  switch: { childTypes: ['camera'], addButtons: [['camera', 'Adicionar Camera']], portsLabel: 'Portas PoE' },
  injector: { childTypes: ['camera'], addButtons: [['camera', 'Adicionar Camera']], portsLabel: 'Portas PoE' },
};

function openPlanningPortHolderDetails(deviceId) {
  const devices = _planningCurrent?.devices || [];
  const item = devices.find(row => Number(row.id) === Number(deviceId));
  if (!item) return;
  const config = PLANNING_PORT_HOLDER_CONFIG[item.device_type];
  if (!config) return;
  const metadata = item.metadata || {};
  const children = devices.filter(row => Number(row.parent_id) === Number(item.id) && config.childTypes.includes(row.device_type))
    .sort((a, b) => (Number(a.metadata?.port_number) || 999) - (Number(b.metadata?.port_number) || 999) || planningNaturalCompare(a.name, b.name));
  const capacity = planningParentPortCapacity(item);
  const modeLabel = item.device_type === 'switch' ? (metadata.switch_mode === 'smart' ? 'Smart - gerenciavel (com IP)' : 'Normal - sem gerenciamento (bridge)')
    : item.device_type === 'ont' ? 'ONT - roteador/VEIP (gerenciavel)'
    : item.device_type === 'onu' ? 'ONU - bridge transparente'
    : '';
  const typeLabel = PLANNING_TYPES[item.device_type] || item.device_type;
  const childRows = children.length ? children.map(row => `
    <button class="planning-box-camera-row" type="button" onclick="closePlanningModal();openPlanningDeviceDetails(${Number(row.id)})">
      <span class="planning-device-icon ${planningEscape(row.device_type)}"><i data-lucide="${planningDeviceIcon(row.device_type)}"></i></span>
      <span class="planning-box-camera-main"><strong>${planningEscape(row.name)}</strong><small>${row.metadata?.port_number ? `Porta ${planningEscape(row.metadata.port_number)}` : 'Porta a definir'} · ${planningEscape(PLANNING_TYPES[row.device_type] || row.device_type)}</small></span>
      <span class="planning-box-camera-location"><strong>${planningItemHasNoIp(row) ? '' : planningEscape(row.ip || 'IP a definir')}</strong></span>
      <i data-lucide="chevron-right"></i>
    </button>`).join('') : '<div class="planning-box-empty">Nada ligado ainda.</div>';
  const addButtonsHtml = config.addButtons.map(([type, label]) => `<button class="secondary-action" type="button" onclick="openPlanningAddPortChild(${Number(item.id)},'${type}')"><i data-lucide="plus"></i> ${planningEscape(label)}</button>`).join('');

  planningModal({
    eyebrow: typeLabel, title: item.name, wide: true, primary: `Editar ${typeLabel.toLowerCase()}`,
    body: `<div class="planning-box-details">
      <div class="planning-box-summary-grid">
        <div><span>Site/local</span><strong>${planningEscape(item.site_name || 'Sem site')}</strong></div>
        ${modeLabel ? `<div><span>Modo</span><strong>${planningEscape(modeLabel)}</strong></div>` : ''}
        <div><span>${planningEscape(config.portsLabel)}</span><strong>${capacity || '-'}</strong></div>
        <div><span>Equipamentos ligados</span><strong>${children.length}</strong></div>
      </div>
      <section class="planning-box-section"><div class="planning-box-section-head"><div><h3>Ligado por porta</h3><p>Cada equipamento ocupa uma porta deste equipamento.</p></div><div class="planning-box-section-head-actions">${addButtonsHtml}<span>${children.length}</span></div></div>${childRows}</section>
    </div>`,
    onSave: async () => { closePlanningModal(); await openPlanningDeviceModal(item.id); },
  });
}

function planningModal({ eyebrow = 'Planejamento', title, body, primary = 'Salvar', onSave, wide = false }) {
  let root = document.getElementById('planningModal');
  if (!root) {
    root = document.createElement('div');
    root.id = 'planningModal';
    root.className = 'modal-backdrop hidden';
    document.body.appendChild(root);
  }
  root.innerHTML = `<div class="modal planning-modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
    <div class="modal-header"><div><p class="eyebrow">${planningEscape(eyebrow)}</p><h2>${planningEscape(title)}</h2></div><button class="icon-button" data-close><i data-lucide="x"></i></button></div>
    <div class="planning-modal-body">${body}</div>
    <div class="planning-modal-footer"><button class="secondary-action" data-close>Cancelar</button><button class="primary-action" data-save><i data-lucide="check"></i> ${planningEscape(primary)}</button></div>
  </div>`;
  root.classList.remove('hidden');
  // Sem fechar ao clicar fora -- e facil perder o que estava editando sem
  // querer. So fecha pelo X ou pelo Cancelar.
  root.onclick = null;
  root.querySelectorAll('[data-close]').forEach(btn => btn.onclick = closePlanningModal);
  root.querySelector('[data-save]').onclick = async event => {
    const btn = event.currentTarget;
    btn.disabled = true;
    try { await onSave(root); } catch (err) { showToast(err.message || 'Operacao nao concluida.', true); btn.disabled = false; }
  };
  lucide.createIcons();
  setTimeout(() => root.querySelector('input,select,textarea')?.focus(), 20);
  return root;
}

function closePlanningModal() {
  document.getElementById('planningModal')?.classList.add('hidden');
}

function planningField(label, id, value = '', extra = '', wrapAttrs = '') {
  return `<label class="planning-field" ${wrapAttrs}><span>${planningEscape(label)}</span><input id="${id}" value="${planningEscape(value)}" ${extra}></label>`;
}

// Caixa/poste/CTO sao elementos fisicos sem endereco de rede proprio. ONU
// (bridge transparente) tambem nao tem IP de gerenciamento -- so a ONT
// (VEIP/roteador) e "gerenciavel" e tem IP, igual no menu OLT. ONU e ONT sao
// um so "cartao" no formulario (Tipo = onu); o campo Modo decide qual das
// duas funcoes se aplica, em vez de serem duas opcoes separadas de Tipo.
// Switch segue o mesmo principio: Normal (sem gerenciamento, bridge, sem IP)
// vs Smart (gerenciavel, com IP) -- so que aqui o device_type salvo continua
// sempre "switch", o Modo so mexe no IP e fica guardado em metadata.
const PLANNING_TYPES_WITHOUT_IP = ['box', 'pole', 'cto', 'onu', 'injector', 'rack', 'dio', 'emenda'];

// Switch normal (sem gerenciamento) tambem nao tem IP -- mas isso vem do
// metadata.switch_mode salvo no item, nao do device_type (que e sempre
// "switch"). Usado na lista/mapa pra decidir se mostra "IP a definir".
function planningItemHasNoIp(item) {
  if (PLANNING_TYPES_WITHOUT_IP.includes(item.device_type)) return true;
  if (item.device_type === 'switch') return (item.metadata?.switch_mode || 'normal') !== 'smart';
  return false;
}

const PLANNING_DEVICE_FIELD_RULES = {
  planDeviceOnuModeField: { showOnlyFor: ['onu'] },
  planDeviceOnuPortsField: { showOnlyFor: ['onu'] },
  planDeviceSwitchModeField: { showOnlyFor: ['switch'] },
  planDeviceSwitchPortsField: { showOnlyFor: ['switch'] },
  planDeviceCtoPortsField: { showOnlyFor: ['cto', 'dio', 'emenda'] },
  // Caixa de emenda/CTO/DIO sao pontos de cabo optico -- "Site/local" da
  // lugar pro cabo que chega ali (total de FO), que e mais relevante pra
  // eles do que um site generico.
  planDeviceSiteField: { hideFor: ['cto', 'dio', 'emenda'] },
  planDeviceCaboOpticoField: { showOnlyFor: ['cto', 'dio', 'emenda'] },
  // DIO nao divide sinal (patch panel, 1 entrada = 1 saida por porta) --
  // "entrada/saida do splitter" so faz sentido em CTO/Caixa de Emenda.
  planDeviceSplitterFibersField: { showOnlyFor: ['cto', 'emenda'] },
  // So CTO (splitter terminal) tem porta de cliente/caixa CFTV pendurada --
  // a Caixa de Emenda/CDO intermediario so passa fibra adiante, nao atende
  // cliente direto.
  planDeviceCtoOcupacaoField: { showOnlyFor: ['cto'] },
  planDeviceOltPortsField: { showOnlyFor: ['olt'] },
  // planDeviceParentPortField NAO entra aqui -- a visibilidade dele depende
  // de quem esta selecionado em "Ligado a" ter portas (CTO, ONU/ONT,
  // switch/injetor), nao do tipo do proprio equipamento. Ver refreshParentPort.
  planDevicePonField: { showOnlyFor: ['onu', 'ont'] },
  planDeviceOnuField: { showOnlyFor: ['onu', 'ont'] },
  planDeviceSerialField: { showOnlyFor: ['onu', 'ont', 'recorder', 'switch', 'olt'] },
  planDeviceMacField: { showOnlyFor: ['onu', 'ont', 'camera', 'recorder', 'switch', 'injector'] },
  planDeviceVlanField: { showOnlyFor: ['onu'] },
  planDeviceRouteField: { showOnlyFor: ['camera', 'cto', 'dio', 'emenda'] },
};

// Tipo=onu agrupa ONU (bridge) e ONT (roteado); o tipo "de verdade" pra
// decidir device_type salvo/parentesco/catalogo vem do Modo quando o Tipo
// e o grupo onu. Switch nao troca de device_type (sempre "switch") -- ver
// planningShouldHideIp pra saber onde o Modo dele entra.
function planningEffectiveType(modal) {
  const type = modal.querySelector('#planDeviceType')?.value || '';
  if (type === 'onu') return modal.querySelector('#planDeviceOnuMode')?.value || 'onu';
  if (type === 'emenda') return 'cto';
  return type;
}

function planningShouldHideIp(modal) {
  const type = modal.querySelector('#planDeviceType')?.value || '';
  if (type === 'onu') return (modal.querySelector('#planDeviceOnuMode')?.value || 'onu') === 'onu';
  if (type === 'switch') return (modal.querySelector('#planDeviceSwitchMode')?.value || 'normal') === 'normal';
  return PLANNING_TYPES_WITHOUT_IP.includes(type);
}

function refreshPlanningDeviceFields(modal) {
  const type = modal.querySelector('#planDeviceType')?.value || 'camera';
  for (const [fieldId, rule] of Object.entries(PLANNING_DEVICE_FIELD_RULES)) {
    const field = modal.querySelector(`#${fieldId}`);
    if (!field) continue;
    const hide = rule.hideFor ? rule.hideFor.includes(type) : !rule.showOnlyFor.includes(type);
    field.classList.toggle('hidden', hide);
  }
  const ipField = modal.querySelector('#planDeviceIpField');
  if (ipField) ipField.classList.toggle('hidden', planningShouldHideIp(modal));
  // CTO/Caixa de Emenda sao splitter optico -- a "quantidade de portas" e a
  // razao de divisao (1x2, 1x4, 1x8...), nao portas soltas como no DIO
  // (patch panel, so organiza fibra sem dividir sinal).
  const portsSelect = modal.querySelector('#planDeviceCtoPorts');
  if (portsSelect) {
    const current = portsSelect.value;
    const isSplitter = type === 'cto' || type === 'emenda';
    const options = isSplitter
      ? [1, 2, 4, 8, 16, 32, 64].map(n => `<option value="${n}">${n === 1 ? '1 (sem divisao)' : `Splitter 1x${n}`}</option>`)
      : [1, 2, 4, 8, 12, 16, 24, 32].map(n => `<option value="${n}">${n} porta${n === 1 ? '' : 's'}</option>`);
    portsSelect.innerHTML = options.join('');
    if ([...portsSelect.options].some(o => o.value === current)) portsSelect.value = current;
  }
  // Switch so ganha campo de MAC/VLAN quando e Smart (gerenciavel) -- normal
  // (bridge) nao tem como consultar/gerenciar por MAC nem segmentar VLAN.
  const smartSwitch = type === 'switch' && (modal.querySelector('#planDeviceSwitchMode')?.value || 'normal') === 'smart';
  const macField = modal.querySelector('#planDeviceMacField');
  if (macField && type === 'switch') macField.classList.toggle('hidden', !smartSwitch);
  const vlanField = modal.querySelector('#planDeviceVlanField');
  if (vlanField && type === 'switch') vlanField.classList.toggle('hidden', !smartSwitch);
}

function openPlanningProjectModal(isNew = false) {
  const item = isNew ? {} : (_planningCurrent || {});
  planningModal({
    title: item.id ? 'Editar projeto' : 'Novo projeto',
    body: `<div class="planning-form-grid">
      ${planningField('Nome do projeto', 'planProjectName', item.name, 'placeholder="Ex: CFTV Condominio Reserva"')}
      ${planningField('Cliente', 'planProjectClient', item.client_name, 'placeholder="Nome do cliente"')}
      <label class="planning-field"><span>Situacao</span><select id="planProjectStatus">${Object.entries(PLANNING_STATUS).map(([key,label]) => `<option value="${key}" ${item.status === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
      <label class="planning-field full"><span>Descricao</span><textarea id="planProjectDescription" rows="3" placeholder="Escopo e observacoes do projeto">${planningEscape(item.description || '')}</textarea></label>
    </div>`,
    onSave: async root => {
      const payload = {
        name: root.querySelector('#planProjectName').value.trim(),
        client_name: root.querySelector('#planProjectClient').value.trim(),
        status: root.querySelector('#planProjectStatus').value,
        description: root.querySelector('#planProjectDescription').value.trim(),
        kmz_layer_id: item.kmz_layer_id || '',
      };
      if (!payload.name) throw new Error('Informe o nome do projeto.');
      const path = item.id ? `/api/planning/projects/${item.id}` : '/api/planning/projects';
      const data = await planningRequest(path, { method: item.id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
      closePlanningModal();
      _planningCurrent = data.item;
      showToast(item.id ? 'Projeto atualizado.' : 'Projeto criado.');
      await loadPlanning(true);
    },
  });
}

function openPlanningSiteModal() {
  if (!_planningCurrent) return;
  const sites = _planningCurrent.sites || [];
  const rows = sites.length ? sites.map(site => `
    <div class="planning-site-row" data-site-id="${Number(site.id)}">
      <div class="planning-site-row-main"><strong>${planningEscape(site.name)}</strong>${site.notes ? `<small>${planningEscape(site.notes)}</small>` : ''}</div>
      <div class="planning-site-row-actions">
        <button class="icon-button" type="button" title="Renomear" onclick="planningEditSiteRow(${Number(site.id)})"><i data-lucide="pencil"></i></button>
        <button class="icon-button" type="button" title="Excluir" onclick="planningDeleteSite(${Number(site.id)})"><i data-lucide="trash-2"></i></button>
      </div>
    </div>`).join('') : '<div class="planning-box-empty">Nenhum site cadastrado ainda.</div>';

  planningModal({
    title: 'Sites/locais do projeto',
    primary: 'Adicionar site',
    body: `<div class="planning-site-list">${rows}</div>
      <div class="planning-form-grid one" style="margin-top:16px;">${planningField('Novo site/local', 'planSiteName', '', 'placeholder="Ex: Bloco A"')}<label class="planning-field"><span>Observacoes</span><textarea id="planSiteNotes" rows="2"></textarea></label></div>`,
    onSave: async root => {
      const name = root.querySelector('#planSiteName').value.trim();
      if (!name) throw new Error('Informe o site/local.');
      await planningRequest(`/api/planning/projects/${_planningCurrent.id}/sites`, { method: 'POST', body: JSON.stringify({ name, notes: root.querySelector('#planSiteNotes').value.trim() }) });
      closePlanningModal(); showToast('Site adicionado.'); await planningRefreshCurrentProject();
    },
  });
}

function planningEditSiteRow(siteId) {
  const row = document.querySelector(`.planning-site-row[data-site-id="${Number(siteId)}"]`);
  const site = (_planningCurrent?.sites || []).find(item => Number(item.id) === Number(siteId));
  if (!row || !site) return;
  row.innerHTML = `
    <div class="planning-site-row-main planning-site-row-editing">
      <input id="planSiteEditName_${Number(siteId)}" value="${planningEscape(site.name)}" placeholder="Nome do site">
      <input id="planSiteEditNotes_${Number(siteId)}" value="${planningEscape(site.notes || '')}" placeholder="Observacoes">
    </div>
    <div class="planning-site-row-actions">
      <button class="icon-button" type="button" title="Salvar" onclick="planningSaveSiteRow(${Number(siteId)})"><i data-lucide="check"></i></button>
      <button class="icon-button" type="button" title="Cancelar" onclick="openPlanningSiteModal()"><i data-lucide="x"></i></button>
    </div>`;
  lucide.createIcons();
  row.querySelector('input')?.focus();
}

async function planningSaveSiteRow(siteId) {
  const name = document.getElementById(`planSiteEditName_${Number(siteId)}`)?.value.trim();
  const notes = document.getElementById(`planSiteEditNotes_${Number(siteId)}`)?.value.trim() || '';
  if (!name) { showToast('Informe o nome do site.', true); return; }
  try {
    await planningRequest(`/api/planning/projects/${_planningCurrent.id}/sites/${Number(siteId)}`, { method: 'PUT', body: JSON.stringify({ name, notes }) });
    showToast('Site atualizado.');
    await planningRefreshCurrentProject();
    openPlanningSiteModal();
  } catch (err) { showToast(err.message || 'Nao foi possivel salvar o site.', true); }
}

async function planningDeleteSite(siteId) {
  const site = (_planningCurrent?.sites || []).find(item => Number(item.id) === Number(siteId));
  if (!site) return;
  const inUse = (_planningCurrent?.devices || []).filter(item => Number(item.site_id) === Number(siteId)).length;
  const msg = inUse ? `${site.name} · ${inUse} equipamento(s) vinculado(s) ficarao sem site.` : site.name;
  if (!await showConfirm({ eyebrow: 'Planejamento', title: 'Excluir site/local?', msg, label: 'Excluir' })) return;
  try {
    await planningRequest(`/api/planning/projects/${_planningCurrent.id}/sites/${Number(siteId)}`, { method: 'DELETE' });
    showToast('Site excluido.');
    await planningRefreshCurrentProject();
    openPlanningSiteModal();
  } catch (err) { showToast(err.message || 'Nao foi possivel excluir o site.', true); }
}

function planningSiteOptions(selected = '') {
  return '<option value="">Sem site</option>' + (_planningCurrent?.sites || []).map(site => `<option value="${Number(site.id)}" ${String(selected) === String(site.id) ? 'selected' : ''}>${planningEscape(site.name)}</option>`).join('');
}

// Quem pode ser "pai" depende de quem e o "filho": ONU/ONT nasce dentro de
// uma caixa (sub-produto dela, nao um item avulso), enquanto a propria caixa
// so faz sentido pendurada em algo upstream (CTO/OLT/poste) -- nunca numa
// ONU, que e uma ponta, nao um ponto de distribuicao.
const PLANNING_PARENT_TYPES = {
  box: ['cto', 'olt', 'pole'],
  rack: ['cto', 'olt', 'pole'],
  // Arvore GPON de verdade: a porta da OLT liga num DIO (patch panel), que
  // segue pro splitter primario (tipo CTO, fica dentro da caixa de emenda).
  // O splitter tambem pode ligar direto na OLT quando nao ha DIO no meio.
  dio: ['olt'],
  // CTO entra na lista dos proprios pais possiveis: Caixa de Emenda
  // (splitter primario) e CTO (splitter secundario/terminal) sao o mesmo
  // device_type='cto' por baixo, entao um CTO/Caixa de Emenda pode alimentar
  // outro CTO em cascata (ver metadata.papel pra distinguir o papel de cada um).
  cto: ['olt', 'dio', 'cto'],
  // ONU/ONT nasce numa caixa de CFTV (poste/rua) ou num rack (predio/CTO
  // do cliente) -- os dois sao so lugares fisicos diferentes pra pendurar
  // a mesma ONU.
  onu: ['box', 'rack'],
  ont: ['box', 'rack'],
  // Switch/injetor ligam na porta ETH da ONU/ONT que esta na caixa, nao
  // direto na caixa -- a ONU e quem fisicamente entrega a rede pra dentro.
  switch: ['onu', 'ont'],
  injector: ['onu', 'ont'],
  camera: ['switch', 'injector'],
};
const PLANNING_DEFAULT_PARENT_TYPES = ['olt', 'onu', 'ont', 'switch', 'recorder', 'box', 'pole'];

function planningParentOptions(childType = '', selected = '', selfId = '') {
  const allowed = PLANNING_PARENT_TYPES[childType] || PLANNING_DEFAULT_PARENT_TYPES;
  return '<option value="">Sem equipamento pai</option>' + (_planningCurrent?.devices || []).filter(item => Number(item.id) !== Number(selfId) && allowed.includes(item.device_type)).map(item => `<option value="${Number(item.id)}" ${String(selected) === String(item.id) ? 'selected' : ''}>${planningEscape(item.name)} (${planningEscape(PLANNING_TYPES[item.device_type])})</option>`).join('');
}

async function loadPlanningCatalog() {
  if (_planningCatalog) return _planningCatalog;
  const data = await apiJson('/api/planning/catalog', { forceRefresh: true, cacheTtl: 0 });
  _planningCatalog = data?.items || [];
  return _planningCatalog;
}

function planningCatalogDatalists(item = {}) {
  return `<datalist id="planningManufacturerOptions"></datalist><datalist id="planningModelOptions"></datalist>
    <div class="planning-catalog-hint full"><i data-lucide="list-plus"></i><span>Escolha uma sugestao ou digite um fabricante/modelo novo. Ao salvar, o novo valor passa a fazer parte das sugestoes deste cliente.</span></div>`;
}

function refreshPlanningCatalogLists(root) {
  const type = planningEffectiveType(root) || 'camera';
  const manufacturer = root.querySelector('#planDeviceManufacturer')?.value.trim().toLowerCase() || '';
  const relevant = (_planningCatalog || []).filter(item => item.device_type === type || (type === 'ont' && item.device_type === 'onu'));
  const manufacturers = [...new Set(relevant.map(item => item.manufacturer).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const models = [...new Set(relevant.filter(item => !manufacturer || !item.manufacturer || item.manufacturer.toLowerCase() === manufacturer).map(item => item.model).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const manufacturerList = root.querySelector('#planningManufacturerOptions');
  const modelList = root.querySelector('#planningModelOptions');
  if (manufacturerList) manufacturerList.innerHTML = manufacturers.map(value => `<option value="${planningEscape(value)}"></option>`).join('');
  if (modelList) modelList.innerHTML = models.map(value => `<option value="${planningEscape(value)}"></option>`).join('');
}

// Tipos que o usuario ja aprovou pro fluxo novo de "Adicionar". Vai crescendo
// conforme cada etapa for liberada -- nao adicione tipo aqui sem pedir.
// "onu" representa o cartao unico ONU/ONT; o campo Modo dentro do cartao
// decide qual das duas funcoes se aplica (nao sao duas opcoes de Tipo).
const PLANNING_ADDABLE_TYPES = ['box', 'rack', 'dio', 'onu', 'switch', 'injector', 'camera', 'emenda', 'cto', 'recorder'];

// Lista as portas PoE do switch/injetor escolhido em "Ligado a" -- cada
// camera ocupa uma porta so dela, entao portas com outra camera aparecem
// desabilitadas (exceto a propria porta de quem esta sendo editado).
// Capacidade de porta de quem pode ser "pai com porta": switch/injetor
// contam PoE (poe_port_capacity); ONU/ONT contam portas ETH (eth_port_capacity).
function planningParentPortCapacity(parent) {
  if (!parent) return 0;
  if (['switch', 'injector'].includes(parent.device_type)) return planningPoeCapacity(parent.metadata || {});
  if (['onu', 'ont'].includes(parent.device_type)) return Number(parent.metadata?.eth_port_capacity || 1);
  // CTO e um splitter optico: cada porta de saida atende uma caixa/cliente.
  // Sem reserva de uplink (nao e PoE) -- todas as portas contam.
  if (parent.device_type === 'cto') return Number(parent.metadata?.port_capacity || 8);
  // DIO e um patch panel: cada porta liga (1 pra 1) num splitter primario/CTO.
  if (parent.device_type === 'dio') return Number(parent.metadata?.port_capacity || 12);
  // OLT: cada porta PON atende um CDO/CTO/caixa ligado direto nela.
  if (parent.device_type === 'olt') return Number(parent.metadata?.pon_port_capacity || 8);
  return 0;
}

function planningPortOptions(parentId, selectedPort, selfId) {
  const devices = _planningCurrent?.devices || [];
  const parent = devices.find(d => Number(d.id) === Number(parentId));
  if (!parent) return '<option value="">Escolha o equipamento em "Ligado a" primeiro</option>';
  const capacity = planningParentPortCapacity(parent);
  if (!capacity) return '<option value="">Esse equipamento nao tem portas definidas</option>';
  // IP da camera sendo editada (do form se ja aberto, senao do cadastro).
  // Cameras com o MESMO IP sao a mesma camera fisica (ex.: termica bi-espectro,
  // que ocupa 2 canais no NVR mas e 1 equipamento/1 cabo) e PODEM dividir a porta.
  const selfEl = document.getElementById('planDeviceIp');
  const selfDev = devices.find(d => Number(d.id) === Number(selfId));
  const selfIp = String((selfEl && selfEl.value) || selfDev?.ip || '').trim();
  const occupied = new Map();
  devices.forEach(d => {
    if (Number(d.parent_id) === Number(parentId) && Number(d.id) !== Number(selfId)) {
      const port = Number(d.metadata?.port_number);
      if (port) occupied.set(port, { name: d.name, ip: String(d.ip || '').trim() });
    }
  });
  let html = '<option value="">Selecione a porta</option>';
  for (let port = 1; port <= capacity; port++) {
    const occ = occupied.get(port);
    const sameCam = !!(occ && selfIp && occ.ip && occ.ip === selfIp);
    const blocked = !!occ && !sameCam;
    const selected = String(selectedPort) === String(port);
    let suffix = '';
    if (occ && sameCam) suffix = ` - mesma camera (${planningEscape(occ.name)})`;
    else if (occ) suffix = ` - ocupada (${planningEscape(occ.name)})`;
    html += `<option value="${port}" ${selected ? 'selected' : ''} ${blocked ? 'disabled' : ''}>Porta ${port}${suffix}</option>`;
  }
  return html;
}

function planningTypeOptionLabel(key) {
  return key === 'onu' ? 'ONU / ONT' : (PLANNING_TYPES[key] || key);
}

// ONT e so um "modo" do cartao ONU/ONT -- normaliza pra 'onu' na hora de
// escolher qual opcao do <select> Tipo fica marcada. CTO com
// papel=splitter_primario normaliza pra 'emenda' pelo mesmo motivo (opcao
// de Tipo separada pra "Caixa de Emenda", ver PLANNING_TYPES).
function planningTypeOptionValue(deviceType, metadata = {}) {
  if (deviceType === 'ont') return 'onu';
  if (deviceType === 'cto' && metadata?.papel === 'splitter_primario') return 'emenda';
  return deviceType;
}

async function openPlanningDeviceModal(deviceId = 0, defaults = {}) {
  if (!_planningCurrent) return;
  await loadPlanningCatalog();
  const isNew = !deviceId;
  const item = isNew
    ? { device_type: 'box', parent_id: null, metadata: {}, ...defaults }
    : ((_planningCurrent.devices || []).find(row => Number(row.id) === Number(deviceId)) || { device_type: 'box' });
  const groupType = planningTypeOptionValue(item.device_type, item.metadata);
  const typeOptions = isNew
    ? PLANNING_ADDABLE_TYPES.map(key => `<option value="${key}" ${groupType === key ? 'selected' : ''}>${planningEscape(planningTypeOptionLabel(key))}</option>`).join('')
    : Object.keys(PLANNING_TYPES).filter(key => key !== 'ont').map(key => `<option value="${key}" ${groupType === key ? 'selected' : ''}>${planningEscape(planningTypeOptionLabel(key))}</option>`).join('');
  const metadata = item.metadata || {};
  const modal = planningModal({
    title: item.id ? 'Editar equipamento planejado' : `Adicionar ${planningEscape(planningTypeOptionLabel(groupType) || 'equipamento')}`, wide: true,
    body: `<div class="planning-form-grid">
      <label class="planning-field"><span>Tipo</span><select id="planDeviceType">${typeOptions}</select></label>
      ${planningField('Nome/titulo', 'planDeviceName', item.name, 'placeholder="01 - ENTRADA"')}
      <label class="planning-field"><span>Ligado a</span><select id="planDeviceParent">${planningParentOptions(item.device_type, item.parent_id, item.id)}</select></label>
      <label class="planning-field" id="planDeviceParentPortField"><span>Porta no equipamento pai</span><select id="planDeviceParentPort">${planningPortOptions(item.parent_id, metadata.port_number, item.id)}</select></label>
      <label class="planning-field" id="planDeviceOnuModeField"><span>Modo</span><select id="planDeviceOnuMode">
        <option value="onu" ${item.device_type !== 'ont' ? 'selected' : ''}>ONU - bridge transparente (sem IP)</option>
        <option value="ont" ${item.device_type === 'ont' ? 'selected' : ''}>ONT - roteador/VEIP (com IP, gerenciavel)</option>
      </select></label>
      <label class="planning-field" id="planDeviceOnuPortsField"><span>Quantidade de portas ETH</span><select id="planDeviceOnuPorts">${[1, 2, 4].map(n => `<option value="${n}" ${Number(metadata.eth_port_capacity || 1) === n ? 'selected' : ''}>${n} porta${n === 1 ? '' : 's'} ETH</option>`).join('')}</select></label>
      <label class="planning-field" id="planDeviceSwitchModeField"><span>Modo</span><select id="planDeviceSwitchMode">
        <option value="normal" ${metadata.switch_mode !== 'smart' ? 'selected' : ''}>Normal - sem gerenciamento (bridge, sem IP)</option>
        <option value="smart" ${metadata.switch_mode === 'smart' ? 'selected' : ''}>Smart - gerenciavel (com IP)</option>
      </select></label>
      <label class="planning-field" id="planDeviceSwitchPortsField"><span>Quantidade de portas</span><select id="planDeviceSwitchPorts">${[4, 5, 8, 16, 24, 48].map(n => `<option value="${n}" ${Number(metadata.port_capacity || 5) === n ? 'selected' : ''}>${n} portas</option>`).join('')}</select></label>
      <label class="planning-field" id="planDeviceCtoPortsField"><span>Quantidade de portas</span><select id="planDeviceCtoPorts">${[1, 2, 4, 8, 12, 16, 24].map(n => `<option value="${n}" ${Number(metadata.port_capacity || 8) === n ? 'selected' : ''}>${n} portas</option>`).join('')}</select></label>
      <label class="planning-field" id="planDeviceOltPortsField"><span>Quantidade de portas PON</span><select id="planDeviceOltPorts">${[1, 2, 4, 8, 16, 32].map(n => `<option value="${n}" ${Number(metadata.pon_port_capacity || 8) === n ? 'selected' : ''}>${n} porta${n === 1 ? '' : 's'} PON</option>`).join('')}</select></label>
      ${planningField('IP planejado', 'planDeviceIp', item.ip, 'placeholder="10.10.20.1"', 'id="planDeviceIpField"')}
      ${planningField('MAC', 'planDeviceMac', metadata.mac || '', 'placeholder="AA:BB:CC:DD:EE:FF"', 'id="planDeviceMacField"')}
      ${planningField('VLAN', 'planDeviceVlan', metadata.vlan || '', 'placeholder="Default ou numero (ex: 10)"', 'id="planDeviceVlanField"')}
      <label class="planning-field" id="planDeviceSiteField"><span>Site/local</span><select id="planDeviceSite">${planningSiteOptions(item.site_id)}</select></label>
      <label class="planning-field" id="planDeviceCaboOpticoField"><span>Cabo optico</span><select id="planDeviceCaboOptico">
        <option value="">Selecione o cabo</option>
        ${[2, 4, 6, 8, 12, 24, 36, 48, 72, 96, 144].map(n => `<option value="${n}" ${Number(metadata.total_fo) === n ? 'selected' : ''}>${n} FO</option>`).join('')}
      </select></label>
      <div class="planning-field full" id="planDeviceSplitterFibersField">
        <span>Fibras do cabo neste trecho</span>
        <div class="planning-splitter-fibers">
          <div class="planning-fiber-row"><span class="planning-fiber-row-label" id="planDeviceFibrasChegamLabel">Chegam aqui</span><div id="planDeviceFibrasChegam" class="planning-fiber-chips"></div></div>
          <div class="planning-fiber-row"><span class="planning-fiber-row-label">Fundidas aqui (marque as usadas nesta caixa)</span><div id="planDeviceFibrasFundidas" class="planning-fiber-checks"></div></div>
          <div class="planning-fiber-row"><span class="planning-fiber-row-label">Continuam daqui pra frente (livres pro proximo ponto)</span><div id="planDeviceFibrasContinuam" class="planning-fiber-chips"></div></div>
        </div>
      </div>
      <div class="planning-field full" id="planDeviceCtoOcupacaoField">
        <span>Ocupacao das portas (clientes/caixas CFTV)</span>
        <div id="planDeviceFibrasSaidaGrid" class="planning-splitter-output-grid"></div>
      </div>
      ${planningField('Fabricante', 'planDeviceManufacturer', item.manufacturer, 'list="planningManufacturerOptions" placeholder="Escolha ou digite um novo"')}
      ${planningField('Modelo', 'planDeviceModel', item.model, 'list="planningModelOptions" placeholder="Escolha ou digite um novo"')}
      ${planningField('PON', 'planDevicePon', item.pon, 'placeholder="1"', 'id="planDevicePonField"')}
      ${planningField('Posicao ONU', 'planDeviceOnu', item.onu_position, 'placeholder="4"', 'id="planDeviceOnuField"')}
      ${planningField('Serial', 'planDeviceSerial', metadata.serial || '', 'placeholder="Serial da ONU/ONT"', 'id="planDeviceSerialField"')}
      ${planningField('Patrimonio', 'planDevicePatrimonio', metadata.patrimonio || '', 'placeholder="Numero do patrimonio"')}
      ${planningField('Percurso viario ate o equipamento pai (m)', 'planDeviceRoute', metadata.route_distance_m ?? '', 'type="number" min="0" step="1" placeholder="Ex: 45"', 'id="planDeviceRouteField"')}
      ${planningField('Coordenadas', 'planDeviceCoords', (item.latitude != null && item.longitude != null) ? `${item.latitude}, ${item.longitude}` : '', 'placeholder="-9.750000, -36.660000"')}
      ${planningField('Imagem de referencia', 'planDeviceImage', item.reference_image_url, 'placeholder="https://..."')}
      <label class="planning-field full"><span>Observacoes</span><textarea id="planDeviceNotes" rows="3">${planningEscape(item.notes || '')}</textarea></label>
      ${planningCatalogDatalists(item)}
    </div>`,
    onSave: async root => {
      const payload = planningDevicePayload(root, item.metadata || {});
      if (!payload.name) throw new Error('Informe o nome do equipamento.');
      const path = `/api/planning/projects/${_planningCurrent.id}/devices${item.id ? `/${item.id}` : ''}`;
      await planningRequest(path, { method: item.id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
      _planningCatalog = null;
      closePlanningModal(); showToast(item.id ? 'Equipamento atualizado.' : 'Equipamento adicionado.'); await planningRefreshCurrentProject();
    },
  });
  const refreshParent = () => {
    const parentSelect = modal.querySelector('#planDeviceParent');
    if (!parentSelect) return;
    const current = parentSelect.value;
    parentSelect.innerHTML = planningParentOptions(planningEffectiveType(modal), current, item.id);
  };
  const refreshParentPort = () => {
    const portSelect = modal.querySelector('#planDeviceParentPort');
    const portField = modal.querySelector('#planDeviceParentPortField');
    if (!portSelect || !portField) return;
    const parentId = modal.querySelector('#planDeviceParent')?.value || '';
    const parent = (_planningCurrent?.devices || []).find(d => Number(d.id) === Number(parentId));
    // CTO/Caixa de Emenda nao usam mais porta pra saber qual fibra chega --
    // isso agora e resolvido pelo modelo de fundida/continua (ver
    // refreshSplitterFibers), entao o campo de porta do pai some pra esses
    // dois tipos, mesmo que o pai tenha portas.
    const type = modal.querySelector('#planDeviceType')?.value || '';
    const hide = ['cto', 'emenda'].includes(type) || planningParentPortCapacity(parent) <= 0;
    portField.classList.toggle('hidden', hide);
    const current = portSelect.value;
    portSelect.innerHTML = planningPortOptions(parentId, current, item.id);
  };
  // Modelo de cabo passando por pontos de emenda: cada CDO/CTO herda as
  // fibras que "chegam" do equipamento pai (ou do cabo optico inteiro, se
  // for a raiz sem pai), marca quais foram fundidas (usadas) aqui, e o
  // resto "continua" automaticamente pro proximo ponto -- sem porta fixa,
  // pois um mesmo trecho pode alimentar varias CDOs/CTOs em sequencia.
  let firstSplitterBuild = true;
  const refreshSplitterFibers = () => {
    const type = modal.querySelector('#planDeviceType')?.value || '';
    if (['cto', 'emenda'].includes(type)) {
      const totalFo = Number(modal.querySelector('#planDeviceCaboOptico')?.value) || 12;
      const parentId = modal.querySelector('#planDeviceParent')?.value || '';
      const parentDevice = (_planningCurrent?.devices || []).find(d => Number(d.id) === Number(parentId));
      // Caixa de Emenda/CDO da sequencia ao cabo -- herda o que "continua"
      // no pai. CTO e terminal -- ela "entra na rua", entao herda o que foi
      // FUNDIDO no pai (a fibra especifica puxada pra area local dela), nao
      // o que segue adiante pro proximo ponto da cascata.
      const parentField = type === 'cto' ? 'cor_fibra' : 'fibras_mencionadas';
      const parentSource = Array.isArray(parentDevice?.metadata?.[parentField]) ? parentDevice.metadata[parentField] : null;
      // Sem pai (ou pai fora da cascata de fibra): chegam aqui todas as
      // cores do cabo escolhido -- e a raiz que recebe o tronco inteiro.
      const fibrasChegam = parentSource || PLANNING_FIBER_COLORS.slice(0, totalFo).map(c => c.toLowerCase());

      const chegamEl = modal.querySelector('#planDeviceFibrasChegam');
      const chegamLabelEl = modal.querySelector('#planDeviceFibrasChegamLabel');
      const fundidasEl = modal.querySelector('#planDeviceFibrasFundidas');
      const continuamEl = modal.querySelector('#planDeviceFibrasContinuam');
      const chip = (c, extraClass = '') => `<span class="planning-fiber-chip ${extraClass}">${c.charAt(0).toUpperCase() + c.slice(1)}</span>`;

      if (chegamLabelEl) {
        chegamLabelEl.textContent = !parentDevice
          ? 'Chegam aqui (cabo optico inteiro, sem equipamento pai)'
          : type === 'cto'
            ? `Chegam aqui (fundidas na ${parentDevice.name})`
            : `Chegam aqui (continuam da ${parentDevice.name})`;
      }
      if (chegamEl) chegamEl.innerHTML = fibrasChegam.length ? fibrasChegam.map(c => chip(c)).join('') : '<span class="planning-fiber-empty">Nenhuma (defina o equipamento pai ou o cabo optico)</span>';

      const prevFundidas = firstSplitterBuild
        ? (Array.isArray(metadata.cor_fibra) ? metadata.cor_fibra : [])
        : [...(fundidasEl?.querySelectorAll('input[type=checkbox]:checked') || [])].map(el => el.value);
      if (fundidasEl) {
        fundidasEl.innerHTML = fibrasChegam.map(c => `<label class="planning-fiber-check"><input type="checkbox" value="${c}" ${prevFundidas.includes(c) ? 'checked' : ''}> ${c.charAt(0).toUpperCase() + c.slice(1)}</label>`).join('')
          || '<span class="planning-fiber-empty">Nada disponivel pra fundir aqui</span>';
        fundidasEl.querySelectorAll('input[type=checkbox]').forEach(el => el.addEventListener('change', refreshSplitterFibers));
      }

      const fundidasAgora = fundidasEl ? [...fundidasEl.querySelectorAll('input[type=checkbox]:checked')].map(el => el.value) : prevFundidas;
      const fibrasContinuam = fibrasChegam.filter(c => !fundidasAgora.includes(c));
      if (continuamEl) continuamEl.innerHTML = fibrasContinuam.length ? fibrasContinuam.map(c => chip(c, 'is-free')).join('') : '<span class="planning-fiber-empty">Nenhuma -- tudo foi fundido aqui</span>';
    }

    // CTO terminal: cada porta do splitter local vai pra um cliente/Caixa
    // de CFTV, nao pra outra cor de fibra -- isso ja e modelado pela
    // propria caixa (device "box") escolhendo esta CTO em "Ligado a" + a
    // porta, aqui so mostra (leitura) quais portas ja tem caixa ligada.
    const grid = modal.querySelector('#planDeviceFibrasSaidaGrid');
    if (grid && type === 'cto') {
      const portCapacity = Number(modal.querySelector('#planDeviceCtoPorts')?.value) || 8;
      const boxesByPort = new Map();
      (_planningCurrent?.devices || []).forEach(d => {
        if (d.device_type === 'box' && Number(d.parent_id) === Number(item.id) && d.metadata?.port_number) {
          boxesByPort.set(Number(d.metadata.port_number), d.name);
        }
      });
      let html = '';
      for (let port = 1; port <= portCapacity; port++) {
        const boxName = boxesByPort.get(port);
        html += `<div class="planning-field"><span>Saida ${port}</span><div class="planning-splitter-readonly ${boxName ? 'is-used' : ''}">${boxName ? planningEscape(boxName) : 'Livre'}</div></div>`;
      }
      grid.innerHTML = html;
    }
    firstSplitterBuild = false;
  };
  const refreshAll = () => { refreshPlanningCatalogLists(modal); refreshPlanningDeviceFields(modal); refreshParent(); refreshParentPort(); refreshSplitterFibers(); };
  refreshAll();
  modal.querySelector('#planDeviceType')?.addEventListener('change', refreshAll);
  modal.querySelector('#planDeviceOnuMode')?.addEventListener('change', refreshAll);
  modal.querySelector('#planDeviceSwitchMode')?.addEventListener('change', refreshAll);
  modal.querySelector('#planDeviceParent')?.addEventListener('change', () => { refreshParentPort(); refreshSplitterFibers(); });
  modal.querySelector('#planDeviceParentPort')?.addEventListener('change', refreshSplitterFibers);
  modal.querySelector('#planDeviceCtoPorts')?.addEventListener('change', refreshSplitterFibers);
  modal.querySelector('#planDeviceCaboOptico')?.addEventListener('change', refreshSplitterFibers);
  modal.querySelector('#planDeviceManufacturer')?.addEventListener('input', () => refreshPlanningCatalogLists(modal));
}

function planningDevicePayload(root, metadata = {}) {
  const value = id => root.querySelector(`#${id}`)?.value?.trim() || '';
  const nextMetadata = { ...metadata };
  const serial = value('planDeviceSerial'); const mac = value('planDeviceMac');
  if (serial) nextMetadata.serial = serial; else delete nextMetadata.serial;
  if (mac) nextMetadata.mac = mac; else delete nextMetadata.mac;
  const vlan = value('planDeviceVlan');
  if (vlan) nextMetadata.vlan = vlan; else delete nextMetadata.vlan;
  // Patrimonio se aplica a qualquer equipamento (CTO ate camera), entao nao
  // depende de tipo -- so guarda o que foi preenchido.
  const patrimonio = value('planDevicePatrimonio');
  if (patrimonio) nextMetadata.patrimonio = patrimonio; else delete nextMetadata.patrimonio;
  const routeDistance = value('planDeviceRoute');
  if (routeDistance) nextMetadata.route_distance_m = Number(routeDistance); else delete nextMetadata.route_distance_m;
  // "emenda" (Caixa de Emenda) e so uma opcao de Tipo -- normaliza pra 'cto'
  // aqui igual planningEffectiveType ja faz, mas guarda o rawType original
  // pra decidir o papel (splitter primario vs secundario/terminal).
  const rawType = value('planDeviceType');
  const deviceType = rawType === 'emenda' ? 'cto' : rawType;
  if (deviceType === 'onu') {
    nextMetadata.eth_port_capacity = Math.max(1, Number(value('planDeviceOnuPorts')) || 1);
  } else {
    delete nextMetadata.eth_port_capacity;
  }
  if (['cto', 'dio'].includes(deviceType)) {
    const caboOptico = value('planDeviceCaboOptico');
    if (caboOptico) nextMetadata.total_fo = Number(caboOptico); else delete nextMetadata.total_fo;
  } else {
    delete nextMetadata.total_fo;
  }
  if (deviceType === 'cto') {
    // Fundidas aqui (checkboxes marcados) + o que continua livre pro proximo
    // ponto da cascata (chegam menos fundidas). Mesmos nomes de campo que o
    // balao do KMZ ja usa: cor_fibra = fundida aqui, fibras_mencionadas =
    // continua livre a partir daqui.
    const fundidas = [...root.querySelectorAll('#planDeviceFibrasFundidas input[type=checkbox]:checked')].map(el => el.value);
    if (fundidas.length) nextMetadata.cor_fibra = fundidas; else delete nextMetadata.cor_fibra;
    const parentId = value('planDeviceParent');
    const parentDevice = (_planningCurrent?.devices || []).find(d => Number(d.id) === Number(parentId));
    // CTO terminal herda o que foi FUNDIDO no pai (entra na rua); Caixa de
    // Emenda/CDO herda o que CONTINUA no pai (segue a cascata do cabo).
    const parentField = rawType === 'cto' ? 'cor_fibra' : 'fibras_mencionadas';
    const parentSource = Array.isArray(parentDevice?.metadata?.[parentField]) ? parentDevice.metadata[parentField] : null;
    const totalFo = Number(value('planDeviceCaboOptico')) || 0;
    const fibrasChegam = parentSource || (totalFo ? PLANNING_FIBER_COLORS.slice(0, totalFo).map(c => c.toLowerCase()) : []);
    const fibrasContinuam = fibrasChegam.filter(c => !fundidas.includes(c));
    if (fibrasContinuam.length) nextMetadata.fibras_mencionadas = fibrasContinuam; else delete nextMetadata.fibras_mencionadas;
  } else {
    delete nextMetadata.cor_fibra;
    delete nextMetadata.fibras_mencionadas;
  }
  if (deviceType === 'switch') {
    nextMetadata.switch_mode = value('planDeviceSwitchMode') || 'normal';
    // 1 porta reservada pro uplink (liga na ONU/ONT) -- so as demais contam
    // como capacidade real pra camera, igual a regra da caixa GPON.
    const portCapacity = Math.max(1, Number(value('planDeviceSwitchPorts')) || 5);
    const uplinkPorts = portCapacity > 1 ? 1 : 0;
    nextMetadata.port_capacity = portCapacity;
    nextMetadata.poe_port_capacity = Math.max(1, portCapacity - uplinkPorts);
    nextMetadata.uplink_ports = uplinkPorts;
  } else if (deviceType === 'injector') {
    // Injetor PoE e sempre porta unica -- nao ha uplink separado a reservar.
    delete nextMetadata.switch_mode;
    nextMetadata.port_capacity = 1; nextMetadata.poe_port_capacity = 1; nextMetadata.uplink_ports = 0;
  } else if (deviceType === 'cto' || deviceType === 'dio') {
    // CTO e splitter optico -- todas as portas de saida contam, sem reserva
    // de uplink (nao e PoE). DIO e patch panel -- mesma logica de porta
    // simples (uma ponta por porta, sem reserva de uplink).
    delete nextMetadata.switch_mode; delete nextMetadata.poe_port_capacity; delete nextMetadata.uplink_ports;
    nextMetadata.port_capacity = Math.max(1, Number(value('planDeviceCtoPorts')) || (deviceType === 'dio' ? 12 : 8));
    delete nextMetadata.pon_port_capacity;
    // Papel so existe pra CTO: distingue splitter primario (Caixa de Emenda,
    // escolhida direto no Tipo) do splitter secundario/terminal (CTO comum).
    if (deviceType === 'cto') nextMetadata.papel = rawType === 'emenda' ? 'splitter_primario' : 'splitter_terminal';
    else delete nextMetadata.papel;
  } else if (deviceType === 'olt') {
    // OLT: portas PON, cada uma atende um CDO/CTO/caixa ligado direto nela.
    delete nextMetadata.switch_mode; delete nextMetadata.port_capacity;
    delete nextMetadata.poe_port_capacity; delete nextMetadata.uplink_ports;
    nextMetadata.pon_port_capacity = Math.max(1, Number(value('planDeviceOltPorts')) || 8);
  } else {
    delete nextMetadata.switch_mode; delete nextMetadata.port_capacity;
    delete nextMetadata.poe_port_capacity; delete nextMetadata.uplink_ports;
    delete nextMetadata.pon_port_capacity;
  }
  // A visibilidade do campo "Porta no equipamento pai" ja depende do pai
  // escolhido ter portas (ver refreshParentPort), entao aqui basta ler o
  // valor -- se nao se aplicar, o campo simplesmente esta vazio.
  const parentPort = value('planDeviceParentPort');
  if (parentPort) nextMetadata.port_number = Number(parentPort); else delete nextMetadata.port_number;
  // Aceita "lat, lon" colado direto do Google Maps num campo so, em vez de
  // duas caixas separadas -- tambem aceita so espaco entre os numeros.
  const coordsParts = value('planDeviceCoords').split(/[,\s]+/).filter(Boolean);
  const lat = Number(coordsParts[0]); const lon = Number(coordsParts[1]);
  return {
    device_type: planningEffectiveType(root), name: value('planDeviceName'), ip: value('planDeviceIp'),
    site_id: value('planDeviceSite') || null, manufacturer: value('planDeviceManufacturer'), model: value('planDeviceModel'),
    parent_id: value('planDeviceParent') || null, pon: value('planDevicePon'), onu_position: value('planDeviceOnu'),
    latitude: Number.isFinite(lat) ? lat : null, longitude: Number.isFinite(lon) ? lon : null,
    reference_image_url: value('planDeviceImage'), notes: value('planDeviceNotes'), metadata: nextMetadata, status: 'planned',
  };
}

function openPlanningGenerateModal() {
  if (!_planningCurrent) return;
  planningModal({
    title: 'Gerar equipamentos em lote', wide: true, primary: 'Gerar equipamentos',
    body: `<div class="planning-form-grid">
      <label class="planning-field"><span>Tipo</span><select id="planGenType">${Object.entries(PLANNING_TYPES).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select></label>
      <label class="planning-field"><span>Site/local</span><select id="planGenSite">${planningSiteOptions()}</select></label>
      ${planningField('IP inicial', 'planGenIp', '10.10.20.1', 'placeholder="10.10.20.1"')}
      ${planningField('Quantidade', 'planGenCount', '10', 'type="number" min="1" max="500"')}
      ${planningField('Numero inicial', 'planGenFirst', '1', 'type="number" min="0"')}
      ${planningField('Digitos', 'planGenDigits', '2', 'type="number" min="1" max="4"')}
      ${planningField('Padrao do nome', 'planGenTemplate', '{number} - CAMERA', 'placeholder="{number} - CAMERA PERIMETRAL"')}
      <label class="planning-field"><span>Ligado a</span><select id="planGenParent">${planningParentOptions()}</select></label>
      ${planningField('Fabricante', 'planGenManufacturer', '', 'placeholder="Intelbras"')}
      ${planningField('Modelo', 'planGenModel', '', 'placeholder="VIP 3230 B"')}
      ${planningField('Imagem ilustrativa do modelo', 'planGenImage', '', 'placeholder="https://..."')}
      ${planningField('PON', 'planGenPon', '', 'placeholder="Opcional"')}
    </div><div class="planning-info"><i data-lucide="info"></i><span>Use <strong>{number}</strong> para a sequencia com zeros, por exemplo 01, 02 e 03. Os IPs serao incrementados automaticamente.</span></div>`,
    onSave: async root => {
      const value = id => root.querySelector(`#${id}`).value.trim();
      const payload = { device_type: value('planGenType'), site_id: value('planGenSite') || null, start_ip: value('planGenIp'), count: Number(value('planGenCount')), first_number: Number(value('planGenFirst')), digits: Number(value('planGenDigits')), name_template: value('planGenTemplate'), parent_id: value('planGenParent') || null, manufacturer: value('planGenManufacturer'), model: value('planGenModel'), reference_image_url: value('planGenImage'), pon: value('planGenPon'), status: 'planned' };
      const data = await planningRequest(`/api/planning/projects/${_planningCurrent.id}/generate`, { method: 'POST', body: JSON.stringify(payload) });
      _planningCatalog = null;
      closePlanningModal(); showToast(`${data.count} equipamento(s) gerado(s).`); await planningRefreshCurrentProject();
    },
  });
}

async function openPlanningBoxModal() {
  if (!_planningCurrent) return;
  await loadPlanningCatalog();
  const modal = planningModal({
    eyebrow: 'Projeto de CFTV', title: 'Montar caixa de CFTV', wide: true, primary: 'Criar caixa e equipamentos',
    body: `<div class="planning-box-wizard">
      <section class="planning-wizard-section"><div class="planning-wizard-heading"><span>1</span><div><strong>Caixa e localizacao</strong><small>Todos os equipamentos e cameras nascem neste ponto.</small></div></div><div class="planning-form-grid">
        ${planningField('Nome da caixa', 'planBoxName', '', 'placeholder="CX-01 - ENTRADA"')}
        <label class="planning-field"><span>Site/local</span><select id="planBoxSite">${planningSiteOptions()}</select></label>
        ${planningField('Latitude', 'planBoxLat', '', 'placeholder="-9.750000"')}${planningField('Longitude', 'planBoxLon', '', 'placeholder="-36.660000"')}
      </div></section>
      <section class="planning-wizard-section"><div class="planning-wizard-heading"><span>2</span><div><strong>Rede optica dentro da caixa</strong><small>Uma ONU por padrao; adicione mais quando o projeto exigir.</small></div></div><div class="planning-form-grid">
        <label class="planning-field"><span>Terminal optico</span><select id="planBoxOnuType"><option value="onu">ONU</option><option value="ont">ONT</option></select></label>
        ${planningField('Quantidade', 'planBoxOnuCount', '1', 'type="number" min="1" max="4"')}
        ${planningField('Fabricante ONU/ONT', 'planBoxOnuManufacturer', '', 'list="planningBoxOnuManufacturers" placeholder="Escolha ou digite"')}
        ${planningField('Modelo ONU/ONT', 'planBoxOnuModel', '', 'list="planningBoxOnuModels" placeholder="Escolha ou digite"')}
        ${planningField('PON', 'planBoxPon', '', 'placeholder="1"')}${planningField('Posicao ONU', 'planBoxOnuPosition', '', 'placeholder="4"')}
        <label class="planning-check full"><input id="planBoxCto" type="checkbox"><span><strong>Incluir CTO nesta caixa</strong><small>Opcional. Tambem pode ser adicionada ou editada depois.</small></span></label>
        <div class="planning-cto-fields full hidden" id="planningCtoFields">${planningField('Nome da CTO', 'planBoxCtoName', '', 'placeholder="CTO-01"')}${planningField('Modelo/capacidade', 'planBoxCtoModel', '', 'list="planningBoxCtoModels" placeholder="CTO 1x8 ou CTO 1x16"')}</div>
      </div></section>
      <section class="planning-wizard-section"><div class="planning-wizard-heading"><span>3</span><div><strong>Alimentacao das cameras</strong><small>Use switch PoE ou injetor PoE quando nao houver switch.</small></div></div><div class="planning-form-grid">
        <label class="planning-field"><span>Equipamento</span><select id="planBoxDistributionType"><option value="switch">Switch PoE</option><option value="injector">Injetor PoE</option></select></label>
        ${planningField('Quantidade', 'planBoxDistributionCount', '1', 'type="number" min="1" max="4"')}
        ${planningField('Fabricante', 'planBoxDistributionManufacturer', '', 'list="planningBoxDistributionManufacturers" placeholder="Escolha ou digite"')}
        ${planningField('Modelo', 'planBoxDistributionModel', '', 'list="planningBoxDistributionModels" placeholder="Escolha ou digite"')}
        <label class="planning-field"><span>Portas por equipamento</span><select id="planBoxPorts"><option value="1">1 porta</option><option value="5" selected>5 portas</option><option value="8">8 portas</option><option value="16">16 portas</option><option value="24">24 portas</option></select></label>
        <div class="planning-capacity-card"><span>Capacidade da caixa</span><strong id="planBoxCapacity">5 cameras</strong><small id="planBoxAvailability">5 portas livres</small></div>
      </div></section>
      <section class="planning-wizard-section"><div class="planning-wizard-heading"><span>4</span><div><strong>Cameras que saem da caixa</strong><small>As coordenadas sao herdadas da caixa e podem ser alteradas individualmente.</small></div></div><div class="planning-form-grid">
        ${planningField('Quantidade de cameras', 'planBoxCameraCount', '5', 'type="number" min="0" max="100"')}
        ${planningField('IP inicial', 'planBoxCameraIp', '', 'placeholder="10.10.20.1"')}
        ${planningField('Numero inicial', 'planBoxCameraFirst', '1', 'type="number" min="0"')}
        ${planningField('Padrao dos nomes', 'planBoxCameraTemplate', '{number} - CAMERA', 'placeholder="{number} - CAMERA"')}
        ${planningField('Fabricante das cameras', 'planBoxCameraManufacturer', '', 'list="planningBoxCameraManufacturers" placeholder="Escolha ou digite"')}
        ${planningField('Modelo das cameras', 'planBoxCameraModel', '', 'list="planningBoxCameraModels" placeholder="Escolha ou digite"')}
      </div></section>
      <datalist id="planningBoxOnuManufacturers"></datalist><datalist id="planningBoxOnuModels"></datalist><datalist id="planningBoxCtoModels"></datalist>
      <datalist id="planningBoxDistributionManufacturers"></datalist><datalist id="planningBoxDistributionModels"></datalist><datalist id="planningBoxCameraManufacturers"></datalist><datalist id="planningBoxCameraModels"></datalist>
    </div>`,
    onSave: async root => {
      const value = id => root.querySelector(`#${id}`)?.value?.trim() || '';
      const payload = {
        box_name: value('planBoxName'), site_id: value('planBoxSite') || null, latitude: value('planBoxLat') || null, longitude: value('planBoxLon') || null,
        onu_type: value('planBoxOnuType'), onu_count: Number(value('planBoxOnuCount')), onu_manufacturer: value('planBoxOnuManufacturer'), onu_model: value('planBoxOnuModel'), pon: value('planBoxPon'), onu_position: value('planBoxOnuPosition'),
        include_cto: root.querySelector('#planBoxCto').checked, cto_name: value('planBoxCtoName'), cto_model: value('planBoxCtoModel'),
        distribution_type: value('planBoxDistributionType'), distribution_count: Number(value('planBoxDistributionCount')), distribution_manufacturer: value('planBoxDistributionManufacturer'), distribution_model: value('planBoxDistributionModel'), port_capacity: Number(value('planBoxPorts')),
        camera_count: Number(value('planBoxCameraCount')), camera_start_ip: value('planBoxCameraIp'), camera_first_number: Number(value('planBoxCameraFirst')), camera_name_template: value('planBoxCameraTemplate'), camera_manufacturer: value('planBoxCameraManufacturer'), camera_model: value('planBoxCameraModel'),
      };
      if (!payload.box_name) throw new Error('Informe o nome da caixa de CFTV.');
      const data = await planningRequest(`/api/planning/projects/${_planningCurrent.id}/assemble-gpon-box`, { method: 'POST', body: JSON.stringify(payload) });
      _planningCatalog = null; closePlanningModal(); showToast(`Caixa montada com ${data.count - 1} equipamento(s).`); await planningRefreshCurrentProject();
    },
  });
  const catalogFor = type => (_planningCatalog || []).filter(item => item.device_type === type || (type === 'ont' && item.device_type === 'onu'));
  const fill = (listId, values) => { const list = modal.querySelector(`#${listId}`); if (list) list.innerHTML = [...new Set(values.filter(Boolean))].sort().map(value => `<option value="${planningEscape(value)}"></option>`).join(''); };
  const refreshCatalog = (type, manufacturerId, manufacturerListId, modelListId) => {
    const rows = catalogFor(type); const manufacturer = modal.querySelector(`#${manufacturerId}`)?.value.trim().toLowerCase() || '';
    fill(manufacturerListId, rows.map(item => item.manufacturer)); fill(modelListId, rows.filter(item => !manufacturer || item.manufacturer.toLowerCase() === manufacturer).map(item => item.model));
  };
  const refreshCapacity = () => {
    const count = Number(modal.querySelector('#planBoxDistributionCount').value || 0);
    const portsPerUnit = Number(modal.querySelector('#planBoxPorts').value || 0);
    const isSwitch = modal.querySelector('#planBoxDistributionType').value === 'switch';
    const uplinkPerUnit = isSwitch && portsPerUnit > 1 ? 1 : 0;
    const poePerUnit = Math.max(1, portsPerUnit - uplinkPerUnit);
    const capacity = count * poePerUnit;
    const used = Number(modal.querySelector('#planBoxCameraCount').value || 0); modal.querySelector('#planBoxCapacity').textContent = `${capacity} camera${capacity === 1 ? '' : 's'}`;
    const free = capacity - used;
    const uplinkNote = uplinkPerUnit ? ` (${count * uplinkPerUnit} porta${count * uplinkPerUnit === 1 ? '' : 's'} reservada${count * uplinkPerUnit === 1 ? '' : 's'} p/ uplink)` : '';
    modal.querySelector('#planBoxAvailability').textContent = (free < 0 ? `${Math.abs(free)} acima da capacidade` : `${free} porta${free === 1 ? '' : 's'} PoE livre${free === 1 ? '' : 's'}`) + uplinkNote;
    modal.querySelector('.planning-capacity-card').classList.toggle('danger', used > capacity);
  };
  const refreshDistribution = () => refreshCatalog(modal.querySelector('#planBoxDistributionType').value, 'planBoxDistributionManufacturer', 'planningBoxDistributionManufacturers', 'planningBoxDistributionModels');
  const refreshOnu = () => refreshCatalog(modal.querySelector('#planBoxOnuType').value, 'planBoxOnuManufacturer', 'planningBoxOnuManufacturers', 'planningBoxOnuModels');
  refreshOnu(); refreshDistribution(); refreshCatalog('camera', 'planBoxCameraManufacturer', 'planningBoxCameraManufacturers', 'planningBoxCameraModels'); fill('planningBoxCtoModels', catalogFor('cto').map(item => item.model)); refreshCapacity();
  ['planBoxOnuType','planBoxOnuManufacturer'].forEach(id => modal.querySelector(`#${id}`)?.addEventListener('input', refreshOnu));
  ['planBoxDistributionType','planBoxDistributionManufacturer'].forEach(id => modal.querySelector(`#${id}`)?.addEventListener('input', refreshDistribution));
  modal.querySelector('#planBoxCameraManufacturer')?.addEventListener('input', () => refreshCatalog('camera', 'planBoxCameraManufacturer', 'planningBoxCameraManufacturers', 'planningBoxCameraModels'));
  ['planBoxDistributionType','planBoxDistributionCount','planBoxPorts','planBoxCameraCount'].forEach(id => modal.querySelector(`#${id}`)?.addEventListener('input', refreshCapacity));
  modal.querySelector('#planBoxCto')?.addEventListener('change', event => modal.querySelector('#planningCtoFields').classList.toggle('hidden', !event.target.checked));
}

function openPlanningCsvModal() {
  if (!_planningCurrent) return;
  const columns = ['tipo', 'nome', 'ip', 'site', 'fabricante', 'modelo', 'equipamento_pai', 'pon', 'onu', 'latitude', 'longitude', 'imagem', 'metadata', 'observacoes'];
  const modal = planningModal({
    title: 'Importar equipamentos por CSV', wide: true, primary: 'Importar CSV',
    body: `<div class="planning-csv-import">
      <input class="planning-file-input" id="planCsvFile" type="file" accept=".csv,text/csv">
      <label class="planning-upload-zone" for="planCsvFile">
        <span class="planning-upload-icon"><i data-lucide="file-up"></i></span>
        <span class="planning-upload-copy"><strong class="planning-upload-title">Escolha o arquivo CSV</strong><small class="planning-upload-meta">Clique para selecionar ou arraste o arquivo para esta area</small></span>
        <span class="planning-upload-action">Selecionar arquivo</span>
      </label>
      <section class="planning-csv-defaults">
        <div class="planning-csv-section-head"><div><strong>Valores de apoio</strong><small>Usados somente quando uma linha do CSV nao informar tipo ou site.</small></div><span>Opcional</span></div>
        <div class="planning-form-grid">
          <label class="planning-field"><span>Tipo padrao</span><select id="planCsvType">${Object.entries(PLANNING_TYPES).map(([key,label]) => `<option value="${key}">${label}</option>`).join('')}</select></label>
          <label class="planning-field"><span>Site padrao</span><select id="planCsvSite">${planningSiteOptions()}</select></label>
        </div>
      </section>
      <details class="planning-csv-columns">
        <summary><span><i data-lucide="columns-3"></i><strong>Estrutura aceita pelo importador</strong></span><small>${columns.length} colunas disponiveis</small></summary>
        <div class="planning-column-chips">${columns.map(column => `<code>${column}</code>`).join('')}</div>
      </details>
      <div class="planning-csv-note"><i data-lucide="git-branch"></i><span>Apenas <strong>nome</strong> e obrigatorio. A hierarquia e montada por <strong>equipamento_pai</strong>, mesmo quando o pai aparece depois no arquivo.</span></div>
    </div>`,
    onSave: async root => {
      const file = root.querySelector('#planCsvFile').files[0];
      if (!file) throw new Error('Escolha um arquivo CSV.');
      const preview = (await file.slice(0, 4096).text()).replace(/^\uFEFF/, '');
      const firstLine = preview.split(/\r?\n/, 1)[0] || '';
      const separator = [';', ',', '\t'].sort((a, b) => firstLine.split(b).length - firstLine.split(a).length)[0];
      const headers = firstLine.split(separator).map(value => value.trim().replace(/^"|"$/g, '').toLowerCase());
      if (!headers.some(value => ['nome', 'titulo'].includes(value))) {
        const isDistanceReport = headers.includes('caixa') && headers.includes('camera') && headers.some(value => value.includes('distancia'));
        throw new Error(isDistanceReport
          ? 'Este e o relatorio de distancias e nao pode ser importado como equipamento. Selecione o arquivo "proposta-caixas-cftv-telha-rotas-viarias.csv".'
          : 'CSV incompatível: falta a coluna obrigatoria "nome".');
      }
      const form = new FormData(); form.append('file', file);
      form.append('defaults_json', JSON.stringify({ device_type: root.querySelector('#planCsvType').value, site_id: root.querySelector('#planCsvSite').value || null, status: 'planned' }));
      const data = await planningMultipart(`/api/planning/projects/${_planningCurrent.id}/import-csv`, form);
      _planningCatalog = null;
      closePlanningModal();
      const typeFilter = document.getElementById('planningTypeFilter');
      if (typeFilter) typeFilter.value = 'box';
      showToast(`${data.imported} item(ns) importado(s)${data.errors?.length ? `; ${data.errors.length} linha(s) com erro` : ''}.`, !!data.errors?.length);
      await planningRefreshCurrentProject();
    },
  });
  const input = modal.querySelector('#planCsvFile');
  const zone = modal.querySelector('.planning-upload-zone');
  const save = modal.querySelector('[data-save]');
  save.disabled = true;
  const showFile = file => {
    if (!file) return;
    zone.classList.add('has-file');
    zone.querySelector('.planning-upload-title').textContent = file.name;
    zone.querySelector('.planning-upload-meta').textContent = `${Math.max(1, Math.round(file.size / 1024))} KB · pronto para importar`;
    zone.querySelector('.planning-upload-action').textContent = 'Trocar arquivo';
    save.disabled = false;
  };
  input.addEventListener('change', () => showFile(input.files[0]));
  ['dragenter', 'dragover'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.remove('dragging'); }));
  zone.addEventListener('drop', event => {
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    const transfer = new DataTransfer(); transfer.items.add(file); input.files = transfer.files; showFile(file);
  });
}

function openPlanningKmzModal() {
  if (!_planningCurrent) return;
  planningModal({
    title: 'Importar mapa KMZ', wide: true, primary: 'Importar mapa',
    body: `<div class="planning-form-grid">
      <label class="planning-field full"><span>Arquivo KMZ</span><input id="planKmzFile" type="file" accept=".kmz,application/vnd.google-earth.kmz"></label>
      <label class="planning-check full"><input id="planKmzCreateCameras" type="checkbox" checked><span><strong>Criar cameras planejadas para os pontos do KMZ</strong><small>Linhas e areas ficam apenas como desenho no mapa.</small></span></label>
      <label class="planning-field"><span>Site das cameras</span><select id="planKmzSite">${planningSiteOptions()}</select></label>
      ${planningField('Fabricante padrao', 'planKmzManufacturer', '', 'placeholder="Intelbras"')}
      ${planningField('Modelo padrao', 'planKmzModel', '', 'placeholder="VIP 3230 B"')}
      ${planningField('IP inicial opcional', 'planKmzIp', '', 'placeholder="10.10.20.1"')}
    </div><div class="planning-info"><i data-lucide="image"></i><span>Fotos de internet devem ser cadastradas como imagem ilustrativa do modelo, nunca como snapshot real.</span></div>`,
    onSave: async root => {
      const file = root.querySelector('#planKmzFile').files[0];
      if (!file) throw new Error('Escolha um arquivo KMZ.');
      const form = new FormData(); form.append('file', file);
      const imported = await planningMultipart('/api/kmz/import', form);
      const updated = { name: _planningCurrent.name, client_name: _planningCurrent.client_name, description: _planningCurrent.description, status: _planningCurrent.status, kmz_layer_id: imported.id };
      await planningRequest(`/api/planning/projects/${_planningCurrent.id}`, { method: 'PUT', body: JSON.stringify(updated) });
      let created = 0;
      if (root.querySelector('#planKmzCreateCameras').checked) {
        const layers = await apiJson('/api/kmz/import/layers?include_features=true', { forceRefresh: true, cacheTtl: 0 });
        const layer = (layers?.layers || []).find(item => item.id === imported.id);
        const points = (layer?.features || []).filter(feature => String(feature?.geometry?.type).toLowerCase() === 'point');
        let ipValue = root.querySelector('#planKmzIp').value.trim();
        let ipNumber = ipValue ? planningIpToNumber(ipValue) : null;
        const plannedPoints = [];
        for (let index = 0; index < points.length; index += 1) {
          const feature = points[index]; const coords = feature.geometry.coordinates || [];
          const payload = { device_type: 'camera', name: feature.properties?.name || `${String(index + 1).padStart(2, '0')} - CAMERA`, ip: ipNumber === null ? '' : planningNumberToIp(ipNumber + index), site_id: root.querySelector('#planKmzSite').value || null, manufacturer: root.querySelector('#planKmzManufacturer').value.trim(), model: root.querySelector('#planKmzModel').value.trim(), longitude: coords[0], latitude: coords[1], notes: feature.properties?.description || '', status: 'planned' };
          plannedPoints.push(payload);
        }
        if (plannedPoints.length) {
          const bulk = await planningRequest(`/api/planning/projects/${_planningCurrent.id}/devices/bulk`, { method: 'POST', body: JSON.stringify({ items: plannedPoints }) });
          created = Number(bulk.count || 0);
          _planningCatalog = null;
        }
      }
      closePlanningModal(); showToast(`Mapa importado${created ? ` e ${created} camera(s) criada(s)` : ''}.`); await loadPlanning(true);
    },
  });
}

function planningIpToNumber(ip) { return ip.split('.').reduce((value, part) => (value * 256) + Number(part), 0) >>> 0; }
function planningNumberToIp(value) { return [24,16,8,0].map(shift => (value >>> shift) & 255).join('.'); }

async function deletePlanningDevice(deviceId) {
  const item = (_planningCurrent?.devices || []).find(row => Number(row.id) === Number(deviceId));
  if (!item || !await showConfirm({ eyebrow: 'Projeto', title: 'Excluir equipamento planejado?', msg: item.name, label: 'Excluir' })) return;
  await planningRequest(`/api/planning/projects/${_planningCurrent.id}/devices/${deviceId}`, { method: 'DELETE' });
  showToast('Equipamento removido.'); await planningRefreshCurrentProject();
}

async function deletePlanningProject() {
  if (!_planningCurrent || !await showConfirm({ eyebrow: 'Projeto', title: 'Excluir projeto completo?', msg: `${_planningCurrent.name}. Sites e equipamentos planejados serao removidos.`, label: 'Excluir projeto' })) return;
  await planningRequest(`/api/planning/projects/${_planningCurrent.id}`, { method: 'DELETE' });
  showToast('Projeto removido.'); _planningCurrent = null; await loadPlanning(true);
}

async function renderPlanningMap() {
  const container = document.getElementById('planningMap');
  if (!container || !_planningCurrent || typeof L === 'undefined') return;
  if (!_planningMap) {
    _planningMap = L.map('planningMap', { zoomControl: true }).setView([-9.76, -36.67], 14);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 20, attribution: '&copy; OpenStreetMap' }).addTo(_planningMap);
  }
  if (_planningMapLayers) _planningMapLayers.remove();
  _planningMapLayers = L.layerGroup().addTo(_planningMap);
  _planningMarkers = {};
  const bounds = [];
  if (_planningCurrent.kmz_layer_id) {
    const data = await apiJson('/api/kmz/import/layers?include_features=true', { forceRefresh: true, cacheTtl: 0 });
    const layer = (data?.layers || []).find(item => item.id === _planningCurrent.kmz_layer_id);
    if (layer?.features?.length) {
      const geo = L.geoJSON({ type: 'FeatureCollection', features: layer.features }, { style: { color: '#5f3dc4', weight: 3, fillOpacity: .08 }, pointToLayer: (_feature, latlng) => L.circleMarker(latlng, { radius: 5, color: '#5f3dc4', fillOpacity: .7 }) }).addTo(_planningMapLayers);
      if (geo.getBounds().isValid()) bounds.push(...[geo.getBounds().getSouthWest(), geo.getBounds().getNorthEast()]);
    }
  }
  (_planningCurrent.devices || []).forEach(item => {
    const lat = Number(item.latitude); const lon = Number(item.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const color = item.device_type === 'camera' ? '#087f5b' : ['onu','ont'].includes(item.device_type) ? '#1971c2' : '#b86b00';
    const referenceImage = item.reference_image_url
      ? `<img class="planning-popup-image" src="${planningEscape(item.reference_image_url)}" alt="Imagem ilustrativa" loading="lazy"><small>Imagem ilustrativa do modelo</small>` : '';
    const marker = L.circleMarker([lat, lon], { radius: item.device_type === 'camera' ? 7 : 9, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1 })
      .bindPopup(`<div class="planning-popup">${referenceImage}<strong>${planningEscape(item.name)}</strong><span>${planningEscape(PLANNING_TYPES[item.device_type] || item.device_type)} · ${planningEscape(item.site_name || 'Sem site')}</span>${planningItemHasNoIp(item) ? '' : `<code>${planningEscape(item.ip || 'IP a definir')}</code>`}<span>${planningEscape([item.manufacturer,item.model].filter(Boolean).join(' / ') || 'Modelo a definir')}</span></div>`)
      .addTo(_planningMapLayers);
    _planningMarkers[item.id] = marker; bounds.push([lat, lon]);
  });
  document.getElementById('planningMapHint').textContent = bounds.length ? `${bounds.length} referencia(s) posicionada(s).` : 'Importe um KMZ ou informe coordenadas.';
  if (bounds.length) _planningMap.fitBounds(L.latLngBounds(bounds), { padding: [24, 24], maxZoom: 18 });
  setTimeout(() => _planningMap.invalidateSize(), 100);
}

function focusPlanningDevice(deviceId) {
  const marker = _planningMarkers[deviceId];
  if (!marker || !_planningMap) { openPlanningDeviceModal(deviceId); return; }
  const position = marker.getLatLng();
  _planningMap.flyTo(position, Math.max(_planningMap.getZoom(), 18), { duration: .45 });
  setTimeout(() => marker.openPopup(), 480);
}

async function exportPlanningKmz() {
  if (!_planningCurrent) return;
  const button = document.getElementById('btnPlanningExportKmz');
  if (button) button.disabled = true;
  try {
    const headers = {};
    if (_token) headers.Authorization = `Bearer ${_token}`;
    const response = await fetch(`${API_BASE}/api/planning/projects/${Number(_planningCurrent.id)}/export-kmz`, { headers, credentials: 'same-origin' });
    if (!response.ok) {
      const raw = await response.text();
      let message = raw;
      try { const data = JSON.parse(raw); message = data.detail || data.message || raw; } catch (_error) { /* resposta textual */ }
      throw new Error(message || 'Nao foi possivel exportar o KMZ.');
    }
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || `${_planningCurrent.name || 'projeto-cftv'}.kmz`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('KMZ do projeto exportado.');
  } catch (error) {
    showToast(error.message || 'Nao foi possivel exportar o KMZ.', true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function downloadPlanningNetworkPdf(cableConfig = null, triggerButton = null) {
  if (!_planningCurrent) return;
  const button = triggerButton || document.getElementById('btnPlanningNetworkPdf');
  if (button) button.disabled = true;
  try {
    const headers = {};
    if (_token) headers.Authorization = `Bearer ${_token}`;
    const params = new URLSearchParams();
    if (cableConfig) {
      if (Number.isFinite(cableConfig.routePercent)) params.set('route_margin_pct', cableConfig.routePercent);
      if (Number.isFinite(cableConfig.slackMeters)) params.set('slack_meters', cableConfig.slackMeters);
    }
    const query = params.toString();
    const response = await fetch(`${API_BASE}/api/planning/projects/${Number(_planningCurrent.id)}/network-document.pdf${query ? `?${query}` : ''}`, { headers, credentials: 'same-origin' });
    if (!response.ok) {
      const raw = await response.text();
      let message = raw;
      try { const data = JSON.parse(raw); message = data.detail || data.message || raw; } catch (_error) { /* resposta textual */ }
      throw new Error(message || 'Nao foi possivel gerar o documento de rede.');
    }
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || `${_planningCurrent.name || 'projeto-cftv'}-documento-rede.pdf`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('Documento de rede gerado em PDF.');
  } catch (error) {
    showToast(error.message || 'Nao foi possivel gerar o documento de rede.', true);
  } finally {
    if (button) button.disabled = false;
  }
}

function planningProposalGroups() {
  const groups = new Map();
  (_planningCurrent?.devices || []).forEach(item => {
    const key = [item.device_type, item.manufacturer || '', item.model || ''].join('|');
    if (!groups.has(key)) groups.set(key, { device_type: item.device_type, manufacturer: item.manufacturer || '', model: item.model || '', count: 0 });
    groups.get(key).count += 1;
  });
  const result = [...groups.values()];
  const cableQuote = planningLoadCableQuote();
  if (cableQuote?.spools > 0) result.push({
    device_type: 'network_cable', label: 'Cabo de rede CAT5e', manufacturer: 'Cabo para instalacao',
    model: `Caixa de 305 m · ${cableQuote.purchaseMeters.toFixed(0)} m calculados`, count: cableQuote.spools,
  });
  return result.sort((a, b) => (a.label || PLANNING_TYPES[a.device_type] || a.device_type).localeCompare(b.label || PLANNING_TYPES[b.device_type] || b.device_type));
}

function planningProposalGroupLabel(group) {
  return group.label || PLANNING_TYPES[group.device_type] || group.device_type;
}

function planningCurrency(value) {
  return Number(value || 0).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
}

function openPlanningProposalModal() {
  if (!_planningCurrent) return;
  const groups = planningProposalGroups();
  const cableQuote = planningLoadCableQuote();
  const boxes = (_planningCurrent.devices || []).filter(item => item.device_type === 'box').length;
  const cameras = (_planningCurrent.devices || []).filter(item => item.device_type === 'camera').length;
  const modal = planningModal({
    eyebrow: 'Documento para o cliente', title: 'Proposta e especificacoes', wide: true, primary: 'Gerar documento',
    body: `<div class="planning-proposal-form">
      <div class="planning-info"><i data-lucide="info"></i><span>Os quantitativos vieram do projeto. Preencha somente os valores que deseja apresentar; itens sem preco ficam como <strong>A definir</strong>.${cableQuote ? ` Cabos incluidos: <strong>${cableQuote.purchaseMeters.toFixed(0)} m / ${cableQuote.spools} caixa(s) de 305 m</strong>.` : ' Execute o calculo de cabos e use <strong>Incluir na proposta</strong> para adicionar esse material.'}</span></div>
      <div class="planning-form-grid">
        ${planningField('Validade da proposta', 'planProposalValidity', '15 dias')}
        ${planningField('Prazo estimado', 'planProposalDeadline', 'A definir apos aprovacao')}
        <label class="planning-field full"><span>Condicoes e observacoes comerciais</span><textarea id="planProposalNotes" rows="3" placeholder="Instalacao, garantia, forma de pagamento e itens nao inclusos."></textarea></label>
      </div>
      <div class="planning-proposal-table"><div class="planning-proposal-head"><span>Item / especificacao</span><span>Qtd.</span><span>Valor unitario</span></div>
        ${groups.map((group, index) => `<div class="planning-proposal-row"><span><strong>${planningEscape(planningProposalGroupLabel(group))}</strong><small>${planningEscape([group.manufacturer, group.model].filter(Boolean).join(' / ') || 'Fabricante e modelo a definir')}</small></span><strong>${group.count}</strong><label><span>R$</span><input id="planProposalPrice${index}" type="number" min="0" step="0.01" placeholder="0,00"></label></div>`).join('')}
      </div>
    </div>`,
    onSave: async root => {
      const prices = groups.map((_group, index) => Number(root.querySelector(`#planProposalPrice${index}`)?.value || 0));
      const validity = root.querySelector('#planProposalValidity').value.trim();
      const deadline = root.querySelector('#planProposalDeadline').value.trim();
      const notes = root.querySelector('#planProposalNotes').value.trim();
      const total = groups.reduce((sum, group, index) => sum + (group.count * prices[index]), 0);
      const rows = groups.map((group, index) => `<tr><td><strong>${planningEscape(planningProposalGroupLabel(group))}</strong><small>${planningEscape([group.manufacturer, group.model].filter(Boolean).join(' / ') || 'Fabricante e modelo a definir')}</small></td><td>${group.count}</td><td>${prices[index] ? planningCurrency(prices[index]) : 'A definir'}</td><td>${prices[index] ? planningCurrency(group.count * prices[index]) : 'A definir'}</td></tr>`).join('');
      const cableSummary = cableQuote ? `<p><strong>Dimensionamento dos cabos:</strong> ${cableQuote.installedMeters.toFixed(0)} m estimados para instalacao; ${cableQuote.purchaseMeters.toFixed(0)} m previstos para compra; ${cableQuote.spools} caixa(s) de 305 m; ${cableQuote.overLimit} trecho(s) para revisao.</p>` : '';
      const report = window.open('', '_blank');
      if (!report) throw new Error('O navegador bloqueou o documento. Libere pop-ups para o SightOps.');
      report.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${planningEscape(_planningCurrent.name)} - Proposta</title><style>body{margin:0;color:#17232b;font:14px Arial,sans-serif}.page{max-width:980px;margin:auto;padding:38px}.brand{color:#087f5b;font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}h1{margin:8px 0 4px;font-size:28px}h2{margin:28px 0 10px;font-size:18px}.muted,small{display:block;color:#667782}.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:22px 0}.summary div{padding:14px;border:1px solid #dce3e7;border-radius:8px}.summary span{display:block;color:#667782;font-size:11px;text-transform:uppercase}.summary strong{display:block;margin-top:5px;font-size:22px}table{width:100%;border-collapse:collapse}th,td{padding:11px 10px;border-bottom:1px solid #dce3e7;text-align:left}th{color:#667782;background:#f6f8f9;font-size:11px;text-transform:uppercase}td:nth-child(n+2),th:nth-child(n+2){text-align:right}.total{margin-top:16px;text-align:right;font-size:19px}.note{padding:14px;border:1px solid #b7decf;border-radius:8px;background:#effaf6;white-space:pre-wrap}.footer{margin-top:34px;padding-top:12px;border-top:1px solid #dce3e7;color:#667782;font-size:11px}@media print{.page{padding:18px}.no-print{display:none}}</style></head><body><main class="page"><div class="brand">SightOps &middot; Projeto de CFTV</div><h1>${planningEscape(_planningCurrent.name)}</h1><p class="muted">${planningEscape(_planningCurrent.client_name || 'Cliente nao informado')} &middot; ${planningEscape(_planningCurrent.description || 'Planejamento de infraestrutura de CFTV')}</p><section class="summary"><div><span>Sites</span><strong>${(_planningCurrent.sites || []).length}</strong></div><div><span>Caixas de CFTV</span><strong>${boxes}</strong></div><div><span>Cameras</span><strong>${cameras}</strong></div><div><span>Total de itens</span><strong>${(_planningCurrent.devices || []).length}</strong></div></section><h2>Escopo tecnico</h2><p>Projeto planejado com caixas de CFTV, infraestrutura optica, distribuicao PoE e cameras em coordenadas individuais. Os percursos e distancias devem ser validados em campo antes da execucao.</p>${cableSummary}<h2>Quantitativos e especificacoes</h2><table><thead><tr><th>Item</th><th>Quantidade</th><th>Valor unitario</th><th>Subtotal</th></tr></thead><tbody>${rows}</tbody></table><div class="total"><strong>Total dos itens precificados: ${planningCurrency(total)}</strong></div><h2>Condicoes</h2><div class="note"><strong>Validade:</strong> ${planningEscape(validity || 'A definir')}\n<strong>Prazo:</strong> ${planningEscape(deadline || 'A definir')}\n\n${planningEscape(notes || 'Valores, instalacao, garantia e forma de pagamento a definir.')}</div><div class="footer">Documento gerado pelo SightOps em ${new Date().toLocaleString('pt-BR')}. Itens planejados nao representam equipamentos instalados.</div><p class="no-print"><button onclick="window.print()">Imprimir ou salvar em PDF</button></p></main></body></html>`);
      report.document.close();
      closePlanningModal();
    },
  });
  return modal;
}

function planningDistanceMeters(lat1, lon1, lat2, lon2) {
  const radians = value => Number(value) * Math.PI / 180;
  const earth = 6371000;
  const dLat = radians(lat2 - lat1);
  const dLon = radians(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(radians(lat1)) * Math.cos(radians(lat2)) * Math.sin(dLon / 2) ** 2;
  return earth * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// Ancora do cabo = equipamento em que a camera esta fisicamente ligada.
// Antes so aceitava 'box'; agora aceita tambem switch/rack/injetor/CTO/DIO/ONU
// (a camera puxa cabo ate o equipamento pai, seja qual for -- num rack e comum
// a camera ligar direto no switch, sem uma caixa intermediaria).
const PLANNING_CABLE_ANCHORS = new Set(['box', 'switch', 'rack', 'injector', 'cto', 'dio', 'onu', 'ont', 'other']);
function planningCameraBox(camera, byId) {
  const visited = new Set();
  let current = camera;
  while (current?.parent_id && !visited.has(Number(current.parent_id))) {
    visited.add(Number(current.parent_id));
    current = byId.get(Number(current.parent_id));
    if (current && PLANNING_CABLE_ANCHORS.has(current.device_type)) return current;
  }
  return null;
}

function planningCableCalculation(config) {
  const devices = _planningCurrent?.devices || [];
  const byId = new Map(devices.map(item => [Number(item.id), item]));
  const rows = devices.filter(item => item.device_type === 'camera').map(camera => {
    const box = planningCameraBox(camera, byId);
    const coordinates = [camera.latitude, camera.longitude, box?.latitude, box?.longitude].map(Number);
    if (!box || coordinates.some(value => !Number.isFinite(value))) return { camera, box, error: !box ? 'Camera sem equipamento pai vinculado' : 'Coordenadas incompletas' };
    const straight = planningDistanceMeters(...coordinates);
    // Sem percurso viario medido a mao, usa a distancia aerea (caixa->camera)
    // como base automatica -- e assim que sempre funcionou; o campo manual e
    // so pra refinar quando tiver a medida real da rua.
    const manualRoute = Number(camera.metadata?.route_distance_m);
    const route = Number.isFinite(manualRoute) ? manualRoute : straight;
    const estimated = !Number.isFinite(manualRoute);
    // Slack fixo so faz sentido cobrindo a incerteza da estimativa por linha reta;
    // quando o percurso e medido a mao (manual), o valor informado ja e o percurso real.
    const installed = (route * (1 + config.routePercent / 100)) + (estimated ? config.slackMeters : 0);
    const purchase = installed * (1 + config.reservePercent / 100);
    return { camera, box, straight, route, installed, purchase, estimated, overLimit: installed > config.maxMeters };
  });
  return rows.sort((a, b) => planningNaturalCompare(a.box?.name, b.box?.name) || planningNaturalCompare(a.camera?.name, b.camera?.name));
}

function planningCableConfig(root) {
  const number = (id, fallback) => {
    const value = Number(root.querySelector(`#${id}`)?.value);
    return Number.isFinite(value) && value >= 0 ? value : fallback;
  };
  return {
    routePercent: number('planCableRoute', 15), slackMeters: number('planCableSlack', 5),
    reservePercent: number('planCableReserve', 10), maxMeters: number('planCableMax', 100), spoolMeters: 305,
  };
}

function planningCableQuoteKey() {
  return `sightops:planning:cables:${Number(_planningCurrent?.id || 0)}`;
}

function planningLoadCableQuote() {
  if (!_planningCurrent?.id) return null;
  try {
    const value = JSON.parse(localStorage.getItem(planningCableQuoteKey()) || sessionStorage.getItem(planningCableQuoteKey()) || 'null');
    return value && Number(value.projectId) === Number(_planningCurrent.id) ? value : null;
  } catch (_error) {
    return null;
  }
}

function planningSaveCableQuote(root) {
  const config = planningCableConfig(root);
  const rows = renderPlanningCableResults(root);
  const valid = rows.filter(row => !row.error);
  const invalid = rows.filter(row => row.error);
  if (!valid.length) throw new Error('Nenhum cabo valido foi calculado para adicionar ao orcamento.');
  if (invalid.length) throw new Error(`${invalid.length} camera(s) ainda estao sem percurso calculado. Corrija o projeto antes de adicionar os cabos ao orcamento.`);
  const installedMeters = valid.reduce((sum, row) => sum + row.installed, 0);
  const purchaseMeters = valid.reduce((sum, row) => sum + row.purchase, 0);
  const quote = {
    projectId: Number(_planningCurrent.id), calculatedAt: new Date().toISOString(), config,
    cameras: valid.length, installedMeters, purchaseMeters,
    spools: Math.ceil(purchaseMeters / config.spoolMeters), overLimit: valid.filter(row => row.overLimit).length,
  };
  localStorage.setItem(planningCableQuoteKey(), JSON.stringify(quote));
  sessionStorage.removeItem(planningCableQuoteKey());
  return quote;
}

function renderPlanningCableResults(root) {
  const target = root.querySelector('#planningCableResults');
  if (!target) return [];
  const config = planningCableConfig(root);
  const rows = planningCableCalculation(config);
  const valid = rows.filter(row => !row.error);
  const missing = rows.filter(row => row.error);
  const overLimit = valid.filter(row => row.overLimit);
  const totalInstalled = valid.reduce((sum, row) => sum + row.installed, 0);
  const totalPurchase = valid.reduce((sum, row) => sum + row.purchase, 0);
  const boxes = [...new Map(valid.map(row => [Number(row.box.id), row.box])).values()].sort((a, b) => planningNaturalCompare(a.name, b.name));
  const boxSections = boxes.map(box => {
    const items = valid.filter(row => Number(row.box.id) === Number(box.id)).sort((a, b) => planningNaturalCompare(a.camera.name, b.camera.name));
    const subtotal = items.reduce((sum, row) => sum + row.purchase, 0);
    return `<details class="planning-cable-box" open><summary><span><strong>${planningEscape(box.name)}</strong><small>${items.length} camera(s) &middot; ${planningEscape(box.site_name || 'Sem site')}</small></span><strong>${subtotal.toFixed(1)} m</strong></summary>
      <div class="planning-cable-table-head"><span>Camera</span><span>Aerea</span><span>Via ruas</span><span>Instalado</span><span>Para compra</span><span>Situacao</span></div>
      ${items.map(row => `<div class="planning-cable-row"><strong title="${planningEscape(row.camera.name)}">${planningEscape(row.camera.name)}</strong><span>${row.straight.toFixed(1)} m</span><span>${row.route.toFixed(1)} m${row.estimated ? ' (estimado)' : ''}</span><span>${row.installed.toFixed(1)} m</span><span>${row.purchase.toFixed(1)} m</span><span class="planning-cable-status ${row.overLimit ? 'danger' : ''}">${row.overLimit ? 'Revisar rota' : 'Dentro do limite'}</span></div>`).join('')}</details>`;
  }).join('');
  const warnings = [];
  if (overLimit.length) warnings.push(`${overLimit.length} trecho(s) ultrapassam ${config.maxMeters} m instalados. Considere reposicionar a caixa ou adicionar outra distribuicao.`);
  if (missing.length) {
    // Motivo real varia (sem caixa vinculada / sem coordenada / sem percurso
    // viario informado) -- agrupa por motivo em vez de um aviso generico so,
    // que confundia mostrando "falta vinculo" quando na verdade so faltava
    // preencher o percurso.
    const byReason = new Map();
    missing.forEach(row => byReason.set(row.error, (byReason.get(row.error) || 0) + 1));
    byReason.forEach((count, reason) => warnings.push(`${count} camera(s): ${reason}.`));
  }
  const emptyMessage = rows.length
    ? 'Nenhuma camera pronta pra calcular ainda -- veja os avisos acima.'
    : 'Nenhuma camera cadastrada neste projeto.';
  target.innerHTML = `<div class="planning-cable-summary"><div><span>Cameras calculadas</span><strong>${valid.length}</strong></div><div><span>Cabo instalado</span><strong>${totalInstalled.toFixed(0)} m</strong></div><div><span>Cabo para compra</span><strong>${totalPurchase.toFixed(0)} m</strong></div><div><span>Caixas de 305 m</span><strong>${Math.ceil(totalPurchase / config.spoolMeters)}</strong></div></div>${warnings.length ? `<div class="planning-cable-warning">${warnings.map(planningEscape).join('<br>')}</div>` : ''}${boxSections || `<div class="planning-cable-empty">${planningEscape(emptyMessage)}</div>`}`;
  lucide.createIcons();
  root._planningCableRows = rows;
  return rows;
}

function planningCsvValue(value) {
  const text = String(value ?? '');
  return `"${text.replace(/"/g, '""')}"`;
}

function downloadPlanningCableCsv(root) {
  const config = planningCableConfig(root);
  const rows = renderPlanningCableResults(root);
  const lines = [['caixa','camera','site','distancia_aerea_m','percurso_viario_m','percurso_estimado','cabo_instalado_m','cabo_compra_m','limite_m','situacao']];
  rows.forEach(row => lines.push([row.box?.name || '', row.camera.name || '', row.camera.site_name || '', row.straight?.toFixed(1) || '', row.route?.toFixed(1) || '', row.estimated ? 'sim' : 'nao', row.installed?.toFixed(1) || '', row.purchase?.toFixed(1) || '', config.maxMeters, row.error || (row.overLimit ? 'revisar rota' : 'dentro do limite')]));
  const content = '\ufeff' + lines.map(line => line.map(planningCsvValue).join(';')).join('\r\n');
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = `${String(_planningCurrent.name || 'projeto-cftv').replace(/[^a-z0-9_-]+/gi, '-')}-cabos.csv`; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  showToast('Calculo de cabos exportado.');
}

function openPlanningCableModal() {
  if (!_planningCurrent) return;
  const modal = planningModal({
    eyebrow: 'Dimensionamento', title: 'Calcular cabo UTP', wide: true, primary: 'Adicionar cabos ao orcamento',
    body: `<div class="planning-cable-tool">
      <div class="planning-cable-settings">
        ${planningField('Margem sobre a rota (%)', 'planCableRoute', '15', 'type="number" min="0" step="1"')}
        ${planningField('Folga tecnica por camera (m)', 'planCableSlack', '5', 'type="number" min="0" step="1"')}
        ${planningField('Reserva para compra (%)', 'planCableReserve', '10', 'type="number" min="0" step="1"')}
        ${planningField('Limite do trecho instalado (m)', 'planCableMax', '100', 'type="number" min="1" step="1"')}
      </div>
      <div class="planning-csv-note"><i data-lucide="route"></i><span><strong>Instalado</strong> = percurso pelas ruas + margem de passagem + folga. Sem percurso viario medido na camera, usa a distancia aerea (caixa-camera) como estimativa automatica -- preencha o percurso real na camera pra refinar. A reserva entra somente na compra.</span></div>
      <div class="planning-info"><i data-lucide="file-plus-2"></i><span>Depois de conferir as metragens, clique em <strong>Adicionar cabos ao orcamento</strong>. O item aparecera automaticamente em <strong>Gerar proposta</strong>, com a quantidade de caixas de 305 m.</span></div>
      <div class="planning-cable-results" id="planningCableResults"></div>
    </div>`,
    onSave: async root => {
      const quote = planningSaveCableQuote(root);
      closePlanningModal(); showToast(`${quote.spools} caixa(s) de cabo adicionadas ao orcamento.`);
    },
  });
  const footer = modal.querySelector('.planning-modal-footer');
  const saveButton = footer?.querySelector('[data-save]');
  if (footer && saveButton) {
    const download = document.createElement('button');
    download.type = 'button'; download.className = 'secondary-action';
    download.innerHTML = '<i data-lucide="download"></i> Baixar CSV';
    download.onclick = () => downloadPlanningCableCsv(modal);
    footer.insertBefore(download, saveButton);
    const pdfButton = document.createElement('button');
    pdfButton.type = 'button'; pdfButton.className = 'secondary-action';
    pdfButton.innerHTML = '<i data-lucide="file-text"></i> Gerar documento de rede';
    pdfButton.onclick = () => downloadPlanningNetworkPdf(planningCableConfig(modal), pdfButton);
    footer.insertBefore(pdfButton, saveButton);
  }
  ['planCableRoute', 'planCableSlack', 'planCableReserve', 'planCableMax'].forEach(id => modal.querySelector(`#${id}`)?.addEventListener('input', () => renderPlanningCableResults(modal)));
  renderPlanningCableResults(modal);
  lucide.createIcons();
}

function planningParentDevice(item, byId) {
  const parentId = Number(item?.parent_id);
  return Number.isFinite(parentId) && parentId ? byId.get(parentId) : null;
}

// Calcula, automaticamente, o trecho de fibra de cada DIO/CTO ate o
// equipamento pai (OLT, DIO ou outro CTO acima na arvore). Nao precisa
// escolher "a caixa de emenda principal" na mao -- percorrendo parent_id de
// cada no ja sai desenhando a rota inteira da arvore de splitters, do jeito
// que o projeto de origem (aereo/estimado, com percurso manual pra refinar)
// ja funciona pra camera->caixa.
function planningFiberCalculation(config) {
  const devices = _planningCurrent?.devices || [];
  const byId = new Map(devices.map(item => [Number(item.id), item]));
  const rows = devices.filter(item => ['cto', 'dio'].includes(item.device_type)).map(node => {
    const parent = planningParentDevice(node, byId);
    const coordinates = [node.latitude, node.longitude, parent?.latitude, parent?.longitude].map(Number);
    if (!parent || coordinates.some(value => !Number.isFinite(value))) {
      return { node, parent, error: !parent ? 'Sem equipamento pai (topo da arvore)' : 'Coordenadas incompletas' };
    }
    const straight = planningDistanceMeters(...coordinates);
    const manualRoute = Number(node.metadata?.route_distance_m);
    const route = Number.isFinite(manualRoute) ? manualRoute : straight;
    const estimated = !Number.isFinite(manualRoute);
    const installed = (route * (1 + config.routePercent / 100)) + (estimated ? config.slackMeters : 0);
    const purchase = installed * (1 + config.reservePercent / 100);
    return { node, parent, straight, route, installed, purchase, estimated, overLimit: installed > config.maxMeters };
  });
  return rows.sort((a, b) => planningNaturalCompare(a.parent?.name, b.parent?.name) || planningNaturalCompare(a.node?.name, b.node?.name));
}

function planningFiberConfig(root) {
  const number = (id, fallback) => {
    const value = Number(root.querySelector(`#${id}`)?.value);
    return Number.isFinite(value) && value >= 0 ? value : fallback;
  };
  return {
    routePercent: number('planFiberRoute', 10), slackMeters: number('planFiberSlack', 10),
    reservePercent: number('planFiberReserve', 10), maxMeters: number('planFiberMax', 2000), spoolMeters: 1000,
  };
}

function renderPlanningFiberResults(root) {
  const target = root.querySelector('#planningFiberResults');
  if (!target) return [];
  const config = planningFiberConfig(root);
  const rows = planningFiberCalculation(config);
  const valid = rows.filter(row => !row.error);
  const missing = rows.filter(row => row.error);
  const overLimit = valid.filter(row => row.overLimit);
  const totalInstalled = valid.reduce((sum, row) => sum + row.installed, 0);
  const totalPurchase = valid.reduce((sum, row) => sum + row.purchase, 0);
  const parents = [...new Map(valid.map(row => [Number(row.parent.id), row.parent])).values()].sort((a, b) => planningNaturalCompare(a.name, b.name));
  const parentSections = parents.map(parent => {
    const items = valid.filter(row => Number(row.parent.id) === Number(parent.id)).sort((a, b) => planningNaturalCompare(a.node.name, b.node.name));
    const subtotal = items.reduce((sum, row) => sum + row.purchase, 0);
    return `<details class="planning-cable-box" open><summary><span><strong>${planningEscape(parent.name)}</strong><small>${items.length} trecho(s) &middot; ${planningEscape(parent.site_name || 'Sem site')}</small></span><strong>${subtotal.toFixed(1)} m</strong></summary>
      <div class="planning-cable-table-head"><span>Destino</span><span>Aerea</span><span>Via ruas</span><span>Instalado</span><span>Para compra</span><span>Situacao</span></div>
      ${items.map(row => `<div class="planning-cable-row"><strong title="${planningEscape(row.node.name)}">${planningEscape(row.node.name)}</strong><span>${row.straight.toFixed(1)} m</span><span>${row.route.toFixed(1)} m${row.estimated ? ' (estimado)' : ''}</span><span>${row.installed.toFixed(1)} m</span><span>${row.purchase.toFixed(1)} m</span><span class="planning-cable-status ${row.overLimit ? 'danger' : ''}">${row.overLimit ? 'Revisar rota' : 'Dentro do limite'}</span></div>`).join('')}</details>`;
  }).join('');
  const warnings = [];
  if (overLimit.length) warnings.push(`${overLimit.length} trecho(s) ultrapassam ${config.maxMeters} m instalados. Considere um ponto de emenda intermediario.`);
  if (missing.length) {
    const byReason = new Map();
    missing.forEach(row => byReason.set(row.error, (byReason.get(row.error) || 0) + 1));
    byReason.forEach((count, reason) => warnings.push(`${count} equipamento(s): ${reason}.`));
  }
  const emptyMessage = rows.length
    ? 'Nenhum trecho pronto pra calcular ainda -- veja os avisos acima.'
    : 'Nenhum DIO/CTO cadastrado neste projeto.';
  target.innerHTML = `<div class="planning-cable-summary"><div><span>Trechos calculados</span><strong>${valid.length}</strong></div><div><span>Fibra instalada</span><strong>${totalInstalled.toFixed(0)} m</strong></div><div><span>Fibra para compra</span><strong>${totalPurchase.toFixed(0)} m</strong></div><div><span>Bobinas de ${config.spoolMeters} m</span><strong>${Math.ceil(totalPurchase / config.spoolMeters)}</strong></div></div>${warnings.length ? `<div class="planning-cable-warning">${warnings.map(planningEscape).join('<br>')}</div>` : ''}${parentSections || `<div class="planning-cable-empty">${planningEscape(emptyMessage)}</div>`}`;
  lucide.createIcons();
  root._planningFiberRows = rows;
  return rows;
}

function downloadPlanningFiberCsv(root) {
  const config = planningFiberConfig(root);
  const rows = renderPlanningFiberResults(root);
  const lines = [['equipamento_pai', 'destino', 'site', 'distancia_aerea_m', 'percurso_viario_m', 'percurso_estimado', 'fibra_instalada_m', 'fibra_compra_m', 'limite_m', 'situacao']];
  rows.forEach(row => lines.push([row.parent?.name || '', row.node.name || '', row.node.site_name || '', row.straight?.toFixed(1) || '', row.route?.toFixed(1) || '', row.estimated ? 'sim' : 'nao', row.installed?.toFixed(1) || '', row.purchase?.toFixed(1) || '', config.maxMeters, row.error || (row.overLimit ? 'revisar rota' : 'dentro do limite')]));
  const content = '﻿' + lines.map(line => line.map(planningCsvValue).join(';')).join('\r\n');
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = `${String(_planningCurrent.name || 'projeto-cftv').replace(/[^a-z0-9_-]+/gi, '-')}-fibra.csv`; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  showToast('Calculo de fibra exportado.');
}

function openPlanningFiberModal() {
  if (!_planningCurrent) return;
  const modal = planningModal({
    eyebrow: 'Dimensionamento', title: 'Calcular fibra (backbone)', wide: true, primary: 'Fechar',
    body: `<div class="planning-cable-tool">
      <div class="planning-cable-settings">
        ${planningField('Margem sobre a rota (%)', 'planFiberRoute', '10', 'type="number" min="0" step="1"')}
        ${planningField('Folga tecnica por trecho (m)', 'planFiberSlack', '10', 'type="number" min="0" step="1"')}
        ${planningField('Reserva para compra (%)', 'planFiberReserve', '10', 'type="number" min="0" step="1"')}
        ${planningField('Limite do trecho instalado (m)', 'planFiberMax', '2000', 'type="number" min="1" step="1"')}
      </div>
      <div class="planning-csv-note"><i data-lucide="route"></i><span><strong>Automatico:</strong> a partir de cada DIO/CTO do projeto, calcula o trecho ate o equipamento pai (OLT, DIO ou CTO acima na arvore) -- percorrendo a hierarquia inteira, da caixa de emenda principal ate cada CTO, sem precisar escolher nada na mao. <strong>Instalado</strong> = percurso pelas ruas + margem de passagem + folga. Sem percurso viario medido, usa a distancia aerea como estimativa -- preencha "Percurso viario ate o equipamento pai" no proprio DIO/CTO pra refinar.</span></div>
      <div class="planning-cable-results" id="planningFiberResults"></div>
    </div>`,
    onSave: async () => closePlanningModal(),
  });
  const footer = modal.querySelector('.planning-modal-footer');
  const saveButton = footer?.querySelector('[data-save]');
  if (footer && saveButton) {
    const download = document.createElement('button');
    download.type = 'button'; download.className = 'secondary-action';
    download.innerHTML = '<i data-lucide="download"></i> Baixar CSV';
    download.onclick = () => downloadPlanningFiberCsv(modal);
    footer.insertBefore(download, saveButton);
  }
  ['planFiberRoute', 'planFiberSlack', 'planFiberReserve', 'planFiberMax'].forEach(id => modal.querySelector(`#${id}`)?.addEventListener('input', () => renderPlanningFiberResults(modal)));
  renderPlanningFiberResults(modal);
  lucide.createIcons();
}

// Diagrama de rede: desenha a hierarquia real do backbone (OLT -> DIO ->
// CDO/CTO em cascata -> CTOs terminais -> caixas) como uma arvore visual,
// igual a foto do KMZ que o usuario mostrou -- tronco horizontal com os
// pontos de emenda em sequencia e as CTOs ramificando, cada caixa exibindo
// a cor fundida/livre.
const PLANNING_NET_COL_W = 250;
const PLANNING_NET_ROW_H = 96;

function planningNetworkLayout() {
  const devices = _planningCurrent?.devices || [];
  const byId = new Map(devices.map(d => [Number(d.id), d]));
  const byParent = new Map();
  devices.forEach(d => {
    const pid = d.parent_id ? Number(d.parent_id) : null;
    if (!byParent.has(pid)) byParent.set(pid, []);
    byParent.get(pid).push(d);
  });
  const backbone = new Set(['olt', 'dio', 'cto']);
  const roots = devices.filter(d => backbone.has(d.device_type) && (!d.parent_id || !byId.has(Number(d.parent_id))));

  let rowCounter = 0;
  const visit = (node, depth) => {
    node._depth = depth;
    node._boxCount = (byParent.get(Number(node.id)) || []).filter(c => c.device_type === 'box').length;
    const children = (byParent.get(Number(node.id)) || []).filter(c => backbone.has(c.device_type)).sort((a, b) => planningNaturalCompare(a.name, b.name));
    node._children = children;
    if (!children.length) {
      node._row = rowCounter;
      rowCounter += 1;
    } else {
      children.forEach(c => visit(c, depth + 1));
      node._row = (children[0]._row + children[children.length - 1]._row) / 2;
    }
  };
  roots.sort((a, b) => planningNaturalCompare(a.name, b.name)).forEach(root => visit(root, 0));
  return { roots, byParent };
}

function planningNetworkCardHtml(node) {
  const meta = node.metadata || {};
  const isRoot = !node.parent_id;
  const isTerminal = node.device_type === 'cto' && meta.papel !== 'splitter_primario';
  const icon = node.device_type === 'olt' ? '🗄️' : node.device_type === 'dio' ? '🔗' : isTerminal ? '🧰' : '📦';
  const roleLabel = node.device_type === 'olt' ? 'OLT' : node.device_type === 'dio' ? 'DIO' : isTerminal ? 'CTO terminal' : 'Caixa de emenda/CDO';
  const chips = (arr, cls) => (arr || []).slice(0, 6).map(c => `<span class="planning-net-chip ${cls}">${planningEscape(c)}</span>`).join('') + ((arr || []).length > 6 ? `<span class="planning-net-chip">+${arr.length - 6}</span>` : '');
  let rows = '';
  if (meta.cor_fibra?.length) rows += `<div class="planning-net-row"><span>Fundida</span><div class="planning-net-chips">${chips(meta.cor_fibra, 'is-used')}</div></div>`;
  if (meta.fibras_mencionadas?.length) rows += `<div class="planning-net-row"><span>Livre</span><div class="planning-net-chips">${chips(meta.fibras_mencionadas, 'is-free')}</div></div>`;
  if (isTerminal) rows += `<div class="planning-net-row"><span>Clientes</span><span class="planning-net-count">${node._boxCount || 0} caixa(s)</span></div>`;
  return `<div class="planning-net-card ${isTerminal ? 'is-terminal' : ''} ${isRoot ? 'is-root' : ''}" data-device-id="${node.id}" title="Editar ${planningEscape(node.name)}">
    <div class="planning-net-card-head"><span>${icon}</span><strong>${planningEscape(node.name)}</strong></div>
    <div class="planning-net-role">${roleLabel}</div>
    ${rows}
  </div>`;
}

function renderPlanningNetworkDiagram(root) {
  const target = root.querySelector('#planningNetworkCanvas');
  if (!target) return;
  const { roots } = planningNetworkLayout();
  if (!roots.length) {
    target.innerHTML = '<div class="planning-cable-empty">Nenhum OLT/DIO/CTO cadastrado neste projeto ainda.</div>';
    return;
  }
  const nodes = [];
  const edges = [];
  const collect = node => {
    nodes.push(node);
    (node._children || []).forEach(child => { edges.push([node, child]); collect(child); });
  };
  roots.forEach(collect);

  const maxDepth = Math.max(...nodes.map(n => n._depth));
  const maxRow = Math.max(...nodes.map(n => n._row));
  const width = (maxDepth + 1) * PLANNING_NET_COL_W + 40;
  const height = (maxRow + 1) * PLANNING_NET_ROW_H + 40;
  const posX = n => n._depth * PLANNING_NET_COL_W + 20;
  const posY = n => n._row * PLANNING_NET_ROW_H + 20 + PLANNING_NET_ROW_H / 2 - 34;
  const centerY = n => n._row * PLANNING_NET_ROW_H + 20 + PLANNING_NET_ROW_H / 2;

  const svgLines = edges.map(([parent, child]) => {
    const x1 = posX(parent) + 210, y1 = centerY(parent);
    const x2 = posX(child), y2 = centerY(child);
    const midX = (x1 + x2) / 2;
    return `<path class="planning-net-line" d="M${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}"/>`;
  }).join('');

  const cardsHtml = nodes.map(n => `<div class="planning-net-card-slot" style="left:${posX(n)}px; top:${posY(n)}px;">${planningNetworkCardHtml(n)}</div>`).join('');

  target.innerHTML = `<div class="planning-net-canvas-inner" style="width:${width}px; height:${height}px;">
    <svg class="planning-net-svg" width="${width}" height="${height}">${svgLines}</svg>
    ${cardsHtml}
  </div>`;
  target.querySelectorAll('.planning-net-card').forEach(card => {
    card.addEventListener('click', () => { closePlanningModal(); openPlanningDeviceModal(Number(card.dataset.deviceId)); });
  });
}

function openPlanningNetworkDiagram() {
  if (!_planningCurrent) return;
  const modal = planningModal({
    eyebrow: 'Visualizacao', title: 'Diagrama de rede (hierarquia da fibra)', wide: true, primary: 'Fechar',
    body: `<div class="planning-net-tool">
      <div class="planning-info"><i data-lucide="git-branch"></i><span>Tronco (OLT/DIO/Caixa de Emenda) seguindo em sequencia, com as CTOs terminais ramificando. Clique numa caixa pra editar. Cores mostram o que foi fundido/continua em cada ponto.</span></div>
      <div class="planning-net-canvas" id="planningNetworkCanvas"></div>
    </div>`,
    onSave: async () => closePlanningModal(),
  });
  renderPlanningNetworkDiagram(modal);
  lucide.createIcons();
}

function bindPlanningUi() {
  const on = (id, event, handler) => { const el = document.getElementById(id); if (el) el.addEventListener(event, handler); };
  on('btnPlanningNew', 'click', () => openPlanningProjectModal(true));
  on('btnPlanningRefresh', 'click', () => loadPlanning(true));
  on('btnPlanningFiber', 'click', openPlanningFiberModal);
  on('btnPlanningNetworkDiagram', 'click', openPlanningNetworkDiagram);
  on('btnPlanningEdit', 'click', () => openPlanningProjectModal(false));
  on('btnPlanningSite', 'click', openPlanningSiteModal);
  on('btnPlanningKmz', 'click', openPlanningKmzModal);
  on('btnPlanningExportKmz', 'click', exportPlanningKmz);
  on('btnPlanningProposal', 'click', openPlanningProposalModal);
  on('btnPlanningDelete', 'click', deletePlanningProject);
  on('btnPlanningAdd', 'click', () => openPlanningDeviceModal());
  on('btnPlanningBox', 'click', openPlanningBoxModal);
  on('btnPlanningCables', 'click', openPlanningCableModal);
  on('btnPlanningNetworkPdf', 'click', downloadPlanningNetworkPdf);
  on('btnPlanningGenerate', 'click', openPlanningGenerateModal);
  on('btnPlanningCsv', 'click', openPlanningCsvModal);
  on('planningSearch', 'input', renderPlanningDevices);
  on('planningTypeFilter', 'change', renderPlanningDevices);
  on('planningSiteFilter', 'change', renderPlanningDevices);
}
