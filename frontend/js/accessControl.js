let _accessPeopleRows = [];
let _accessPeopleSelected = new Set();
let _accessPersonModalCurrent = null;
let _accessPersonAccessLoaded = false;
let _accessPersonAccessLoadFailed = false;
let _accessPersonHeroPhotoObjectUrl = '';
let _accessGroupPeopleRows = [];
let _accessGroupSelectedPeople = new Set();
let _accessReportRows = [];
let _accessReportSearchTimer = null;
let _accessReportAutoRefreshTimer = null;
let _accessReportAutoRefreshBusy = false;
let _accessControlUiBound = false;
let _accessControlBinding = false;
let _accessPeopleImportDeviceRows = [];
let _accessWhatsappCurrentConfigured = false;
let _accessWhatsappKpiLoading = false;
const ACCESS_REPORT_AUTO_REFRESH_MS = 8000;

function accessPersonTypeLabel(type) {
  const key = String(type || '').toLowerCase();
  if (key === 'employee') return 'Funcionario';
  if (key === 'visitor') return 'Visitante';
  return 'Aluno';
}

function accessPersonStatusBadge(active) {
  return active
    ? '<span class="pill success">Ativo</span>'
    : '<span class="pill neutral">Inativo</span>';
}

function accessProvisionStatusBadge(summary) {
  const data = summary || {};
  const status = String(data.status || 'not_configured').toLowerCase();
  if (status === 'ok') return '<span class="pill success">sync ok</span>';
  if (status === 'pending') return '<span class="pill neutral">pendente</span>';
  if (status === 'failed') return `<span class="pill danger" title="${esc(data.last_error || '')}">falhou</span>`;
  return '<span class="pill neutral">sem regra</span>';
}

function accessDirectionLabel(value) {
  const key = String(value || 'entrada').toLowerCase();
  if (key === 'saida') return 'Saida';
  if (key === 'entrada_saida') return 'Entrada e saida';
  return 'Entrada';
}

function accessEventTypeLabel(value) {
  const key = String(value || 'entrada').toLowerCase();
  if (key === 'saida_manual') return 'Saida manual';
  if (key === 'saida') return 'Saida';
  return 'Entrada';
}

function accessEventTypeBadge(value) {
  const key = String(value || 'entrada').toLowerCase();
  if (key === 'entrada') return '<span class="pill success">entrada</span>';
  if (key === 'saida') return '<span class="pill neutral">saida</span>';
  if (key === 'saida_manual') return '<span class="pill amber">saida manual</span>';
  return `<span class="pill neutral">${esc(value || 'evento')}</span>`;
}

const ACCESS_TYPE_ICONS = { student: 'graduation-cap', employee: 'briefcase', visitor: 'user' };
function accessPersonTypeIcon(type) {
  const key = String(type || '').toLowerCase();
  const icon = ACCESS_TYPE_ICONS[key] || ACCESS_TYPE_ICONS.student;
  const cls = ACCESS_TYPE_ICONS[key] ? key : 'student';
  return `<span class="access-type-icon ${cls}"><i data-lucide="${icon}"></i></span>`;
}

function accessPersonFaceBadge(person) {
  return person?.face_photo_path
    ? '<span class="pill success">foto</span>'
    : '<span class="pill neutral">sem foto</span>';
}

function selectedAccessPersonGroupIds() {
  return new Set(Array.from(document.querySelectorAll('#accessPersonGroupsChecklist input:checked')).map(el => el.value));
}

function updateAccessPersonHero() {
  const name = document.getElementById('accessPersonName')?.value?.trim() || _accessPersonModalCurrent?.full_name || 'Pessoa sem nome';
  const type = document.getElementById('accessPersonType')?.value || _accessPersonModalCurrent?.person_type || 'student';
  const enrollment = document.getElementById('accessPersonEnrollment')?.value?.trim() || '';
  const className = document.getElementById('accessPersonClass')?.value?.trim() || '';
  const site = valorSitePessoa();
  const active = document.getElementById('accessPersonActive')?.checked !== false;
  setText('accessPersonHeroName', name);
  setText('accessPersonHeroMeta', [accessPersonTypeLabel(type), enrollment, className, site].filter(Boolean).join(' | '));
  const typeEl = document.getElementById('accessPersonHeroType');
  const activeEl = document.getElementById('accessPersonHeroStatus');
  if (typeEl) {
    typeEl.className = 'pill neutral';
    typeEl.textContent = accessPersonTypeLabel(type);
  }
  if (activeEl) {
    activeEl.outerHTML = accessPersonStatusBadge(active).replace('<span ', '<span id="accessPersonHeroStatus" ');
  }
}

function updateAccessPersonFacePreview() {
  const photo = document.getElementById('accessPersonHeroPhoto');
  const input = document.getElementById('accessPersonFacePhoto');
  const file = input?.files?.[0];
  if (!photo) return;
  if (_accessPersonHeroPhotoObjectUrl) {
    URL.revokeObjectURL(_accessPersonHeroPhotoObjectUrl);
    _accessPersonHeroPhotoObjectUrl = '';
  }
  if (file) {
    _accessPersonHeroPhotoObjectUrl = URL.createObjectURL(file);
    photo.innerHTML = `<img src="${_accessPersonHeroPhotoObjectUrl}" alt="">`;
    const faceStatus = document.getElementById('accessPersonFacePhotoStatus');
    if (faceStatus) faceStatus.textContent = `Nova foto selecionada: ${file.name}`;
    return;
  }
  photo.innerHTML = _accessPersonModalCurrent?.face_photo_path
    ? '<i data-lucide="loader-circle"></i>'
    : '<i data-lucide="user"></i>';
  lucide.createIcons();
  loadAccessPersonSavedFacePreview(_accessPersonModalCurrent);
}

async function loadAccessPersonSavedFacePreview(person) {
  const photo = document.getElementById('accessPersonHeroPhoto');
  if (!photo || !person?.id || !person?.face_photo_path) return;
  try {
    const headers = {};
    if (_token) headers.Authorization = `Bearer ${_token}`;
    const res = await fetch(`${API_BASE}/api/access-control/people/${encodeURIComponent(person.id)}/face-photo`, {
      credentials: 'same-origin',
      headers,
    });
    if (!res.ok) throw new Error('Foto indisponivel');
    const blob = await res.blob();
    if (_accessPersonModalCurrent?.id !== person.id) return;
    if (_accessPersonHeroPhotoObjectUrl) URL.revokeObjectURL(_accessPersonHeroPhotoObjectUrl);
    _accessPersonHeroPhotoObjectUrl = URL.createObjectURL(blob);
    photo.innerHTML = `<img src="${_accessPersonHeroPhotoObjectUrl}" alt="">`;
  } catch (err) {
    if (_accessPersonModalCurrent?.id === person.id) {
      photo.innerHTML = '<i data-lucide="image-check"></i>';
      lucide.createIcons();
    }
  }
}

async function ensureAccessPersonAccessData(force = false) {
  if (_accessPersonAccessLoaded && !force) return true;
  _accessPersonAccessLoadFailed = false;
  try {
    const [groupsRes, doorGroupsRes, rulesRes, devicesRes] = await Promise.all([
      apiJson('/api/access-control/groups', { forceRefresh: force, cacheTtl: 0 }),
      apiJson('/api/access-control/door-groups', { forceRefresh: force, cacheTtl: 0 }),
      apiJson('/api/access-control/rules', { forceRefresh: force, cacheTtl: 0 }),
      apiJson('/api/access-control/devices', { forceRefresh: force, cacheTtl: 0 }),
    ]);
    if (!groupsRes || !Array.isArray(groupsRes.groups)
      || !doorGroupsRes || !Array.isArray(doorGroupsRes.door_groups)
      || !rulesRes || !Array.isArray(rulesRes.rules)
      || !devicesRes || !Array.isArray(devicesRes.devices)) {
      throw new Error('Nao foi possivel carregar grupos, regras e controladoras.');
    }
    _accessGroupRows = groupsRes.groups;
    _accessDoorGroupRows = doorGroupsRes.door_groups;
    _accessRuleRows = rulesRes.rules;
    _accessDeviceRows = devicesRes.devices;
    _accessPersonAccessLoaded = true;
    return true;
  } catch (err) {
    _accessPersonAccessLoadFailed = true;
    _accessPersonAccessLoaded = false;
    const checklist = document.getElementById('accessPersonGroupsChecklist');
    if (checklist) checklist.innerHTML = '<p class="muted-block">Nao foi possivel carregar grupos e regras. Feche e reabra para tentar de novo.</p>';
    showToast(err?.message || 'Nao foi possivel carregar o acesso da pessoa.', true);
    return false;
  }
}

function renderAccessPersonGroupsChecklist(person = _accessPersonModalCurrent) {
  const checklist = document.getElementById('accessPersonGroupsChecklist');
  if (!checklist) return;
  const personId = person?.id || '';
  checklist.innerHTML = _accessGroupRows.map(group => {
    const checked = personId && (group.member_ids || []).includes(personId);
    return `
      <label class="access-checklist-item">
        <input type="checkbox" value="${esc(group.id)}" ${checked ? 'checked' : ''}>
        <span><strong>${esc(group.name)}</strong>${group.site ? ` <small>${esc(group.site)}</small>` : ''}</span>
      </label>
    `;
  }).join('') || '<p class="muted-block">Cadastre um grupo de pessoas na aba Grupos.</p>';
}

function renderAccessPersonAccessPanel() {
  const selectedGroups = selectedAccessPersonGroupIds();
  const activeRules = _accessRuleRows.filter(rule => rule.active !== false && selectedGroups.has(rule.people_group_id));
  const doorIds = new Set(activeRules.map(rule => rule.door_group_id).filter(Boolean));
  const deviceIds = new Set();
  _accessDoorGroupRows
    .filter(group => doorIds.has(group.id))
    .forEach(group => (group.device_ids || []).forEach(id => deviceIds.add(id)));

  const doorEl = document.getElementById('accessPersonDoorAccessSummary');
  const deviceEl = document.getElementById('accessPersonDeviceAccessSummary');
  const doorGroups = _accessDoorGroupRows.filter(group => doorIds.has(group.id));
  const devices = _accessDeviceRows.filter(device => deviceIds.has(device.id));
  if (doorEl) {
    doorEl.innerHTML = doorGroups.length
      ? doorGroups.map(group => `<span class="pill neutral">${esc(group.name)}</span>`).join('')
      : '<p class="muted-block">Nenhuma regra ativa para os grupos marcados.</p>';
  }
  if (deviceEl) {
    deviceEl.innerHTML = devices.length
      ? devices.map(device => `<span class="pill neutral" title="${esc(device.host || '')}">${esc(device.name)}</span>`).join('')
      : '<p class="muted-block">Nenhuma controladora vinculada por regra.</p>';
  }
}

async function saveAccessPersonGroupMembership(personId) {
  if (!personId) return;
  if (_accessPersonAccessLoadFailed) {
    throw new Error('A lista de grupos nao carregou. Reabra o cadastro antes de salvar.');
  }
  const loaded = await ensureAccessPersonAccessData(false);
  if (!loaded) {
    throw new Error('A lista de grupos nao carregou. Reabra o cadastro antes de salvar.');
  }
  const selectedGroups = selectedAccessPersonGroupIds();
  const updates = _accessGroupRows
    .map(group => {
      const memberIds = new Set(group.member_ids || []);
      const hadMember = memberIds.has(personId);
      if (selectedGroups.has(group.id)) memberIds.add(personId);
      else memberIds.delete(personId);
      if (hadMember === memberIds.has(personId)) return null;
      return {
        id: group.id,
        name: group.name,
        site: group.site || '',
        member_ids: Array.from(memberIds),
      };
    })
    .filter(Boolean);
  for (const payload of updates) {
    const res = await api('/api/access-control/groups', { method: 'POST', body: JSON.stringify(payload) });
    await jsonOrReadableError(res, 'Pessoa salva, mas nao foi possivel atualizar os grupos.');
  }
  if (updates.length) {
    _accessPersonAccessLoaded = false;
    await ensureAccessPersonAccessData(true);
  }
}

// Numero fica salvo so com digitos (+ opcional na frente, ver _clean_phone no
// backend) -- aqui e so pra exibicao, nunca reenviado formatado.
function formatBrPhone(phone) {
  const digits = String(phone || '').replace(/\D/g, '');
  if (digits.length === 11) return `(${digits.slice(0, 2)}) ${digits.slice(2, 7)}-${digits.slice(7)}`;
  if (digits.length === 10) return `(${digits.slice(0, 2)}) ${digits.slice(2, 6)}-${digits.slice(6)}`;
  return phone || '';
}

// Documento e texto livre no cadastro (CPF/RG) -- so aplica mascara de CPF
// quando bate exatamente com 11 digitos, senao mostra como foi digitado.
function formatDocument(doc) {
  const digits = String(doc || '').replace(/\D/g, '');
  if (digits.length === 11 && digits === String(doc || '').trim()) {
    return `${digits.slice(0, 3)}.${digits.slice(3, 6)}.${digits.slice(6, 9)}-${digits.slice(9)}`;
  }
  return doc || '';
}

function isAccessControlViewVisible() {
  return !document.getElementById('viewAccessControl')?.classList.contains('hidden');
}

function activeAccessControlTab() {
  return document.querySelector('.access-control-tabs [data-access-tab].active')?.dataset.accessTab || 'people';
}

function stopAccessReportAutoRefresh() {
  if (!_accessReportAutoRefreshTimer) return;
  clearInterval(_accessReportAutoRefreshTimer);
  _accessReportAutoRefreshTimer = null;
}

function startAccessReportAutoRefresh() {
  stopAccessReportAutoRefresh();
  _accessReportAutoRefreshTimer = setInterval(async () => {
    if (document.hidden || !isAccessControlViewVisible() || activeAccessControlTab() !== 'reports') return;
    if (_accessReportAutoRefreshBusy) return;
    _accessReportAutoRefreshBusy = true;
    try {
      await Promise.all([
        loadAccessControlSummary(true),
        loadAccessReports(true, { silent: true }),
      ]);
    } catch (err) {
      console.warn('SightOps Access Control auto refresh failed', err);
    } finally {
      _accessReportAutoRefreshBusy = false;
    }
  }, ACCESS_REPORT_AUTO_REFRESH_MS);
}

function bindAccessControl() {
  if (_accessControlUiBound || _accessControlBinding) return;
  _accessControlBinding = true;
  try {
    document.getElementById('btnAccessPrimaryAction')?.addEventListener('click', handleAccessPrimaryAction);
    const studentsCard = document.getElementById('accessKpiStudentsCard');
    studentsCard?.addEventListener('click', () => openAccessStudentsDrawer());
    studentsCard?.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openAccessStudentsDrawer();
    });
    const devicesCard = document.getElementById('accessKpiDevicesCard');
    devicesCard?.addEventListener('click', () => openAccessDevicesDrawer());
    devicesCard?.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openAccessDevicesDrawer();
    });
    const eventsCard = document.getElementById('accessKpiEventsCard');
    eventsCard?.addEventListener('click', openAccessTodayEventsReport);
    eventsCard?.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openAccessTodayEventsReport();
    });
    const whatsappCard = document.getElementById('accessKpiWhatsappCard');
    whatsappCard?.addEventListener('click', openAccessWhatsappConnections);
    whatsappCard?.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openAccessWhatsappConnections();
    });
    document.getElementById('btnAccessPeopleClearFilters')?.addEventListener('click', clearAccessPeopleFilters);
    document.getElementById('btnAccessPersonFooterNew')?.addEventListener('click', () => openAccessPersonModal());
    document.getElementById('btnAccessPeopleFooterRefresh')?.addEventListener('click', () => loadAccessControl(true));
    document.getElementById('btnAccessPeopleFooterImport')?.addEventListener('click', openAccessPeopleImportModal);
  document.getElementById('btnAccessPeopleFooterSheet')?.addEventListener('click', abrirImportacaoPlanilha);
  document.getElementById('btnAccessSheetClose')?.addEventListener('click', fecharImportacaoPlanilha);
  document.getElementById('btnAccessSheetCancel')?.addEventListener('click', fecharImportacaoPlanilha);
  document.getElementById('btnAccessSheetAnalyze')?.addEventListener('click', () => enviarPlanilha(false));
  document.getElementById('btnAccessSheetApply')?.addEventListener('click', () => enviarPlanilha(true));
  document.getElementById('accessSheetFile')?.addEventListener('change', (ev) => {
    // arquivo trocado invalida a conferencia anterior
    const aplicar = document.getElementById('btnAccessSheetApply');
    if (aplicar) aplicar.disabled = true;
    const previa = document.getElementById('accessSheetPreview');
    if (previa) previa.hidden = true;
    const nomeArquivo = document.getElementById('accessSheetFileName');
    if (nomeArquivo) nomeArquivo.textContent = ev.target.files?.[0]?.name || 'Nenhum arquivo escolhido';
  });
  document.getElementById('btnAccessSheetModelo')?.addEventListener('click', baixarModeloPlanilhaAlunos);
    document.getElementById('btnAccessPeopleFooterSync')?.addEventListener('click', syncSelectedAccessPeople);
    document.getElementById('btnAccessPeopleFooterEdit')?.addEventListener('click', editSelectedAccessPerson);
    document.getElementById('btnAccessPeopleFooterDeleteSelected')?.addEventListener('click', deleteSelectedAccessPeople);
    document.getElementById('btnAccessPeopleFooterDeleteAll')?.addEventListener('click', deleteAllVisibleAccessPeople);
    document.getElementById('accessPeopleSearch')?.addEventListener('input', debounceAccessPeopleSearch);
    document.getElementById('accessPeopleStatus')?.addEventListener('change', () => loadAccessControl(true));
    document.getElementById('accessPeopleType')?.addEventListener('change', () => loadAccessControl(true));
    document.getElementById('accessPeopleSite')?.addEventListener('change', () => loadAccessControl(true));
    document.getElementById('accessPeopleSelectAll')?.addEventListener('change', toggleAccessPeopleSelectAll);
    document.getElementById('btnAccessPersonClose')?.addEventListener('click', closeAccessPersonModal);
    document.getElementById('btnAccessPersonCancel')?.addEventListener('click', closeAccessPersonModal);
    document.getElementById('btnAccessPeopleImportClose')?.addEventListener('click', closeAccessPeopleImportModal);
    document.getElementById('btnAccessPeopleImportCancel')?.addEventListener('click', closeAccessPeopleImportModal);
    document.getElementById('btnAccessPeopleImportRun')?.addEventListener('click', importAccessPeopleFromSelectedDevice);
    document.getElementById('accessPersonForm')?.addEventListener('submit', saveAccessPersonFromForm);
    document.getElementById('accessPeopleBody')?.addEventListener('click', handleAccessPeopleBodyClick);
    bindAccessPersonModal();
    loadAccessPeopleSiteOptions();
    bindAccessTabs();
    bindAccessDevices();
    bindAccessGroups();
    bindAccessReports();
    bindAccessConnections();
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && isAccessControlViewVisible() && activeAccessControlTab() === 'reports') {
        loadAccessControlSummary(true);
        loadAccessReports(true, { silent: true });
      }
    });
    updateAccessPrimaryAction('people');
    _accessControlUiBound = true;
  } finally {
    _accessControlBinding = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => {
    try {
      bindAccessControl();
    } catch (err) {
      _accessControlBinding = false;
      console.error('SightOps Access Control bind failed', err);
    }
  }, 0);
});

function bindAccessPersonModal() {
  document.querySelectorAll('[data-access-person-tab]').forEach(btn => {
    btn.addEventListener('click', event => {
      event.stopPropagation();
      showAccessPersonTab(btn.dataset.accessPersonTab || 'details');
    });
  });
  ['accessPersonName', 'accessPersonType', 'accessPersonEnrollment', 'accessPersonClass', 'accessPersonSite', 'accessPersonActive'].forEach(id => {
    const el = document.getElementById(id);
    const eventName = el?.tagName === 'SELECT' || el?.type === 'checkbox' ? 'change' : 'input';
    el?.addEventListener(eventName, updateAccessPersonHero);
  });
  document.getElementById('accessPersonGroupsChecklist')?.addEventListener('change', renderAccessPersonAccessPanel);
  document.getElementById('accessPersonFacePhoto')?.addEventListener('change', updateAccessPersonFacePreview);
}

function showAccessPersonTab(tab) {
  const activeTab = tab || 'details';
  document.querySelectorAll('[data-access-person-tab]').forEach(btn => {
    const active = btn.dataset.accessPersonTab === activeTab;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('[data-access-person-panel]').forEach(panel => {
    panel.hidden = panel.dataset.accessPersonPanel !== activeTab;
  });
}

let _accessPeopleSearchTimer = null;
function debounceAccessPeopleSearch() {
  clearTimeout(_accessPeopleSearchTimer);
  _accessPeopleSearchTimer = setTimeout(() => loadAccessControl(true), 280);
}

function clearAccessPeopleFilters() {
  const search = document.getElementById('accessPeopleSearch');
  const status = document.getElementById('accessPeopleStatus');
  const type = document.getElementById('accessPeopleType');
  const site = document.getElementById('accessPeopleSite');
  if (search) search.value = '';
  if (status) status.value = '';
  if (type) type.value = '';
  if (site) site.value = '';
  loadAccessControl(true);
}

function updateAccessPrimaryAction(tab = 'people') {
  const button = document.getElementById('btnAccessPrimaryAction');
  const label = document.getElementById('accessPrimaryActionLabel');
  if (!button || !label) return;
  const actions = {
    people: 'Nova pessoa',
    devices: 'Novo dispositivo',
    groups: 'Novo grupo',
    rules: 'Nova regra',
    reports: 'Saida manual',
    connections: 'Testar WhatsApp',
  };
  button.dataset.accessPrimaryAction = tab;
  label.textContent = actions[tab] || actions.people;
}

function handleAccessPrimaryAction() {
  const action = document.getElementById('btnAccessPrimaryAction')?.dataset.accessPrimaryAction || 'people';
  if (action === 'devices') {
    openAccessDeviceModal();
    return;
  }
  if (action === 'groups') {
    openAccessGroupModal();
    return;
  }
  if (action === 'rules') {
    openAccessRuleModal();
    return;
  }
  if (action === 'reports') {
    document.getElementById('accessManualExitPerson')?.focus();
    return;
  }
  if (action === 'connections') {
    document.getElementById('accessWhatsappTestNumber')?.focus();
    return;
  }
  openAccessPersonModal();
}

async function loadAccessControl(force = false) {
  bindAccessControl();
  const query = new URLSearchParams();
  const search = document.getElementById('accessPeopleSearch')?.value?.trim() || '';
  const active = document.getElementById('accessPeopleStatus')?.value || '';
  const type = document.getElementById('accessPeopleType')?.value || '';
  const site = document.getElementById('accessPeopleSite')?.value || '';
  if (search) query.set('search', search);
  if (active) query.set('active', active);
  if (type) query.set('person_type', type);
  if (site) query.set('site', site);

  try {
    renderAccessPeopleLoading();
    const [summaryRes, peopleRes] = await Promise.all([
      apiJson('/api/access-control/summary', { forceRefresh: force, cacheTtl: 0 }),
      apiJson(`/api/access-control/people?${query.toString()}`, { forceRefresh: force, cacheTtl: 0 }),
    ]);
    const summary = summaryRes?.summary || {};
    _accessPeopleRows = peopleRes?.people || [];
    renderAccessControlSummary(summary, force);
    renderAccessPeople(_accessPeopleRows);
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel carregar controle de acesso.', true);
  }
}

async function loadAccessPeopleSiteOptions() {
  const select = document.getElementById('accessPeopleSite');
  if (!select) return;
  try {
    const res = await apiJson('/api/access-control/people/sites', { cacheTtl: 0 });
    const sites = res?.sites || [];
    const current = select.value;
    select.innerHTML = '<option value="">Todos os sites</option>'
      + sites.map(site => `<option value="${esc(site)}">${esc(site)}</option>`).join('');
    if (sites.includes(current)) select.value = current;
  } catch (err) {
    // Filtro fica so com "Todos os sites" se a busca falhar -- nao bloqueia a tela.
  }
}

async function loadAccessWhatsappSiteOptions(force = false) {
  const select = document.getElementById('accessWhatsappSite');
  if (!select) return;
  try {
    const res = await apiJson('/api/access-control/people/sites', { forceRefresh: force, cacheTtl: 0 });
    const sites = res?.sites || [];
    const current = select.value;
    select.innerHTML = '<option value="">Padrao do cliente</option>'
      + sites.map(site => `<option value="${esc(site)}">${esc(site)}</option>`).join('');
    if (sites.includes(current)) select.value = current;
  } catch (err) {
    if (!select.options.length) select.innerHTML = '<option value="">Padrao do cliente</option>';
  }
}

function renderAccessControlSummary(summary, force = false) {
  setText('accessKpiStudents', summary.students || 0);
  setText('accessKpiPeopleSub', `${summary.people_active || 0} ativo(s) de ${summary.people_total || 0}`);
  setText('accessKpiDevices', summary.devices_active || 0);
  setText('accessKpiEvents', summary.events_today || 0);
  renderAccessWhatsappKpiStatus({ state: 'checking' });
  loadAccessWhatsappKpiStatus(force);
}

function renderAccessWhatsappKpiStatus(data = {}) {
  const configurado = !!data?.configured;
  if (!configurado) {
    setText('accessKpiWhatsapp', 'Nao configurado');
    setText('accessKpiWhatsappSub', 'configure na aba Conexoes');
    return;
  }
  const provider = String(data?.provider || 'cloud_api').toLowerCase();
  // Para o Evolution, "configurado" so quer dizer que a plataforma tem URL e
  // chave -- nao que a sessao esta viva. Mostrar "Conectado" so por isso
  // repetiria em menor escala o bug que esta feature existe para evitar: o
  // card do topo dizendo Conectado com a sessao morta. No canal oficial nao
  // ha sessao para cair, entao "configurado" continua sendo o sinal certo.
  if (provider === 'evolution' && !data?.connected) {
    const ESTADOS = {
      waiting_qr: 'Aguardando QR Code', disconnected: 'Desconectado',
      error: 'Erro ao consultar', unknown: 'Desconhecido', not_configured: 'Nao configurado',
    };
    setText('accessKpiWhatsapp', ESTADOS[String(data?.state || '')] || 'Nao conectado');
    setText('accessKpiWhatsappSub', 'via Evolution API');
    return;
  }
  setText('accessKpiWhatsapp', 'Conectado');
  if (provider === 'evolution') {
    const site = String(data?.site || '').trim();
    setText('accessKpiWhatsappSub', site ? `via ${site} (Evolution)` : 'via Evolution API');
    return;
  }
  // Com mais de uma escola, citar so a primeira daria a impressao de que as
  // demais estao fora do ar.
  const escolas = Array.isArray(data?.configured_sites) ? data.configured_sites : [];
  const site = String(data?.site || '').trim();
  if (escolas.length > 1) {
    setText('accessKpiWhatsappSub', `${escolas.length} escolas conectadas`);
  } else if (escolas.length === 1 || site) {
    setText('accessKpiWhatsappSub', `via ${escolas[0] || site}`);
  } else {
    setText('accessKpiWhatsappSub', 'canal oficial da Meta');
  }
}

async function loadAccessWhatsappKpiStatus(force = false) {
  if (_accessWhatsappKpiLoading) return;
  _accessWhatsappKpiLoading = true;
  try {
    const data = await apiJson('/api/access-control/whatsapp/connection?summary=1', { forceRefresh: force, cacheTtl: 0 });
    renderAccessWhatsappKpiStatus(data);
  } catch (err) {
    renderAccessWhatsappKpiStatus({ state: 'error', error: err?.message || '' });
  } finally {
    _accessWhatsappKpiLoading = false;
  }
}

function renderAccessPeople(rows) {
  const body = document.getElementById('accessPeopleBody');
  if (!body) return;
  const countEl = document.getElementById('accessPeopleCount');
  if (countEl) countEl.textContent = `${rows.length} pessoa${rows.length === 1 ? '' : 's'}`;
  const rowIds = new Set(rows.map(person => person.id));
  _accessPeopleSelected = new Set([..._accessPeopleSelected].filter(id => rowIds.has(id)));
  syncAccessPeopleSelectAll();
  if (!rows.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="11">Nenhuma pessoa encontrada com esses filtros.</td></tr>';
    syncAccessPeopleFooterActions();
    scheduleResponsiveHydration(body);
    return;
  }
  body.innerHTML = rows.map(person => `
    <tr class="${_accessPeopleSelected.has(person.id) ? 'selected' : ''}" data-access-person-row="${esc(person.id)}">
      <td class="access-checkbox-cell access-cell-check"><input type="checkbox" class="access-row-check" data-person-check="${esc(person.id)}" aria-label="Selecionar ${esc(person.full_name)}" ${_accessPeopleSelected.has(person.id) ? 'checked' : ''}></td>
      <td class="access-cell-name">
        <div class="access-person-name-cell" title="${esc(person.full_name)} (${esc(accessPersonTypeLabel(person.person_type))})">
          ${accessPersonTypeIcon(person.person_type)}
          <strong>${esc(person.full_name)}</strong>
        </div>
      </td>
      <td class="access-cell-photo">${accessPersonFaceBadge(person)}</td>
      <td class="access-cell-document access-cell-nowrap" title="${esc(formatDocument(person.document_id) || '')}">${esc(formatDocument(person.document_id) || '-')}</td>
      <td class="access-cell-site access-cell-truncate" title="${esc(person.site || '')}">${esc(person.site || '-')}</td>
      <td class="access-cell-enrollment access-cell-nowrap" title="${esc(person.enrollment_code || '')}">${esc(person.enrollment_code || '-')}</td>
      <td class="access-cell-class access-cell-truncate" title="${esc(person.class_name || '')}">${esc(person.class_name || '-')}</td>
      <td class="access-cell-guardian access-cell-truncate" title="${esc(person.guardian_name || '')}">${esc(person.guardian_name || '-')}</td>
      <td class="access-cell-phone access-cell-nowrap" title="${esc(formatBrPhone(person.guardian_phone) || '')}">${esc(formatBrPhone(person.guardian_phone) || '-')}</td>
      <td class="access-cell-status">${accessPersonStatusBadge(person.active)}</td>
      <td class="access-cell-sync">${accessProvisionStatusBadge(person.provision_summary)}</td>
    </tr>
  `).join('');
  syncAccessPeopleFooterActions();
  scheduleResponsiveHydration(body);
  lucide.createIcons();
}

function renderAccessPeopleLoading() {
  const body = document.getElementById('accessPeopleBody');
  if (!body) return;
  body.innerHTML = '<tr class="empty-row"><td colspan="11">Carregando pessoas...</td></tr>';
  setText('accessPeopleCount', 'carregando');
  scheduleResponsiveHydration(body);
}

async function openAccessStudentsDrawer(filterKey = 'all', activeSite = null) {
  if (typeof _openDashDrawer !== 'function' || typeof _drawerRenderRows !== 'function') return;
  filterKey = filterKey || 'all';
  activeSite = activeSite || null;
  _openDashDrawer('Controle de Acesso', 'Alunos');

  let rows = [];
  try {
    const res = await apiJson('/api/access-control/people?person_type=student', { forceRefresh: true, cacheTtl: 0 });
    rows = res?.people || [];
  } catch (err) {
    _drawerRenderRows(`<div class="drawer-empty-state">Nao foi possivel carregar os alunos: ${esc(err?.message || err)}</div>`);
    return;
  }

  const rowSite = person => String(person.site || '').trim();
  const sites = [...new Set(rows.map(rowSite).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'pt'));
  if (activeSite && !sites.includes(activeSite)) activeSite = null;

  const siteRows = activeSite ? rows.filter(person => rowSite(person) === activeSite) : rows;
  const counts = {
    all: siteRows.length,
    active: siteRows.filter(person => person.active !== false).length,
    inactive: siteRows.filter(person => person.active === false).length,
    no_photo: siteRows.filter(person => !person.face_photo_path).length,
  };

  if (typeof _drawerFilterBar === 'function') {
    _drawerFilterBar(
      [
        { key: 'all', label: 'Todos', count: counts.all },
        { key: 'active', label: 'Ativos', count: counts.active },
        { key: 'inactive', label: 'Inativos', count: counts.inactive },
        { key: 'no_photo', label: 'Sem foto', count: counts.no_photo },
      ],
      filterKey,
      sites,
      activeSite,
      key => openAccessStudentsDrawer(key, activeSite),
      site => openAccessStudentsDrawer(filterKey, site)
    );
  }

  let filtered = siteRows;
  if (filterKey === 'active') filtered = filtered.filter(person => person.active !== false);
  if (filterKey === 'inactive') filtered = filtered.filter(person => person.active === false);
  if (filterKey === 'no_photo') filtered = filtered.filter(person => !person.face_photo_path);

  filtered.sort((a, b) => String(a.full_name || '').localeCompare(String(b.full_name || ''), 'pt', { numeric: true }));
  _drawerRenderRows(filtered.map(person => `
    <button class="drawer-item access-student-drawer-item" type="button" data-access-student-id="${esc(person.id)}" title="Editar aluno">
      ${_drawerStatusDot(person.active === false ? 'inactive' : 'active')}
      <span class="drawer-item-main">
        <span class="drawer-item-title">${esc(person.full_name || 'Aluno sem nome')}</span>
        <span class="drawer-item-sub">${esc([person.enrollment_code, person.class_name, person.site].filter(Boolean).join('  |  ') || 'Sem detalhes')}</span>
      </span>
      ${person.face_photo_path ? '<span class="drawer-mini-badge">foto</span>' : '<span class="drawer-mini-badge">sem foto</span>'}
      <i data-lucide="chevron-right" style="width:13px;height:13px;color:var(--muted);flex-shrink:0"></i>
    </button>
  `).join(''));

  document.querySelectorAll('[data-access-student-id]').forEach(button => {
    button.addEventListener('click', () => {
      const person = rows.find(row => row.id === button.dataset.accessStudentId);
      if (!person) return;
      closeDashDrawer();
      focusAccessPersonFromDrawer(person.id);
    });
  });
}

async function openAccessDevicesDrawer(filterKey = 'all', activeSite = null) {
  if (typeof _openDashDrawer !== 'function' || typeof _drawerRenderRows !== 'function') return;
  filterKey = filterKey || 'all';
  activeSite = activeSite || null;
  _openDashDrawer('Controle de Acesso', 'Dispositivos');

  let rows = [];
  try {
    await loadAccessConnectors(true).catch(() => []);
    const res = await apiJson('/api/access-control/devices', { forceRefresh: true, cacheTtl: 0 });
    rows = res?.devices || [];
    _accessDeviceRows = rows;
  } catch (err) {
    _drawerRenderRows(`<div class="drawer-empty-state">Nao foi possivel carregar os dispositivos: ${esc(err?.message || err)}</div>`);
    return;
  }

  const rowSite = device => String(device.site || '').trim();
  const sites = [...new Set(rows.map(rowSite).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'pt'));
  if (activeSite && !sites.includes(activeSite)) activeSite = null;

  const siteRows = activeSite ? rows.filter(device => rowSite(device) === activeSite) : rows;
  const isOnline = device => String(device.status || '').toLowerCase() === 'online';
  const hasConnector = device => Boolean(String(device.connector_id || '').trim());
  const counts = {
    all: siteRows.length,
    online: siteRows.filter(isOnline).length,
    offline: siteRows.filter(device => !isOnline(device)).length,
    local: siteRows.filter(device => !hasConnector(device)).length,
    connector: siteRows.filter(hasConnector).length,
  };

  if (typeof _drawerFilterBar === 'function') {
    _drawerFilterBar(
      [
        { key: 'all', label: 'Todos', count: counts.all },
        { key: 'online', label: 'Online', count: counts.online },
        { key: 'offline', label: 'Offline', count: counts.offline },
        { key: 'local', label: 'Local', count: counts.local },
        { key: 'connector', label: 'Conector', count: counts.connector },
      ],
      filterKey,
      sites,
      activeSite,
      key => openAccessDevicesDrawer(key, activeSite),
      site => openAccessDevicesDrawer(filterKey, site)
    );
  }

  let filtered = siteRows;
  if (filterKey === 'online') filtered = filtered.filter(isOnline);
  if (filterKey === 'offline') filtered = filtered.filter(device => !isOnline(device));
  if (filterKey === 'local') filtered = filtered.filter(device => !hasConnector(device));
  if (filterKey === 'connector') filtered = filtered.filter(hasConnector);

  filtered.sort((a, b) => String(a.name || a.host || '').localeCompare(String(b.name || b.host || ''), 'pt', { numeric: true }));
  _drawerRenderRows(filtered.map(device => `
    <button class="drawer-item access-device-drawer-item" type="button" data-access-drawer-device-id="${esc(device.id)}" title="Ver na aba Dispositivos">
      ${_drawerStatusDot(isOnline(device) ? 'active' : 'inactive')}
      <span class="drawer-item-main">
        <span class="drawer-item-title">${esc(device.name || 'Dispositivo sem nome')}</span>
        <span class="drawer-item-sub">${esc([device.host, device.model, device.site].filter(Boolean).join('  |  ') || 'Sem detalhes')}</span>
      </span>
      <span class="drawer-mini-badge">${esc(accessDeviceConnectorLabel(device))}</span>
      <i data-lucide="chevron-right" style="width:13px;height:13px;color:var(--muted);flex-shrink:0"></i>
    </button>
  `).join(''));

  document.querySelectorAll('[data-access-drawer-device-id]').forEach(button => {
    button.addEventListener('click', () => {
      const device = rows.find(row => row.id === button.dataset.accessDrawerDeviceId);
      if (!device) return;
      closeDashDrawer();
      focusAccessDeviceFromDrawer(device.id);
    });
  });
}

function valorSitePessoa() {
  const select = document.getElementById('accessPersonSite');
  if (select?.value === '__novo__') {
    return document.getElementById('accessPersonSiteNovo')?.value.trim() || '';
  }
  return select?.value?.trim() || '';
}

async function preencherSitesPessoa(atual = '') {
  // A fonte dos sites e a controladora: e o site dela que vai no evento e
  // decide por qual canal a notificacao sai. Digitar a mao abria espaco para
  // divergencia de uma letra, que quebra o roteamento em silencio.
  const select = document.getElementById('accessPersonSite');
  const novo = document.getElementById('accessPersonSiteNovo');
  if (!select) return;
  let sites = [];
  try {
    const res = await apiJson('/api/access-control/people/sites', { forceRefresh: true, cacheTtl: 0 });
    sites = res?.sites || [];
  } catch (err) {
    /* sem lista: sobra a opcao de digitar */
  }
  if (atual && !sites.includes(atual)) sites = sites.concat([atual]);
  select.innerHTML = '<option value="">Sem site</option>'
    + sites.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')
    + '<option value="__novo__">+ Outro site...</option>';
  select.value = atual && sites.includes(atual) ? atual : '';
  if (novo) {
    novo.hidden = true;
    novo.value = '';
  }
  if (!select.dataset.ligado) {
    select.dataset.ligado = '1';
    select.addEventListener('change', () => {
      const campo = document.getElementById('accessPersonSiteNovo');
      if (!campo) return;
      campo.hidden = select.value !== '__novo__';
      if (!campo.hidden) campo.focus();
    });
  }
}

function openAccessPersonModal(person = null) {
  const item = person || {};
  _accessPersonModalCurrent = item.id ? { ...item } : null;
  setText('accessPersonModalTitle', item.id ? 'Editar pessoa' : 'Nova pessoa');
  showAccessPersonTab('details');
  document.getElementById('accessPersonId').value = item.id || '';
  document.getElementById('accessPersonName').value = item.full_name || '';
  document.getElementById('accessPersonType').value = item.person_type || 'student';
  document.getElementById('accessPersonEnrollment').value = item.enrollment_code || '';
  document.getElementById('accessPersonDocument').value = item.document_id || '';
  document.getElementById('accessPersonClass').value = item.class_name || '';
  // save_person() no backend faz UPDATE completo (ON CONFLICT DO UPDATE SET
  // site=excluded.site), entao o formulario precisa carregar e reenviar o site
  // atual -- sem este campo, qualquer edicao pela UI apagava o site da pessoa.
  preencherSitesPessoa(item.site || '');
  document.getElementById('accessPersonControllerId').value = item.controller_user_id || '';
  const faceInput = document.getElementById('accessPersonFacePhoto');
  if (faceInput) faceInput.value = '';
  const faceStatus = document.getElementById('accessPersonFacePhotoStatus');
  if (faceStatus) faceStatus.textContent = '';
  document.getElementById('accessPersonGuardian').value = item.guardian_name || '';
  document.getElementById('accessPersonPhone').value = item.guardian_phone || '';
  document.getElementById('accessPersonNotes').value = item.notes || '';
  document.getElementById('accessPersonWhatsapp').checked = item.whatsapp_enabled !== false;
  document.getElementById('accessPersonActive').checked = item.active !== false;
  renderAccessPersonProvisionStatus(item);
  updateAccessPersonHero();
  updateAccessPersonFacePreview();
  const checklist = document.getElementById('accessPersonGroupsChecklist');
  if (checklist) checklist.innerHTML = '<p class="muted-block">Carregando grupos...</p>';
  document.getElementById('modalAccessPerson')?.classList.remove('hidden');
  ensureAccessPersonAccessData(false).then(ok => {
    if (!ok) return;
    renderAccessPersonGroupsChecklist(item);
    renderAccessPersonAccessPanel();
  });
  setTimeout(() => document.getElementById('accessPersonName')?.focus(), 50);
  lucide.createIcons();
}

function closeAccessPersonModal() {
  document.getElementById('modalAccessPerson')?.classList.add('hidden');
  _accessPersonModalCurrent = null;
  if (_accessPersonHeroPhotoObjectUrl) {
    URL.revokeObjectURL(_accessPersonHeroPhotoObjectUrl);
    _accessPersonHeroPhotoObjectUrl = '';
  }
}

function renderAccessPersonProvisionStatus(person = null, message = '') {
  const item = person || {};
  const statusEl = document.getElementById('accessPersonProvisionStatus');
  const heroStatusEl = document.getElementById('accessPersonHeroStatus');
  const hintEl = document.getElementById('accessPersonSyncStatus');
  if (statusEl) {
    statusEl.innerHTML = item.id
      ? accessProvisionStatusBadge(item.provision_summary)
      : '<span class="pill neutral">salve primeiro</span>';
  }
  if (heroStatusEl) {
    heroStatusEl.outerHTML = (item.id
      ? accessProvisionStatusBadge(item.provision_summary)
      : '<span class="pill neutral">salve primeiro</span>').replace('<span ', '<span id="accessPersonHeroStatus" ');
  }
  if (hintEl) {
    hintEl.textContent = message || (item.id
      ? 'Ao salvar, o SightOps atualiza a controladora automaticamente.'
      : 'Ao salvar, o SightOps cria a pessoa e sincroniza com a controladora.');
  }
}

async function saveAccessPersonFromForm(event) {
  event.preventDefault();
  const btn = document.getElementById('btnAccessPersonSave');
  const oldHtml = btn?.innerHTML;
  const siteEscolhido = valorSitePessoa();
  if (!siteEscolhido) {
    showToast('Escolha a escola/site da pessoa.', true);
    document.getElementById('accessPersonSite')?.focus();
    return;
  }

  const payload = {
    id: document.getElementById('accessPersonId').value.trim(),
    full_name: document.getElementById('accessPersonName').value.trim(),
    person_type: document.getElementById('accessPersonType').value,
    enrollment_code: document.getElementById('accessPersonEnrollment').value.trim(),
    document_id: document.getElementById('accessPersonDocument').value.trim(),
    class_name: document.getElementById('accessPersonClass').value.trim(),
    site: valorSitePessoa(),
    controller_user_id: document.getElementById('accessPersonControllerId').value.trim(),
    guardian_name: document.getElementById('accessPersonGuardian').value.trim(),
    guardian_phone: document.getElementById('accessPersonPhone').value.trim(),
    notes: document.getElementById('accessPersonNotes').value.trim(),
    whatsapp_enabled: document.getElementById('accessPersonWhatsapp').checked,
    active: document.getElementById('accessPersonActive').checked,
  };
  const faceFile = document.getElementById('accessPersonFacePhoto')?.files?.[0];
  if (!payload.full_name) {
    showToast('Informe o nome da pessoa.', true);
    return;
  }
  const cpfDigitos = payload.document_id.replace(/\D/g, '');
  if (cpfDigitos.length !== 11) {
    showToast('Informe o CPF (11 digitos) da pessoa.', true);
    document.getElementById('accessPersonDocument')?.focus();
    return;
  }
  payload.document_id = cpfDigitos;
  if (payload.whatsapp_enabled && !payload.guardian_phone) {
    showToast('Informe o WhatsApp do responsavel ou desmarque o envio de notificacoes.', true);
    document.getElementById('accessPersonPhone')?.focus();
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Salvando e sincronizando';
    lucide.createIcons();
  }
  try {
    const res = await api('/api/access-control/people', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    const saved = await jsonOrReadableError(res, 'Nao foi possivel salvar a pessoa.');
    const person = saved?.person;
    if (faceFile && person?.id) {
      const formData = new FormData();
      formData.append('file', faceFile);
      const headers = {};
      if (_token) headers.Authorization = `Bearer ${_token}`;
      const photoRes = await fetch(`${API_BASE}/api/access-control/people/${encodeURIComponent(person.id)}/face-photo`, {
        method: 'POST',
        credentials: 'same-origin',
        headers,
        body: formData,
      });
      await jsonOrReadableError(photoRes, 'Nao foi possivel salvar a foto facial.');
      clearApiJsonCache();
    }
    await saveAccessPersonGroupMembership(person.id);
    const syncedPerson = await syncAccessPersonAfterSave(person);
    closeAccessPersonModal();
    await loadAccessControl(true);
    loadAccessPeopleSiteOptions();
    renderAccessPersonProvisionStatus(syncedPerson);
    showToast('Pessoa salva e sincronizada.');
  } catch (err) {
    if (_accessPersonModalCurrent?.id) {
      await loadAccessControl(true);
      loadAccessPeopleSiteOptions();
      renderAccessPersonProvisionStatus(_accessPersonModalCurrent, err?.message || 'Pessoa salva, mas nao foi possivel sincronizar.');
    }
    showToast(err?.message || 'Nao foi possivel salvar a pessoa.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="save"></i> Salvar e sincronizar';
      lucide.createIcons();
    }
  }
}

async function syncAccessPersonAfterSave(person) {
  if (!person?.id) return person;
  renderAccessPersonProvisionStatus(person, 'Sincronizando com a controladora...');
  _accessPersonModalCurrent = { ...person };
  const res = await api(`/api/access-control/people/${encodeURIComponent(person.id)}/sync`, { method: 'POST' });
  const payload = await jsonOrReadableError(res, 'Pessoa salva, mas nao foi possivel sincronizar com a controladora.');
  const syncedPerson = { ...person, provision_summary: payload?.provision_summary };
  _accessPersonModalCurrent = syncedPerson;
  return syncedPerson;
}

function baixarModeloPlanilhaAlunos() {
  // Mesmas colunas que app/services/access_control_import.py reconhece --
  // usar o primeiro apelido de cada campo, que e o nome mostrado na ajuda.
  const linhas = [
    ['matricula', 'nome', 'telefone do responsavel', 'responsavel', 'turma', 'cpf', 'id da controladora'],
    ['1001', 'Nome Completo do Aluno', '82999998888', 'Nome do Responsavel', '5º Ano', '000.000.000-00', ''],
  ];
  const conteudo = linhas.map(linha => linha.map(v => `"${String(v).replaceAll('"', '""')}"`).join(';')).join('\r\n');
  const blob = new Blob([`﻿${conteudo}`], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'sightops-modelo-importacao-alunos.csv';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function abrirImportacaoPlanilha() {
  const modal = document.getElementById('modalAccessSheetImport');
  if (!modal) return;
  modal.classList.remove('hidden');
  const previa = document.getElementById('accessSheetPreview');
  if (previa) previa.hidden = true;
  const aplicar = document.getElementById('btnAccessSheetApply');
  if (aplicar) aplicar.disabled = true;
  const arquivo = document.getElementById('accessSheetFile');
  if (arquivo) arquivo.value = '';
  const nomeArquivo = document.getElementById('accessSheetFileName');
  if (nomeArquivo) nomeArquivo.textContent = 'Nenhum arquivo escolhido';

  const select = document.getElementById('accessSheetSite');
  if (!select) return;
  try {
    const res = await apiJson('/api/access-control/people/sites', { forceRefresh: true, cacheTtl: 0 });
    const sites = res?.sites || [];
    select.innerHTML = '<option value="">Manter o site atual de cada aluno</option>'
      + sites.map(site => `<option value="${esc(site)}">${esc(site)}</option>`).join('');
  } catch (err) {
    /* sem sites cadastrados ainda: a opcao padrao ja serve */
  }
}

function fecharImportacaoPlanilha() {
  document.getElementById('modalAccessSheetImport')?.classList.add('hidden');
}

function mostrarPreviaPlanilha(dados, aplicado) {
  const previa = document.getElementById('accessSheetPreview');
  if (!previa) return;
  previa.hidden = false;

  const criar = aplicado ? (dados.criados || 0) : (dados.criar || []).length;
  const atualizar = aplicado ? (dados.atualizados || 0) : (dados.atualizar || []).length;
  const recusados = dados.recusados || [];
  const falhas = dados.falhas || [];
  const semTelefone = dados.sem_telefone || 0;

  const cartao = (rotulo, valor, cor) =>
    `<div class="access-sheet-card"><strong style="color:${cor}">${valor}</strong><span>${rotulo}</span></div>`;

  let html = '<div class="access-sheet-cards">'
    + cartao(aplicado ? 'criados' : 'serao criados', criar, 'var(--ok, #0f7b5f)')
    + cartao(aplicado ? 'atualizados' : 'serao atualizados', atualizar, 'var(--text)')
    + cartao('recusados', recusados.length + falhas.length, recusados.length + falhas.length ? '#b42318' : 'var(--muted)')
    + '</div>';

  if (semTelefone) {
    html += `<div class="inline-help" style="margin-top:10px">${semTelefone} aluno(s) sem telefone do responsavel.
      Entram no cadastro, mas <strong>nao recebem notificacao</strong> ate o telefone ser preenchido.</div>`;
  }

  const problemas = recusados.concat(falhas);
  if (problemas.length) {
    html += '<div class="access-sheet-erros"><table class="data-table"><thead><tr>'
      + '<th>Linha</th><th>Matricula</th><th>Nome</th><th>Motivo</th></tr></thead><tbody>'
      + problemas.slice(0, 40).map(r => `<tr><td>${esc(r.linha ?? '-')}</td><td>${esc(r.matricula || '-')}</td>`
        + `<td>${esc(r.nome || '-')}</td><td>${esc(r.motivo || '-')}</td></tr>`).join('')
      + '</tbody></table>';
    if (problemas.length > 40) html += `<div class="inline-help">e mais ${problemas.length - 40} linha(s).</div>`;
    html += '</div>';
  }

  previa.innerHTML = html;
}

async function enviarPlanilha(aplicar) {
  const entrada = document.getElementById('accessSheetFile');
  const arquivo = entrada?.files?.[0];
  if (!arquivo) {
    showToast('Escolha a planilha primeiro.', true);
    return;
  }
  const site = document.getElementById('accessSheetSite')?.value || '';
  const btn = document.getElementById(aplicar ? 'btnAccessSheetApply' : 'btnAccessSheetAnalyze');
  const htmlAntigo = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader-circle"></i> ${aplicar ? 'Importando' : 'Conferindo'}`;
    lucide.createIcons();
  }
  try {
    const corpo = new FormData();
    corpo.append('arquivo', arquivo);
    const url = `/api/access-control/people/import?site=${encodeURIComponent(site)}&aplicar=${aplicar ? 'true' : 'false'}`;
    const res = await api(url, { method: 'POST', body: corpo });
    const dados = await jsonOrReadableError(res, 'Nao foi possivel ler a planilha.');
    mostrarPreviaPlanilha(dados, aplicar);
    if (aplicar) {
      showToast(`Importado: ${dados.criados} criado(s), ${dados.atualizados} atualizado(s).`);
      await loadAccessControl(true);   // recarrega tabela e KPIs, como o botao Atualizar
      const botao = document.getElementById('btnAccessSheetApply');
      if (botao) botao.disabled = true;
    } else {
      const total = (dados.criar || []).length + (dados.atualizar || []).length;
      const botao = document.getElementById('btnAccessSheetApply');
      if (botao) botao.disabled = total === 0;
      if (!total) showToast('Nenhuma linha aproveitavel na planilha.', true);
    }
  } catch (err) {
    showToast(err?.message || 'Falha ao processar a planilha.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = htmlAntigo;
      lucide.createIcons();
    }
  }
}

async function openAccessPeopleImportModal() {
  const modal = document.getElementById('modalAccessPeopleImport');
  const select = document.getElementById('accessPeopleImportDevice');
  const result = document.getElementById('accessPeopleImportResult');
  if (!modal || !select) return;
  modal.classList.remove('hidden');
  if (result) result.textContent = 'Carregando controladoras cadastradas...';
  select.innerHTML = '<option value="">Carregando controladoras...</option>';
  try {
    const res = await apiJson('/api/access-control/devices', { forceRefresh: true, cacheTtl: 0 });
    _accessPeopleImportDeviceRows = Array.isArray(res?.devices) ? res.devices : [];
    const devices = _accessPeopleImportDeviceRows
      .filter(device => device?.id)
      .sort((a, b) => String(a.site || '').localeCompare(String(b.site || ''), 'pt-BR')
        || String(a.name || '').localeCompare(String(b.name || ''), 'pt-BR'));
    select.innerHTML = devices.length
      ? '<option value="">Escolha uma controladora</option>' + devices.map(device => {
        const meta = [device.site, device.host, device.model].filter(Boolean).join(' - ');
        const label = meta ? `${device.name || device.host} (${meta})` : (device.name || device.host || device.id);
        return `<option value="${esc(device.id)}">${esc(label)}</option>`;
      }).join('')
      : '<option value="">Nenhuma controladora cadastrada</option>';
    if (result) result.textContent = devices.length
      ? 'Escolha uma controladora para importar pessoas e fotos.'
      : 'Cadastre uma controladora antes de importar pessoas.';
  } catch (err) {
    select.innerHTML = '<option value="">Falha ao carregar</option>';
    if (result) result.textContent = err?.message || 'Nao foi possivel carregar controladoras.';
    showToast(err?.message || 'Nao foi possivel carregar controladoras.', true);
  }
  lucide.createIcons();
}

function closeAccessPeopleImportModal() {
  document.getElementById('modalAccessPeopleImport')?.classList.add('hidden');
}

async function importAccessPeopleFromSelectedDevice() {
  const select = document.getElementById('accessPeopleImportDevice');
  const result = document.getElementById('accessPeopleImportResult');
  const btn = document.getElementById('btnAccessPeopleImportRun');
  const deviceId = select?.value || '';
  if (!deviceId) {
    showToast('Escolha uma controladora para importar.', true);
    return;
  }
  const device = _accessPeopleImportDeviceRows.find(row => row.id === deviceId);
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-2"></i> Importando...';
    lucide.createIcons();
  }
  if (result) result.textContent = `Lendo cadastros de ${device?.name || device?.host || 'controladora'}...`;
  try {
    const res = await api(`/api/access-control/devices/${encodeURIComponent(deviceId)}/import-people`, { method: 'POST' });
    const payload = await jsonOrReadableError(res, 'Nao foi possivel importar pessoas da controladora.');
    const msg = `${payload.imported || 0} pessoa(s) importada(s), ${payload.photos_imported || 0} foto(s), ${payload.photos_missing || 0} sem foto.`;
    if (result) result.textContent = msg;
    _accessPeopleSelected.clear();
    await loadAccessControl(true);
    loadAccessPeopleSiteOptions();
    showToast(msg);
  } catch (err) {
    if (result) result.textContent = err?.message || 'Erro ao importar pessoas.';
    showToast(err?.message || 'Erro ao importar pessoas.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="download"></i> Importar';
      lucide.createIcons();
    }
  }
}

let _accessDeviceRows = [];
let _accessConnectorRows = [];
let _accessConnectorsLoaded = false;
let _accessDeviceDeleteConfirmId = '';
let _accessDeviceSelectedId = '';

function showAccessControlTab(tab) {
  const targetBtn = document.querySelector(`.access-control-tabs [data-access-tab="${tab}"]`);
  if (!targetBtn) return;
  document.querySelectorAll('.access-control-tabs [data-access-tab]').forEach(btn => {
    const active = btn === targetBtn;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.access-tab-panel').forEach(panel => {
    panel.hidden = panel.dataset.accessPanel !== tab;
  });
  updateAccessPrimaryAction(tab);
}

function handleAccessTabClick(tab, event) {
  if (event?.__accessTabHandled) return;
  if (event) event.__accessTabHandled = true;
  event?.preventDefault?.();
  if (!tab) return;
  showAccessControlTab(tab);
  if (tab === 'people') loadAccessControl(true);
  if (tab === 'devices') loadAccessDevices(true);
  if (tab === 'groups') loadAccessGroups(true);
  if (tab === 'rules') loadAccessRules(true);
  if (tab === 'connections') loadAccessWhatsappConfig(true);
  if (tab === 'reports') {
    loadAccessControlSummary(true);
    loadAccessReports(true);
    startAccessReportAutoRefresh();
  } else {
    stopAccessReportAutoRefresh();
  }
}

function openAccessTodayEventsReport() {
  const period = document.getElementById('accessReportPeriod');
  const eventType = document.getElementById('accessReportType');
  const site = document.getElementById('accessReportSite');
  const search = document.getElementById('accessReportSearch');
  if (period) period.value = 'today';
  if (eventType) eventType.value = '';
  if (site) site.value = '';
  if (search) search.value = '';
  syncAccessReportCustomRange();
  showAccessControlTab('reports');
  loadAccessControlSummary(true);
  loadAccessReports(true);
  startAccessReportAutoRefresh();
}

function handleAccessKpiKeydown(target, event) {
  if (event?.key !== 'Enter' && event?.key !== ' ') return;
  event.preventDefault();
  if (target === 'events') openAccessTodayEventsReport();
  if (target === 'whatsapp') openAccessWhatsappConnections();
}

function openAccessWhatsappConnections() {
  stopAccessReportAutoRefresh();
  showAccessControlTab('connections');
  loadAccessWhatsappConfig(true);
}

function bindAccessConnections() {
  document.getElementById('btnAccessWhatsappReload')?.addEventListener('click', () => loadAccessWhatsappConfig(true));
  document.getElementById('btnAccessWhatsappSave')?.addEventListener('click', saveAccessWhatsappConfig);
  document.getElementById('btnAccessWhatsappTest')?.addEventListener('click', testAccessWhatsappConfig);
  document.getElementById('btnAccessWhatsappConnection')?.addEventListener('click', verificarCanalWhatsapp);
  document.getElementById('accessWhatsappSite')?.addEventListener('change', () => loadAccessWhatsappConfig(true));
  document.getElementById('accessWhatsappProvider')?.addEventListener('change', updateAccessWhatsappProviderUi);
}

function accessWhatsappSiteValue() {
  return document.getElementById('accessWhatsappSite')?.value || '';
}

function accessWhatsappSiteQuery() {
  const site = accessWhatsappSiteValue();
  return site ? `?site=${encodeURIComponent(site)}` : '';
}

function accessWhatsappProviderValue() {
  // sem o select na tela o default e o canal oficial, igual ao backend: cair
  // em 'evolution' aqui escondia os campos da Meta de quem usa Cloud API
  return (document.getElementById('accessWhatsappProvider')?.value || 'cloud_api').toLowerCase();
}

// A API oficial nao tem sessao nem QR: a autenticacao e um token permanente,
// entao a metade da tela pensada para parear aparelho nao se aplica.
function isAccessWhatsappCloudProvider(provider = accessWhatsappProviderValue()) {
  return provider === 'cloud_api' || provider === 'cloud' || provider === 'oficial';
}

function updateAccessWhatsappProviderUi() {
  const cloud = isAccessWhatsappCloudProvider();
  const alterna = (id, mostrar) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', !mostrar);
  };
  alterna('accessWhatsappPhoneIdGroup', cloud);
  alterna('accessWhatsappCloudRow', cloud);
  alterna('accessWhatsappTemplateRow', cloud);
  alterna('accessWhatsappCloudBox', cloud);
  alterna('accessWhatsappEvolutionBox', !cloud);
  alterna('btnAccessWhatsappDisconnect', !cloud);
}

function setAccessWhatsappStatus(config = null) {
  const status = document.getElementById('accessWhatsappStatus');
  if (!status) return;
  const configured = !!config?.configured;
  const enabled = !!config?.enabled;
  const siteConfigured = !!config?.site_configured;
  status.textContent = configured ? (enabled ? (config?.site ? (siteConfigured ? 'Ativo no site' : 'Ativo padrao') : 'Ativo') : 'Configurado') : 'Nao configurado';
  status.className = `badge ${configured && enabled ? 'badge-green' : 'badge-gray'}`;
}

async function loadAccessWhatsappConfig(force = false) {
  try {
    await loadAccessWhatsappSiteOptions(force);
    const data = await apiJson(`/api/access-control/whatsapp${accessWhatsappSiteQuery()}`, { forceRefresh: force, cacheTtl: 0 });
    _accessWhatsappCurrentConfigured = !!data.configured;
    document.getElementById('accessWhatsappProvider').value = data.provider || 'cloud_api';
    document.getElementById('accessWhatsappEnabled').checked = !!data.enabled;
    const porId = (id, valor) => {
      const el = document.getElementById(id);
      if (el) el.value = valor || '';
    };
    porId('accessWhatsappPhoneId', data.phone_number_id);
    porId('accessWhatsappWabaId', data.waba_id);
    porId('accessWhatsappTemplate', data.template_name);
    porId('accessWhatsappTemplateLang', data.template_language || 'pt_BR');
    porId('accessWhatsappToken', '');   // o token nunca volta do servidor
    updateAccessWhatsappProviderUi();
    setAccessWhatsappStatus(data);
    await loadAccessWhatsappConnection(force);
  } catch (err) {
    _accessWhatsappCurrentConfigured = false;
    setAccessWhatsappStatus(null);
    setAccessWhatsappConnection(null);
    showToast(err?.message || 'Nao foi possivel carregar o WhatsApp.', true);
  }
}

async function saveAccessWhatsappConfig() {
  const btn = document.getElementById('btnAccessWhatsappSave');
  const oldHtml = btn?.innerHTML;
  const payload = {
    site: accessWhatsappSiteValue(),
    enabled: !!document.getElementById('accessWhatsappEnabled')?.checked,
    provider: document.getElementById('accessWhatsappProvider')?.value || 'cloud_api',
  };
  if (isAccessWhatsappCloudProvider(payload.provider)) {
    payload.phone_number_id = document.getElementById('accessWhatsappPhoneId')?.value.trim() || '';
    payload.waba_id = document.getElementById('accessWhatsappWabaId')?.value.trim() || '';
    payload.access_token = document.getElementById('accessWhatsappToken')?.value.trim() || '';
    payload.template_name = document.getElementById('accessWhatsappTemplate')?.value.trim() || '';
    payload.template_language = document.getElementById('accessWhatsappTemplateLang')?.value.trim() || 'pt_BR';
    if (payload.enabled && !payload.phone_number_id) {
      showToast('Informe o Phone Number ID para ativar a API oficial.', true);
      document.getElementById('accessWhatsappPhoneId')?.focus();
      return null;
    }
    if (payload.enabled && !payload.access_token && !_accessWhatsappCurrentConfigured) {
      showToast('Informe o token de acesso para ativar a API oficial.', true);
      document.getElementById('accessWhatsappToken')?.focus();
      return null;
    }
  }
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Salvando';
    lucide.createIcons();
  }
  try {
    const res = await api('/api/access-control/whatsapp', { method: 'PUT', body: JSON.stringify(payload) });
    const data = await jsonOrReadableError(res, 'Nao foi possivel salvar o WhatsApp.');
    const campoToken = document.getElementById('accessWhatsappToken');
    if (campoToken) campoToken.value = '';
    _accessWhatsappCurrentConfigured = !!data.configured;
    setAccessWhatsappStatus(data);
    await loadAccessWhatsappConnection(true);
    const avisoIncompleto = isAccessWhatsappCloudProvider(payload.provider)
      ? 'WhatsApp salvo, mas falta o Phone Number ID e o token da API oficial.'
      : 'WhatsApp salvo, mas falta configurar URL/chave do Evolution na plataforma.';
    showToast(data.configured ? 'WhatsApp salvo.' : avisoIncompleto);
    return data;
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel salvar o WhatsApp.', true);
    return null;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="save"></i> Salvar';
      lucide.createIcons();
    }
  }
}

function setAccessWhatsappConnection(data = null) {
  // Canal oficial: nao ha sessao para cair. O painel mostra de quem a mensagem
  // sai e em que estado esta o modelo -- que e o que costuma travar o envio.
  const status = document.getElementById('accessWhatsappConnectionStatus');
  const escreve = (id, valor) => {
    const el = document.getElementById(id);
    if (el) el.textContent = valor || '-';
  };
  const configurado = !!data?.configured;

  const ESTADOS_EVOLUTION = {
    connected: 'Conectado', waiting_qr: 'Aguardando leitura do QR Code',
    disconnected: 'Desconectado', not_configured: 'Nao configurado',
    error: 'Erro ao consultar', unknown: 'Desconhecido',
  };
  const estadoTexto = ESTADOS_EVOLUTION[String(data?.state || '')] || data?.state || '-';

  // Para o Evolution, "configurado" so quer dizer que a plataforma tem
  // URL e chave -- nao diz nada sobre a sessao estar viva. Pintar verde por
  // isso repetiria em menor escala o bug que esta feature existe para
  // evitar: a tela dizendo Conectado com a sessao morta. No canal oficial
  // nao ha sessao para cair, entao "configurado" continua sendo o sinal certo.
  const provider = String(
    data?.provider || (isAccessWhatsappCloudProvider() ? 'cloud_api' : 'evolution'),
  ).toLowerCase();
  const conectado = provider === 'evolution' ? !!data?.connected : configurado;

  if (status) {
    if (!data) {
      status.textContent = 'Indisponivel';
      status.className = 'badge badge-gray';
    } else if (!configurado) {
      status.textContent = 'Nao configurado';
      status.className = 'badge badge-gray';
    } else if (conectado) {
      status.textContent = 'Pronto';
      status.className = 'badge badge-green';
    } else {
      status.textContent = estadoTexto;
      status.className = 'badge badge-amber';
    }
  }

  escreve('accessWhatsappCloudNumber', data?.display_phone_number || data?.phone_number_id);
  escreve('accessWhatsappCloudName', data?.verified_name);

  const QUALIDADE = { GREEN: 'Boa', YELLOW: 'Media', RED: 'Baixa', UNKNOWN: 'Sem historico' };
  escreve('accessWhatsappCloudQuality', QUALIDADE[String(data?.quality_rating || '').toUpperCase()]);

  const SITUACAO = { APPROVED: 'aprovado', PENDING: 'em analise', REJECTED: 'reprovado' };
  const modelo = data?.template_name;
  const situacao = SITUACAO[String(data?.template_status || '').toUpperCase()];
  escreve('accessWhatsappCloudTemplate', modelo ? (situacao ? `${modelo} (${situacao})` : modelo) : null);

  // sem restaurar o texto padrao, um erro de um site ficava grudado ao trocar
  // para outro que esta funcionando
  const nota = document.getElementById('accessWhatsappCloudHint');
  if (nota) {
    nota.textContent = data?.error || 'Sem QR Code e sem sessao: a autenticacao e um token permanente.';
  }

  const escreveEvolution = (id, valor) => {
    const el = document.getElementById(id);
    if (el) el.textContent = valor || '-';
  };
  escreveEvolution('accessWhatsappEvolutionInstance', data?.instance);
  escreveEvolution('accessWhatsappEvolutionState', estadoTexto);
  // A caixa do QR Code so aparece quando tem um QR de verdade pra mostrar --
  // do contrario fica um retangulo vazio do tamanho do QR Code, que parece
  // erro de tela em vez de "sessao ja conectada, nao precisa de nada".
  const qrBox = document.getElementById('accessWhatsappEvolutionQrBox');
  const imgQr = document.getElementById('accessWhatsappEvolutionQr');
  if (qrBox && imgQr) {
    if (data?.qrcode) {
      imgQr.src = data.qrcode.startsWith('data:') ? data.qrcode : `data:image/png;base64,${data.qrcode}`;
      imgQr.hidden = false;
      qrBox.classList.remove('hidden');
    } else {
      imgQr.hidden = true;
      qrBox.classList.add('hidden');
    }
  }
  const notaEvolution = document.getElementById('accessWhatsappEvolutionHint');
  if (notaEvolution) {
    if (data?.error) {
      notaEvolution.textContent = data.error;
    } else if (data?.qrcode) {
      notaEvolution.textContent = 'Escaneie o QR Code no WhatsApp do celular da escola (Aparelhos conectados).';
    } else if (data?.connected) {
      notaEvolution.textContent = 'Sessao conectada: nenhum QR Code necessario.';
    } else {
      notaEvolution.textContent = 'Clique em "Verificar conexao" para gerar o QR Code.';
    }
  }
}

async function verificarCanalWhatsapp() {
  // Sem retorno visual a verificacao parece nao fazer nada: os valores quase
  // nunca mudam, entao repintar em silencio e indistinguivel de botao quebrado.
  const btn = document.getElementById('btnAccessWhatsappConnection');
  const htmlAntigo = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Verificando';
    lucide.createIcons();
  }
  try {
    const data = await loadAccessWhatsappConnection(true);
    if (!data) {
      showToast(isAccessWhatsappCloudProvider() ? 'Nao foi possivel falar com a Meta.' : 'Nao foi possivel falar com o Evolution.', true);
    } else if (!data.configured) {
      showToast(data.error || 'Canal ainda nao configurado.', true);
    } else if (String(data.provider || (isAccessWhatsappCloudProvider() ? 'cloud_api' : 'evolution')).toLowerCase() === 'evolution') {
      if (data.connected) {
        showToast(`Sessao conectada (instancia ${data.instance || '-'}).`);
      } else if (data.state === 'error') {
        showToast(data.error || 'Erro ao consultar o Evolution.', true);
      } else {
        const ESTADOS = {
          waiting_qr: 'aguardando leitura do QR Code',
          disconnected: 'desconectada',
          unknown: 'em estado desconhecido',
          not_configured: 'nao configurada',
        };
        showToast(`Sessao ${ESTADOS[data.state] || 'nao conectada'} (instancia ${data.instance || '-'}).`);
      }
    } else {
      const modelo = data.template_status === 'APPROVED' ? 'modelo aprovado'
        : data.template_status === 'PENDING' ? 'modelo em analise'
        : data.template_status === 'REJECTED' ? 'modelo reprovado'
        : 'usando modelo padrao';
      showToast(`Canal ativo em ${data.display_phone_number || data.phone_number_id} (${modelo}).`);
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = htmlAntigo;
      lucide.createIcons();
    }
  }
}

async function desconectarCanalWhatsapp() {
  const btn = document.getElementById('btnAccessWhatsappDisconnect');
  const htmlAntigo = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Desconectando';
    lucide.createIcons();
  }
  try {
    const res = await api('/api/access-control/whatsapp/disconnect', { method: 'POST', body: JSON.stringify({ site: accessWhatsappSiteValue() }) });
    const data = await jsonOrReadableError(res, 'Nao foi possivel desconectar o WhatsApp.');
    showToast('Sessao desconectada.');
    await loadAccessWhatsappConnection(true);
    return data;
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel desconectar o WhatsApp.', true);
    return null;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = htmlAntigo;
      lucide.createIcons();
    }
  }
}

document.getElementById('btnAccessWhatsappDisconnect')?.addEventListener('click', desconectarCanalWhatsapp);

async function loadAccessWhatsappConnection(force = false) {
  try {
    const params = new URLSearchParams();
    const site = accessWhatsappSiteValue();
    if (site) params.set('site', site);
    if (!isAccessWhatsappCloudProvider()) params.set('refresh_qr', '1');
    const query = params.toString() ? `?${params.toString()}` : '';
    const data = await apiJson(`/api/access-control/whatsapp/connection${query}`, { forceRefresh: force, cacheTtl: 0 });
    setAccessWhatsappConnection(data);
    return data;
  } catch (err) {
    setAccessWhatsappConnection({ state: 'error', error: err?.message || 'Nao foi possivel consultar a conexao.' });
    return null;
  }
}

async function testAccessWhatsappConfig() {
  const saved = await saveAccessWhatsappConfig();
  if (!saved) return;
  const number = document.getElementById('accessWhatsappTestNumber')?.value.trim() || '';
  const status = document.getElementById('accessWhatsappTestStatus');
  const result = document.getElementById('accessWhatsappTestResult');
  if (!number) {
    showToast('Informe o numero de teste.', true);
    return;
  }
  const btn = document.getElementById('btnAccessWhatsappTest');
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Enviando';
    lucide.createIcons();
  }
  if (status) {
    status.textContent = 'Enviando';
    status.className = 'badge badge-gray';
  }
  try {
    const res = await api('/api/access-control/whatsapp/test', { method: 'POST', body: JSON.stringify({ number, site: accessWhatsappSiteValue() }) });
    const data = await jsonOrReadableError(res, 'Nao foi possivel enviar o teste.');
    if (status) {
      const pending = data.status === 'whatsapp_pending';
      status.textContent = data.status === 'whatsapp_sent' ? 'Enviado' : (pending ? 'Pendente' : 'Verifique');
      status.className = `badge ${data.status === 'whatsapp_sent' ? 'badge-green' : (pending ? 'badge-amber' : 'badge-gray')}`;
    }
    if (result) result.textContent = data.status === 'whatsapp_pending' ? 'A API aceitou a mensagem, mas ainda nao confirmou entrega.' : 'Teste enviado. Confira o WhatsApp do numero informado.';
    showToast(data.status === 'whatsapp_pending' ? 'Teste aceito pela API.' : 'Teste de WhatsApp enviado.');
  } catch (err) {
    if (status) {
      status.textContent = 'Erro';
      status.className = 'badge badge-red';
    }
    if (result) result.textContent = err?.message || 'Nao foi possivel enviar o teste.';
    showToast(err?.message || 'Nao foi possivel enviar o teste.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="send"></i> Enviar teste';
      lucide.createIcons();
    }
  }
}

function bindAccessTabs() {
  document.querySelectorAll('.access-control-tabs [data-access-tab]').forEach(btn => {
    btn.addEventListener('click', event => handleAccessTabClick(btn.dataset.accessTab, event));
  });
}

function bindAccessDevices() {
  document.getElementById('btnAccessDeviceNew')?.addEventListener('click', () => openAccessDeviceModal());
  document.getElementById('btnAccessDevicesRefresh')?.addEventListener('click', () => loadAccessDevices(true));
  document.getElementById('btnAccessDeviceClose')?.addEventListener('click', closeAccessDeviceModal);
  document.getElementById('btnAccessDeviceCancel')?.addEventListener('click', closeAccessDeviceModal);
  document.getElementById('accessDeviceForm')?.addEventListener('submit', saveAccessDeviceFromForm);
  document.getElementById('accessDevicesBody')?.addEventListener('click', handleAccessDeviceAction);
  document.getElementById('accessDevicesSelectAll')?.addEventListener('change', toggleAccessDeviceMasterCheck);
  document.getElementById('btnAccessDevicesFooterRefresh')?.addEventListener('click', refreshAccessDevicesFromButton);
  document.getElementById('btnAccessDevicesFooterTest')?.addEventListener('click', testSelectedAccessDevice);
  document.getElementById('btnAccessDevicesFooterOpenDoor')?.addEventListener('click', openSelectedAccessDeviceDoor);
  document.getElementById('btnAccessDevicesFooterEdit')?.addEventListener('click', editSelectedAccessDevice);
  document.getElementById('btnAccessDevicesFooterDelete')?.addEventListener('click', deleteSelectedAccessDevice);
}

function bindAccessReports() {
  document.getElementById('btnAccessReportRefresh')?.addEventListener('click', () => loadAccessReports(true));
  document.getElementById('btnAccessReportPdf')?.addEventListener('click', printAccessReportPdf);
  document.getElementById('btnAccessReportCsv')?.addEventListener('click', exportAccessReportCsv);
  document.getElementById('btnAccessReportToggleEvents')?.addEventListener('click', toggleAccessReportEvents);
  document.getElementById('btnAccessManualExit')?.addEventListener('click', recordAccessManualExit);
  document.getElementById('accessReportPeriod')?.addEventListener('change', () => {
    syncAccessReportCustomRange();
    loadAccessReports(true);
  });
  document.getElementById('accessReportStart')?.addEventListener('change', () => loadAccessReports(true));
  document.getElementById('accessReportEnd')?.addEventListener('change', () => loadAccessReports(true));
  document.getElementById('accessReportType')?.addEventListener('change', () => loadAccessReports(true));
  document.getElementById('accessReportSite')?.addEventListener('change', () => loadAccessReports(true));
  document.getElementById('accessReportSearch')?.addEventListener('input', () => {
    clearTimeout(_accessReportSearchTimer);
    _accessReportSearchTimer = setTimeout(() => loadAccessReports(true), 280);
  });
  syncAccessReportCustomRange();
}

function accessReportLocalDateTime(date) {
  const pad = value => String(value).padStart(2, '0');
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
  ].join('-') + `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function syncAccessReportCustomRange() {
  const period = document.getElementById('accessReportPeriod')?.value || 'today';
  const range = document.getElementById('accessReportCustomRange');
  const startInput = document.getElementById('accessReportStart');
  const endInput = document.getElementById('accessReportEnd');
  if (!range || !startInput || !endInput) return;
  const custom = period === 'custom';
  range.hidden = !custom;
  if (!custom) return;
  const now = new Date();
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  if (!startInput.value) startInput.value = accessReportLocalDateTime(start);
  if (!endInput.value) endInput.value = accessReportLocalDateTime(now);
}

function accessReportDateTimeParam(id, endOfMinute = false) {
  const value = document.getElementById(id)?.value || '';
  if (!value) return '';
  const clean = value.replace('T', ' ');
  if (clean.length === 16) return `${clean}:${endOfMinute ? '59' : '00'}`;
  return clean;
}

function accessReportQuery() {
  const query = new URLSearchParams();
  const period = document.getElementById('accessReportPeriod')?.value || 'today';
  const eventType = document.getElementById('accessReportType')?.value || '';
  const site = document.getElementById('accessReportSite')?.value || '';
  const search = document.getElementById('accessReportSearch')?.value?.trim() || '';
  if (period) query.set('period', period);
  if (period === 'custom') {
    const start = accessReportDateTimeParam('accessReportStart');
    const end = accessReportDateTimeParam('accessReportEnd', true);
    if (start) query.set('start', start);
    if (end) query.set('end', end);
  }
  if (eventType) query.set('type', eventType);
  if (site) query.set('site', site);
  if (search) query.set('search', search);
  return query;
}

function populateAccessReportSiteOptions(selected = '') {
  const select = document.getElementById('accessReportSite');
  if (!select) return;
  const current = selected || select.value || '';
  const sites = new Set();
  _accessPeopleRows.forEach(row => row.site && sites.add(row.site));
  _accessDeviceRows.forEach(row => row.site && sites.add(row.site));
  _accessReportRows.forEach(row => row.site && sites.add(row.site));
  const html = ['<option value="">Todos os sites</option>']
    .concat([...sites].sort((a, b) => a.localeCompare(b)).map(site => `<option value="${esc(site)}">${esc(site)}</option>`));
  select.innerHTML = html.join('');
  if (current && sites.has(current)) select.value = current;
}

function populateAccessManualExitPeople() {
  const select = document.getElementById('accessManualExitPerson');
  if (!select) return;
  const current = select.value || '';
  const people = _accessPeopleRows
    .filter(row => row.id && row.active !== false)
    .sort((a, b) => String(a.full_name || '').localeCompare(String(b.full_name || '')));
  select.innerHTML = ['<option value="">Pessoa para saida manual</option>']
    .concat(people.map(person => {
      const meta = [person.site, person.class_name || person.enrollment].filter(Boolean).join(' - ');
      const label = meta ? `${person.full_name} (${meta})` : person.full_name;
      return `<option value="${esc(person.id)}">${esc(label)}</option>`;
    }))
    .join('');
  if (current && people.some(person => person.id === current)) select.value = current;
}

async function ensureAccessReportBaseData(force = false) {
  const jobs = [];
  if (force || !_accessPeopleRows.length) {
    jobs.push(apiJson('/api/access-control/people', { forceRefresh: force, cacheTtl: 0 })
      .then(res => { _accessPeopleRows = res?.people || []; }));
  }
  if (force || !_accessDeviceRows.length) {
    jobs.push(loadAccessDevices(force).catch(() => null));
  }
  if (jobs.length) await Promise.all(jobs);
  populateAccessManualExitPeople();
  populateAccessReportSiteOptions();
}

async function loadAccessControlSummary(force = false) {
  const summaryRes = await apiJson('/api/access-control/summary', { forceRefresh: force, cacheTtl: 0 });
  renderAccessControlSummary(summaryRes?.summary || {}, force);
}

function renderAccessReportSummary(summary = {}) {
  return summary;
}

function accessReportDate(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  const parsed = new Date(raw.replace(' ', 'T'));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function accessReportTimeShort(value) {
  const raw = String(value || '').trim();
  if (!raw) return '-';
  const normalized = raw.replace('T', ' ');
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/);
  if (match) return `${match[3]}/${match[2]} ${match[4]}:${match[5]}`;
  const parts = normalized.split(' ');
  if (parts.length >= 2) return `${parts[0]} ${parts[1].slice(0, 5)}`;
  return raw;
}

function accessReportDuration(startValue, endValue) {
  const start = accessReportDate(startValue);
  const end = accessReportDate(endValue) || new Date();
  if (!start || end < start) return '-';
  const minutes = Math.max(0, Math.round((end - start) / 60000));
  if (minutes < 60) return `${minutes}min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}min` : `${hours}h`;
}

function accessReportNotificationKind(status) {
  const text = String(status || '').toLowerCase();
  if (!text || text === '-') return 'skipped';
  if (text.includes('failed') || text.includes('erro')) return 'failed';
  if (text.includes('sent') || text.includes('enviado')) return 'sent';
  if (text.includes('skipped') || text.includes('sem')) return 'skipped';
  return 'other';
}

function accessReportNotificationBadge(status) {
  const kind = accessReportNotificationKind(status);
  const label = status || (kind === 'sent' ? 'enviada' : kind === 'failed' ? 'falha' : 'sem envio');
  if (kind === 'sent') return `<span class="pill success" title="${esc(status || '')}">enviada</span>`;
  if (kind === 'failed') return `<span class="pill danger" title="${esc(status || '')}">falha</span>`;
  if (kind === 'skipped') return `<span class="pill neutral" title="${esc(status || '')}">sem envio</span>`;
  return `<span class="pill amber" title="${esc(status || '')}">${esc(label)}</span>`;
}

function accessReportPersonKey(event) {
  return String(event.person_id || event.person_document || event.person_enrollment || event.person_name || event.person_name_raw || '').trim();
}

function buildAccessReportPeople(events = []) {
  const map = new Map();
  events.forEach(event => {
    const person = event.person_name || event.person_name_raw || 'Pessoa nao identificada';
    const key = accessReportPersonKey(event) || person;
    if (!map.has(key)) {
      map.set(key, {
        key,
        person,
        document: event.person_document || event.person_enrollment || '',
        site: event.site || '',
        latest: event,
        firstEntry: '',
        lastEntry: '',
        lastExit: '',
        entries: 0,
        exits: 0,
        manualExits: 0,
        notification: event.notification_status || '',
      });
    }
    const item = map.get(key);
    const eventDate = accessReportDate(event.occurred_at);
    const latestDate = accessReportDate(item.latest?.occurred_at);
    if (!latestDate || (eventDate && eventDate > latestDate)) item.latest = event;
    if (event.site && !item.site) item.site = event.site;
    if (event.notification_status && !item.notification) item.notification = event.notification_status;
    if (event.event_type === 'entrada') {
      item.entries += 1;
      item.lastEntry = item.lastEntry || event.occurred_at || '';
      item.firstEntry = event.occurred_at || item.firstEntry;
    } else if (event.event_type === 'saida' || event.event_type === 'saida_manual') {
      item.exits += 1;
      if (event.event_type === 'saida_manual') item.manualExits += 1;
      item.lastExit = item.lastExit || event.occurred_at || '';
    }
  });
  return [...map.values()].sort((a, b) => String(b.latest?.occurred_at || '').localeCompare(String(a.latest?.occurred_at || '')));
}

function accessReportPeriodLabel() {
  const period = document.getElementById('accessReportPeriod')?.value || 'today';
  if (period === 'custom') {
    const start = document.getElementById('accessReportStart')?.value || '';
    const end = document.getElementById('accessReportEnd')?.value || '';
    return `Periodo: ${start ? start.replace('T', ' ') : 'inicio'} ate ${end ? end.replace('T', ' ') : 'fim'}`;
  }
  if (period === '7d') return 'Periodo: ultimos 7 dias';
  if (period === '30d') return 'Periodo: ultimos 30 dias';
  if (period === 'all') return 'Periodo: todo historico';
  return 'Periodo: hoje';
}

function renderAccessReportDocumentSummary(people = [], events = []) {
  const peopleWithEntries = people.filter(item => item.entries > 0).length;
  const peopleWithExits = people.filter(item => item.exits > 0).length;
  const presentPeople = people.filter(item => item.latest?.event_type === 'entrada').length;
  const site = document.getElementById('accessReportSite')?.value || 'Todos';
  setText('accessReportEntrantPeople', peopleWithEntries);
  setText('accessReportExitPeople', peopleWithExits);
  setText('accessReportPresentPeople', presentPeople);
  setText('accessReportPeopleWithEntries', peopleWithEntries);
  setText('accessReportPeopleWithExits', peopleWithExits);
  setText('accessReportPeoplePresent', presentPeople);
  setText('accessReportPrintPeriod', accessReportPeriodLabel());
  setText('accessReportPrintSite', site);
}

function renderAccessReportPeople(events = []) {
  const body = document.getElementById('accessReportPeopleBody');
  const people = buildAccessReportPeople(events);
  setText('accessReportPeopleCount', `${people.length} pessoa${people.length === 1 ? '' : 's'}`);
  renderAccessReportDocumentSummary(people, events);
  if (!body) return people;
  if (!people.length) {
    body.innerHTML = `
      <div class="access-report-empty-state">
        <i data-lucide="users"></i>
        <strong>Nenhuma movimentacao encontrada</strong>
        <span>Ajuste os filtros para visualizar entradas e saidas.</span>
      </div>
    `;
    lucide.createIcons();
    return people;
  }
  body.innerHTML = people.map(item => {
    const latestType = item.latest?.event_type || '';
    const statusBadge = latestType === 'entrada'
      ? '<span class="pill success">presente</span>'
      : latestType === 'saida_manual'
        ? '<span class="pill amber">saida manual</span>'
        : '<span class="pill neutral">saiu</span>';
    const entry = accessReportTimeShort(item.lastEntry || item.firstEntry);
    const exit = accessReportTimeShort(item.lastExit);
    const identity = [item.document, item.site].filter(Boolean).join(' - ') || 'sem documento';
    return `
      <article class="access-report-person-card">
        <div class="access-report-person-main">
          <strong title="${esc(item.person)}">${esc(item.person)}</strong>
          <span>${esc(identity)}</span>
        </div>
        <div class="access-report-person-status">${statusBadge}</div>
        <div class="access-report-person-metrics">
          <span><small>Entrada</small><b class="mono">${esc(entry)}</b></span>
          <span><small>Saida</small><b class="mono">${esc(exit)}</b></span>
          <span><small>Entradas</small><b>${esc(item.entries)}</b></span>
          <span><small>Saidas</small><b>${esc(item.exits)}</b></span>
        </div>
        <div class="access-report-person-notification">${accessReportNotificationBadge(item.notification)}</div>
      </article>
    `;
  }).join('');
  return people;
}

function toggleAccessReportEvents() {
  const wrap = document.getElementById('accessReportEventsWrap');
  if (!wrap) return;
  wrap.hidden = !wrap.hidden;
}

function accessReportCsvCell(value) {
  return `"${String(value ?? '').replaceAll('"', '""')}"`;
}

function exportAccessReportCsv() {
  const people = buildAccessReportPeople(_accessReportRows || []);
  if (!people.length) {
    showToast('Nao ha movimentacao para exportar.', true);
    return;
  }
  const header = ['Pessoa', 'Documento/matricula', 'Status', 'Entrada', 'Saida', 'Entradas', 'Saidas', 'Site'];
  const csvRows = [header].concat(people.map(item => [
    item.person || '',
    item.document || '',
    item.latest?.event_type === 'entrada' ? 'presente' : item.latest?.event_type === 'saida_manual' ? 'saida manual' : 'saiu',
    accessReportTimeShort(item.lastEntry || item.firstEntry),
    accessReportTimeShort(item.lastExit),
    item.entries || 0,
    item.exits || 0,
    item.site || '',
  ]));
  const content = csvRows.map(row => row.map(accessReportCsvCell).join(';')).join('\r\n');
  const blob = new Blob([`\ufeff${content}`], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `sightops-entradas-saidas-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function printAccessReportPdf() {
  document.body.classList.add('access-report-printing');
  const cleanup = () => document.body.classList.remove('access-report-printing');
  window.addEventListener('afterprint', cleanup, { once: true });
  setTimeout(() => {
    window.print();
    setTimeout(cleanup, 1500);
  }, 50);
}

function renderAccessReportIntelligence(summary = {}, events = []) {
  const people = renderAccessReportPeople(events);
  const insidePeople = people.filter(item => item.latest?.event_type === 'entrada');
  const failedEvents = events.filter(event => accessReportNotificationKind(event.notification_status) === 'failed');
  const sentEvents = events.filter(event => accessReportNotificationKind(event.notification_status) === 'sent');
  const skippedEvents = events.filter(event => accessReportNotificationKind(event.notification_status) === 'skipped');
  const manualEvents = events.filter(event => event.event_type === 'saida_manual');
  const pending = insidePeople.length + failedEvents.length + manualEvents.length;
  setText('accessReportPending', pending);
  setText('accessReportNotificationsOk', sentEvents.length);
  setText('accessReportNotificationsFail', failedEvents.length);
  setText('accessReportNotificationsSkipped', skippedEvents.length);
  setText('accessReportAttentionCount', `${pending} item${pending === 1 ? '' : 's'}`);

  const list = document.getElementById('accessReportAttentionBody');
  if (!list) return;
  const items = [];
  insidePeople.slice(0, 4).forEach(item => {
    items.push({
      icon: 'user-check',
      tone: 'green',
      title: `${item.person} esta presente`,
      meta: `${item.site || 'Sem site'} · entrada ${accessReportTimeShort(item.lastEntry || item.firstEntry)} · ${accessReportDuration(item.lastEntry || item.firstEntry, '')}`,
    });
  });
  failedEvents.slice(0, 3).forEach(event => {
    items.push({
      icon: 'bell-off',
      tone: 'red',
      title: `Falha de notificacao: ${event.person_name || event.person_name_raw || 'Pessoa'}`,
      meta: `${event.site || 'Sem site'} · ${accessReportTimeShort(event.occurred_at)} · ${event.notification_status || 'falha'}`,
    });
  });
  manualEvents.slice(0, 2).forEach(event => {
    items.push({
      icon: 'hand',
      tone: 'amber',
      title: `Saida manual: ${event.person_name || event.person_name_raw || 'Pessoa'}`,
      meta: `${event.site || 'Sem site'} · ${accessReportTimeShort(event.occurred_at)} · operador interno`,
    });
  });
  if (!items.length) {
    list.innerHTML = `
      <div class="access-report-empty-state">
        <i data-lucide="check-circle-2"></i>
        <strong>Nenhuma pendencia no periodo</strong>
        <span>Os eventos carregados nao indicam falha de notificacao, saida manual ou pessoa ainda presente.</span>
      </div>
    `;
    lucide.createIcons();
    return;
  }
  list.innerHTML = items.slice(0, 7).map(item => `
    <article class="access-report-attention-item ${item.tone}">
      <span><i data-lucide="${item.icon}"></i></span>
      <div>
        <strong>${esc(item.title)}</strong>
        <small>${esc(item.meta)}</small>
      </div>
    </article>
  `).join('');
  lucide.createIcons();
}

function renderAccessReportEvents(events = []) {
  const body = document.getElementById('accessReportBody');
  if (!body) return;
  _accessReportRows = events;
  setText('accessReportCount', `${events.length} evento${events.length === 1 ? '' : 's'}`);
  populateAccessReportSiteOptions();
  renderAccessReportIntelligence({}, events);
  if (!events.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="7">Nenhum evento encontrado.</td></tr>';
    scheduleResponsiveHydration(body);
    return;
  }
  body.innerHTML = events.map(event => {
    const person = event.person_name || event.person_name_raw || '-';
    const device = event.device_name || event.device_id || '-';
    const source = event.source === 'manual' ? 'Manual' : 'Dispositivo';
    return `
      <tr>
        <td class="mono">${esc(event.occurred_at || '-')}</td>
        <td><strong title="${esc(person)}">${esc(person)}</strong><span class="muted-block">${esc(event.person_document || event.person_enrollment || '')}</span></td>
        <td>${accessEventTypeBadge(event.event_type)}</td>
        <td title="${esc(event.site || '')}">${esc(event.site || '-')}</td>
        <td title="${esc(device)}">${esc(device)}</td>
        <td>${esc(source)}</td>
        <td>${accessReportNotificationBadge(event.notification_status)}</td>
      </tr>
    `;
  }).join('');
  scheduleResponsiveHydration(body);
  lucide.createIcons();
}

async function loadAccessReports(force = false, options = {}) {
  const silent = Boolean(options?.silent);
  const btn = document.getElementById('btnAccessReportRefresh');
  const oldHtml = btn?.innerHTML;
  if (btn && !silent) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Atualizando';
    lucide.createIcons();
  }
  try {
    await ensureAccessReportBaseData(force);
    const query = accessReportQuery();
    const [summary, events] = await Promise.all([
      apiJson(`/api/access-control/reports/summary?${query.toString()}`, { forceRefresh: force, cacheTtl: 0 }),
      apiJson(`/api/access-control/reports/events?${query.toString()}`, { forceRefresh: force, cacheTtl: 0 }),
    ]);
    const reportSummary = summary?.summary || summary || {};
    const reportEvents = events?.events || [];
    renderAccessReportSummary(reportSummary);
    renderAccessReportEvents(reportEvents);
    renderAccessReportIntelligence(reportSummary, reportEvents);
  } catch (err) {
    if (!silent) showToast(err?.message || 'Nao foi possivel carregar relatorios.', true);
    else console.warn('SightOps Access Control reports refresh failed', err);
  } finally {
    if (btn && !silent) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="refresh-cw"></i> Atualizar';
      lucide.createIcons();
    }
  }
}

async function recordAccessManualExit() {
  const btn = document.getElementById('btnAccessManualExit');
  const personId = document.getElementById('accessManualExitPerson')?.value || '';
  const person = _accessPeopleRows.find(row => row.id === personId);
  if (!person) {
    showToast('Escolha uma pessoa para registrar saida manual.', true);
    return;
  }
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Registrando';
    lucide.createIcons();
  }
  try {
    const payload = {
      person_id: person.id,
      site: person.site || document.getElementById('accessReportSite')?.value || '',
      reason: document.getElementById('accessManualExitReason')?.value?.trim() || '',
    };
    const res = await api('/api/access-control/reports/manual-exit', { method: 'POST', body: JSON.stringify(payload) });
    await jsonOrReadableError(res, 'Nao foi possivel registrar saida manual.');
    if (document.getElementById('accessManualExitReason')) document.getElementById('accessManualExitReason').value = '';
    showToast('Saida manual registrada.');
    await loadAccessReports(true);
    await loadAccessControl(true);
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel registrar saida manual.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="log-out"></i> Registrar saida manual';
      lucide.createIcons();
    }
  }
}

async function focusAccessPersonFromDrawer(personId) {
  const search = document.getElementById('accessPeopleSearch');
  const status = document.getElementById('accessPeopleStatus');
  const type = document.getElementById('accessPeopleType');
  const site = document.getElementById('accessPeopleSite');
  if (search) search.value = '';
  if (status) status.value = '';
  if (type) type.value = 'student';
  if (site) site.value = '';
  _accessPeopleSelected = new Set(personId ? [personId] : []);
  showAccessControlTab('people');
  await loadAccessControl(true);
  setTimeout(() => {
    const safeId = window.CSS?.escape ? CSS.escape(personId || '') : String(personId || '').replace(/"/g, '\\"');
    const row = document.querySelector(`[data-access-person-row="${safeId}"]`);
    row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, 60);
}

async function focusAccessDeviceFromDrawer(deviceId) {
  _accessDeviceSelectedId = deviceId || '';
  showAccessControlTab('devices');
  await loadAccessDevices(true);
  setTimeout(() => {
    const safeId = window.CSS?.escape ? CSS.escape(_accessDeviceSelectedId) : _accessDeviceSelectedId.replace(/"/g, '\\"');
    const row = document.querySelector(`[data-access-device-row="${safeId}"]`);
    row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, 60);
}

async function loadAccessDevices(force = false) {
  try {
    const connectorsPromise = loadAccessConnectors(force).catch(() => []);
    const res = await apiJson('/api/access-control/devices', { forceRefresh: force, cacheTtl: 0 });
    await connectorsPromise;
    _accessDeviceRows = res?.devices || [];
    if (_accessDeviceSelectedId && !_accessDeviceRows.some(row => row.id === _accessDeviceSelectedId)) {
      _accessDeviceSelectedId = '';
    }
    renderAccessDevices(_accessDeviceRows);
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel carregar dispositivos.', true);
  }
}

function accessConnectorKey(connector) {
  return String(connector?.id || connector?.connector_id || '').trim();
}

function accessConnectorLabel(connector) {
  if (!connector) return 'Local';
  return connector.name || connector.site || accessConnectorKey(connector) || 'Conector';
}

function accessConnectorSite(connector) {
  return String(connector?.site || connector?.scope?.site || '').trim();
}

function accessConnectorStatus(connector) {
  const raw = String(connector?.status || connector?.heartbeat_status || '').toLowerCase();
  if (raw === 'online' || connector?.online === true) return 'online';
  if (raw === 'offline' || connector?.online === false) return 'offline';
  return raw || '';
}

function accessDeviceConnectorLabel(device) {
  const connectorId = String(device?.connector_id || '').trim();
  if (!connectorId) return 'Local';
  const connector = _accessConnectorRows.find(row => accessConnectorKey(row) === connectorId);
  return connector ? accessConnectorLabel(connector) : connectorId;
}

async function loadAccessConnectors(force = false) {
  if (_accessConnectorsLoaded && !force) return _accessConnectorRows;
  const data = await apiJson('/api/connectors', { forceRefresh: force, cacheTtl: 0 });
  _accessConnectorRows = Array.isArray(data?.connectors) ? data.connectors : [];
  _accessConnectorsLoaded = true;
  return _accessConnectorRows;
}

function populateAccessDeviceConnectorOptions(selectedId = '') {
  const select = document.getElementById('accessDeviceConnector');
  if (!select) return;
  const selected = String(selectedId || '').trim();
  const options = ['<option value="">Local / VPN do servidor</option>'];
  _accessConnectorRows
    .filter(connector => accessConnectorKey(connector))
    .forEach(connector => {
      const key = accessConnectorKey(connector);
      const site = accessConnectorSite(connector);
      const status = accessConnectorStatus(connector);
      const meta = [site, status].filter(Boolean).join(' - ');
      const label = meta ? `${accessConnectorLabel(connector)} (${meta})` : accessConnectorLabel(connector);
      options.push(`<option value="${esc(key)}">${esc(label)}</option>`);
    });
  if (selected && !_accessConnectorRows.some(connector => accessConnectorKey(connector) === selected)) {
    options.push(`<option value="${esc(selected)}">${esc(selected)} (nao encontrado)</option>`);
  }
  select.innerHTML = options.join('');
  select.value = selected;
}

function accessDeviceStatusBadge(status) {
  const key = String(status || '').toLowerCase();
  if (key === 'online') return '<span class="pill success">online</span>';
  if (key === 'offline') return '<span class="pill neutral">offline</span>';
  return `<span class="pill neutral">${esc(status || 'desconhecido')}</span>`;
}

function renderAccessDevices(rows) {
  const body = document.getElementById('accessDevicesBody');
  if (!body) return;
  setText('accessDevicesCount', `${rows.length} dispositivo${rows.length === 1 ? '' : 's'}`);
  if (!rows.length) {
    _accessDeviceSelectedId = '';
    body.innerHTML = '<tr class="empty-row"><td colspan="8">Nenhum dispositivo cadastrado.</td></tr>';
    syncAccessDevicesFooterActions();
    scheduleResponsiveHydration(body);
    return;
  }
  body.innerHTML = rows.map(device => `
    <tr class="${device.id === _accessDeviceSelectedId ? 'selected' : ''}" data-access-device-row="${esc(device.id)}">
      <td class="access-checkbox-cell"><input type="checkbox" data-access-device-check="${esc(device.id)}" aria-label="Selecionar ${esc(device.name)}" ${device.id === _accessDeviceSelectedId ? 'checked' : ''}></td>
      <td><strong>${esc(device.name)}</strong></td>
      <td>${esc(device.site || '-')}</td>
      <td title="${esc(accessDeviceConnectorLabel(device))}">${esc(accessDeviceConnectorLabel(device))}</td>
      <td>${esc(device.host)}</td>
      <td>${esc(device.model || '-')}</td>
      <td>${esc(accessDirectionLabel(device.access_direction))}</td>
      <td>${accessDeviceStatusBadge(device.status)}</td>
    </tr>
  `).join('');
  syncAccessDevicesFooterActions();
  scheduleResponsiveHydration(body);
  lucide.createIcons();
}

function openAccessDeviceModal(device = null) {
  const item = device || {};
  setText('accessDeviceModalTitle', item.id ? 'Editar dispositivo' : 'Novo dispositivo');
  document.getElementById('accessDeviceId').value = item.id || '';
  // Preserva vendor/model do registro existente: o backend faz UPDATE completo
  // (ON CONFLICT DO UPDATE SET vendor=excluded.vendor, model=excluded.model), entao
  // se nao reenviarmos esses campos aqui, editar um dispositivo apaga o model salvo
  // e forca o vendor de volta para o default.
  document.getElementById('accessDeviceVendor').value = item.vendor || 'intelbras';
  document.getElementById('accessDeviceModel').value = item.model || '';
  document.getElementById('accessDeviceName').value = item.name || '';
  document.getElementById('accessDeviceSite').value = item.site || '';
  populateAccessDeviceConnectorOptions(item.connector_id || '');
  loadAccessConnectors(false)
    .then(() => populateAccessDeviceConnectorOptions(item.connector_id || ''))
    .catch(() => populateAccessDeviceConnectorOptions(item.connector_id || ''));
  document.getElementById('accessDeviceHost').value = item.host || '';
  document.getElementById('accessDeviceUsername').value = item.username || 'admin';
  // O backend nunca devolve a senha (get_device_with_password e uso interno) --
  // este campo fica sempre em branco, mesmo editando um dispositivo existente.
  document.getElementById('accessDevicePassword').value = '';
  document.getElementById('accessDeviceDirection').value = item.access_direction || 'entrada';
  document.getElementById('accessDeviceActive').checked = item.active !== false;
  document.getElementById('modalAccessDevice')?.classList.remove('hidden');
  setTimeout(() => document.getElementById('accessDeviceName')?.focus(), 50);
  lucide.createIcons();
}

function closeAccessDeviceModal() {
  document.getElementById('modalAccessDevice')?.classList.add('hidden');
}

async function saveAccessDeviceFromForm(event) {
  event.preventDefault();
  const btn = document.getElementById('btnAccessDeviceSave');
  const oldHtml = btn?.innerHTML;
  const payload = {
    id: document.getElementById('accessDeviceId').value.trim(),
    name: document.getElementById('accessDeviceName').value.trim(),
    site: document.getElementById('accessDeviceSite').value.trim(),
    connector_id: document.getElementById('accessDeviceConnector')?.value || '',
    vendor: document.getElementById('accessDeviceVendor').value.trim(),
    model: document.getElementById('accessDeviceModel').value.trim(),
    host: document.getElementById('accessDeviceHost').value.trim(),
    username: document.getElementById('accessDeviceUsername').value.trim(),
    password: document.getElementById('accessDevicePassword').value,
    access_direction: document.getElementById('accessDeviceDirection')?.value || 'entrada',
    active: document.getElementById('accessDeviceActive').checked,
  };
  if (!payload.name || !payload.host) {
    showToast('Informe nome e host do dispositivo.', true);
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Salvando';
    lucide.createIcons();
  }
  try {
    const res = await api('/api/access-control/devices', { method: 'POST', body: JSON.stringify(payload) });
    await jsonOrReadableError(res, 'Nao foi possivel salvar o dispositivo.');
    closeAccessDeviceModal();
    await loadAccessDevices(true);
    showToast('Dispositivo salvo.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel salvar o dispositivo.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="save"></i> Salvar dispositivo';
      lucide.createIcons();
    }
  }
}

async function handleAccessDeviceAction(event) {
  const check = event.target.closest?.('[data-access-device-check]');
  if (check) {
    _accessDeviceSelectedId = check.checked ? (check.dataset.accessDeviceCheck || '') : '';
    renderAccessDevices(_accessDeviceRows);
    return;
  }
  const row = event.target.closest?.('[data-access-device-row]');
  if (!row) return;
  _accessDeviceSelectedId = row.dataset.accessDeviceRow || '';
  renderAccessDevices(_accessDeviceRows);
}

async function refreshAccessDevicesFromButton() {
  const btn = document.getElementById('btnAccessDevicesFooterRefresh');
  const hint = document.getElementById('accessDevicesFooterHint');
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i>';
    lucide.createIcons();
  }
  if (hint) hint.textContent = 'Atualizando lista de controladoras...';
  try {
    await loadAccessDevices(true);
    showToast('Lista de dispositivos atualizada.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel atualizar os dispositivos.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="refresh-cw"></i>';
      lucide.createIcons();
    }
    syncAccessDevicesFooterActions();
  }
}

function toggleAccessDeviceMasterCheck(event) {
  const firstDevice = _accessDeviceRows[0];
  _accessDeviceSelectedId = event.target.checked && firstDevice ? firstDevice.id : '';
  renderAccessDevices(_accessDeviceRows);
}

function selectedAccessDevice() {
  return _accessDeviceRows.find(row => row.id === _accessDeviceSelectedId) || null;
}

function syncAccessDevicesFooterActions() {
  const device = selectedAccessDevice();
  const hasSelection = Boolean(device);
  const hint = document.getElementById('accessDevicesFooterHint');
  const master = document.getElementById('accessDevicesSelectAll');
  if (hint) {
    hint.textContent = hasSelection
      ? `Selecionado: ${device.name || device.host || 'dispositivo'}`
      : 'Selecione uma controladora para testar conexao, abrir porta, editar ou excluir.';
  }
  if (master) {
    master.checked = hasSelection;
    master.disabled = !_accessDeviceRows.length;
  }
  ['btnAccessDevicesFooterTest', 'btnAccessDevicesFooterOpenDoor', 'btnAccessDevicesFooterEdit', 'btnAccessDevicesFooterDelete'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !hasSelection;
  });
}

async function testSelectedAccessDevice() {
  const device = selectedAccessDevice();
  const btn = document.getElementById('btnAccessDevicesFooterTest');
  if (!device) return;
  const hint = document.getElementById('accessDevicesFooterHint');
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i>';
    lucide.createIcons();
  }
  if (hint) {
    const via = device.connector_id ? ` via conector ${accessDeviceConnectorLabel(device)}` : '';
    hint.textContent = `Testando conexao com ${device.name || device.host || 'dispositivo'}${via}...`;
  }
  try {
    const res = await api(`/api/access-control/devices/${encodeURIComponent(device.id)}/test`, { method: 'POST', skipLogout: true });
    const data = await jsonOrReadableError(res, 'Nao foi possivel testar o dispositivo.');
    await loadAccessDevices(true);
    const model = data?.device?.model || data?.info?.updateSerial || data?.info?.deviceType || '';
    showToast(model ? `Conexao OK. Modelo detectado: ${model}.` : 'Conexao OK com a controladora.');
  } catch (err) {
    await loadAccessDevices(true);
    showToast(err?.message || 'Nao foi possivel testar o dispositivo.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="plug-zap"></i>';
      lucide.createIcons();
    }
    syncAccessDevicesFooterActions();
  }
}

async function openSelectedAccessDeviceDoor() {
  const device = selectedAccessDevice();
  const btn = document.getElementById('btnAccessDevicesFooterOpenDoor');
  if (!device) return;
  const hint = document.getElementById('accessDevicesFooterHint');
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i>';
    lucide.createIcons();
  }
  if (hint) {
    const via = device.connector_id ? ` via conector ${accessDeviceConnectorLabel(device)}` : '';
    hint.textContent = `Enviando comando de abertura para ${device.name || device.host || 'dispositivo'}${via}...`;
  }
  try {
    const res = await api(`/api/access-control/devices/${encodeURIComponent(device.id)}/open-door`, { method: 'POST' });
    await jsonOrReadableError(res, 'Nao foi possivel abrir a porta.');
    showToast('Porta liberada.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel abrir a porta.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="door-open"></i>';
      lucide.createIcons();
    }
    syncAccessDevicesFooterActions();
  }
}

function editSelectedAccessDevice() {
  const device = selectedAccessDevice();
  if (!device) {
    showToast('Selecione um dispositivo para editar.', true);
    return;
  }
  openAccessDeviceModal(device);
}

async function deleteSelectedAccessDevice() {
  const device = selectedAccessDevice();
  if (!device) {
    showToast('Selecione um dispositivo para excluir.', true);
    return;
  }
  if (_accessDeviceDeleteConfirmId !== device.id) {
    _accessDeviceDeleteConfirmId = device.id;
    showToast(`Clique novamente para excluir ${device.name}.`);
    setTimeout(() => {
      if (_accessDeviceDeleteConfirmId === device.id) _accessDeviceDeleteConfirmId = '';
    }, 4500);
    return;
  }
  try {
    const res = await api(`/api/access-control/devices/${encodeURIComponent(device.id)}`, { method: 'DELETE' });
    await jsonOrReadableError(res, 'Nao foi possivel excluir o dispositivo.');
    _accessDeviceDeleteConfirmId = '';
    _accessDeviceSelectedId = '';
    await loadAccessDevices(true);
    showToast('Dispositivo excluido.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel excluir o dispositivo.', true);
  }
}

async function deletePersonWithConfirm(person) {
  const ok = await showConfirm({
    title: 'Excluir pessoa',
    msg: `Excluir ${person.full_name} do controle de acesso? Isso remove o cadastro e o historico de sincronizacao.`,
    label: 'Excluir',
  });
  if (!ok) return;
  try {
    const res = await api(`/api/access-control/people/${encodeURIComponent(person.id)}`, { method: 'DELETE' });
    await jsonOrReadableError(res, 'Nao foi possivel excluir a pessoa.');
    await loadAccessControl(true);
    showToast('Pessoa excluida.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel excluir a pessoa.', true);
  }
}

function handleAccessPeopleBodyClick(event) {
  const check = event.target.closest?.('[data-person-check]');
  if (check) {
    if (check.checked) _accessPeopleSelected.add(check.dataset.personCheck);
    else _accessPeopleSelected.delete(check.dataset.personCheck);
    syncAccessPeopleSelectAll();
    syncAccessPeopleFooterActions();
  }
}

function selectedAccessPeople() {
  return _accessPeopleRows.filter(row => _accessPeopleSelected.has(row.id));
}

function syncAccessPeopleFooterActions() {
  const selected = selectedAccessPeople();
  const editBtn = document.getElementById('btnAccessPeopleFooterEdit');
  const syncBtn = document.getElementById('btnAccessPeopleFooterSync');
  const deleteSelectedBtn = document.getElementById('btnAccessPeopleFooterDeleteSelected');
  const deleteAllBtn = document.getElementById('btnAccessPeopleFooterDeleteAll');
  if (editBtn) editBtn.disabled = selected.length !== 1;
  if (syncBtn) syncBtn.disabled = selected.length < 1;
  if (deleteSelectedBtn) deleteSelectedBtn.disabled = selected.length < 1;
  if (deleteAllBtn) deleteAllBtn.disabled = _accessPeopleRows.length < 1;
}

function editSelectedAccessPerson() {
  const selected = selectedAccessPeople();
  if (selected.length !== 1) {
    showToast('Selecione exatamente uma pessoa para editar.', true);
    return;
  }
  openAccessPersonModal(selected[0]);
}

async function deleteAccessPeopleBatch(people, title, msg) {
  if (!people.length) {
    showToast('Nenhuma pessoa selecionada.', true);
    return;
  }
  const ok = await showConfirm({ title, msg, label: 'Excluir' });
  if (!ok) return;
  try {
    for (const person of people) {
      const res = await api(`/api/access-control/people/${encodeURIComponent(person.id)}`, { method: 'DELETE' });
      await jsonOrReadableError(res, `Nao foi possivel excluir ${person.full_name || 'a pessoa'}.`);
    }
    _accessPeopleSelected.clear();
    await loadAccessControl(true);
    loadAccessPeopleSiteOptions();
    showToast(`${people.length} pessoa${people.length === 1 ? '' : 's'} excluida${people.length === 1 ? '' : 's'}.`);
  } catch (err) {
    await loadAccessControl(true);
    showToast(err?.message || 'Nao foi possivel excluir as pessoas.', true);
  }
}

async function deleteSelectedAccessPeople() {
  const people = selectedAccessPeople();
  await deleteAccessPeopleBatch(
    people,
    people.length === 1 ? 'Excluir pessoa selecionada' : 'Excluir pessoas selecionadas',
    `Excluir ${people.length} pessoa${people.length === 1 ? '' : 's'} selecionada${people.length === 1 ? '' : 's'}? Isso remove cadastro e historico de sincronizacao.`,
  );
}

async function deleteAllVisibleAccessPeople() {
  const site = document.getElementById('accessPeopleSite')?.value || '';
  const scope = site ? ` do site ${site}` : ' carregadas na tabela';
  await deleteAccessPeopleBatch(
    [..._accessPeopleRows],
    site ? 'Excluir pessoas do site' : 'Excluir todas as pessoas',
    `Excluir ${_accessPeopleRows.length} pessoa${_accessPeopleRows.length === 1 ? '' : 's'}${scope}? Isso remove cadastro e historico de sincronizacao.`,
  );
}

async function syncSelectedAccessPeople() {
  const people = selectedAccessPeople();
  if (!people.length) {
    showToast('Selecione pelo menos uma pessoa para sincronizar.', true);
    return;
  }
  const btn = document.getElementById('btnAccessPeopleFooterSync');
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-2"></i>';
    lucide.createIcons();
  }
  let ok = 0;
  let failed = 0;
  try {
    for (const person of people) {
      showToast(`Sincronizando ${ok + failed + 1}/${people.length}: ${person.full_name || 'pessoa'}...`);
      const res = await api(`/api/access-control/people/${encodeURIComponent(person.id)}/sync`, { method: 'POST' });
      try {
        await jsonOrReadableError(res, `Nao foi possivel sincronizar ${person.full_name || 'a pessoa'}.`);
        ok += 1;
      } catch (err) {
        failed += 1;
        console.warn('Falha ao sincronizar pessoa', person.id, err);
      }
    }
    await loadAccessControl(true);
    const msg = failed
      ? `${ok} sincronizada(s), ${failed} com erro.`
      : `${ok} pessoa${ok === 1 ? '' : 's'} sincronizada${ok === 1 ? '' : 's'}.`;
    showToast(msg, failed > 0);
  } finally {
    if (btn) {
      btn.innerHTML = oldHtml || '<i data-lucide="upload-cloud"></i>';
      lucide.createIcons();
    }
    syncAccessPeopleFooterActions();
  }
}

function syncAccessPeopleSelectAll() {
  const master = document.getElementById('accessPeopleSelectAll');
  if (!master) return;
  const total = _accessPeopleRows.length;
  const selected = _accessPeopleSelected.size;
  master.checked = total > 0 && selected === total;
  master.indeterminate = selected > 0 && selected < total;
  syncAccessPeopleFooterActions();
}

function toggleAccessPeopleSelectAll(event) {
  const checked = event.target.checked;
  _accessPeopleSelected = new Set(checked ? _accessPeopleRows.map(p => p.id) : []);
  document.querySelectorAll('#accessPeopleBody [data-person-check]').forEach(el => { el.checked = checked; });
  syncAccessPeopleSelectAll();
}

// Menu flutuante de acoes por linha ("3 pontinhos") -- anexado no <body> (nao
// dentro da celula) e posicionado via getBoundingClientRect, pra nao ficar
// cortado pelo overflow:hidden do .table-wrap quando a linha esta perto da
// borda da tabela. Generico o bastante pra reaproveitar em outras tabelas
// deste arquivo (dispositivos, grupos, regras) se precisar.
let _openRowActionsMenu = null;
let _openRowActionsAnchor = null;
function closeRowActionsMenu() {
  if (!_openRowActionsMenu) return;
  _openRowActionsMenu.remove();
  _openRowActionsMenu = null;
  _openRowActionsAnchor = null;
  document.removeEventListener('click', _rowActionsOutsideHandler, true);
  document.removeEventListener('keydown', _rowActionsEscHandler, true);
  window.removeEventListener('scroll', closeRowActionsMenu, true);
  window.removeEventListener('resize', closeRowActionsMenu, true);
}
function _rowActionsOutsideHandler(e) {
  if (_openRowActionsMenu && !_openRowActionsMenu.contains(e.target) && e.target !== _openRowActionsAnchor) closeRowActionsMenu();
}
function _rowActionsEscHandler(e) {
  if (e.key === 'Escape') closeRowActionsMenu();
}
function openRowActionsMenu(anchorBtn, items) {
  const wasOpenForSameAnchor = _openRowActionsAnchor === anchorBtn;
  closeRowActionsMenu();
  if (wasOpenForSameAnchor) return;
  const panel = document.createElement('div');
  panel.className = 'row-actions-panel';
  panel.setAttribute('role', 'menu');
  panel.innerHTML = items.map((it, idx) => `
    <button type="button" class="${it.danger ? 'danger' : ''}" data-idx="${idx}" role="menuitem">
      <i data-lucide="${it.icon}"></i> ${esc(it.label)}
    </button>
  `).join('');
  document.body.appendChild(panel);
  panel.querySelectorAll('button').forEach((btn, idx) => {
    btn.addEventListener('click', () => { closeRowActionsMenu(); items[idx].onClick(); });
  });
  lucide.createIcons();

  const rect = anchorBtn.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const spaceBelow = window.innerHeight - rect.bottom;
  const top = spaceBelow >= panelRect.height + 8 ? rect.bottom + 4 : rect.top - panelRect.height - 4;
  let left = rect.right - panelRect.width;
  left = Math.max(8, Math.min(left, window.innerWidth - panelRect.width - 8));
  panel.style.top = `${Math.max(8, top)}px`;
  panel.style.left = `${left}px`;

  _openRowActionsMenu = panel;
  _openRowActionsAnchor = anchorBtn;
  setTimeout(() => document.addEventListener('click', _rowActionsOutsideHandler, true), 0);
  document.addEventListener('keydown', _rowActionsEscHandler, true);
  window.addEventListener('scroll', closeRowActionsMenu, true);
  window.addEventListener('resize', closeRowActionsMenu, true);
}

let _accessGroupRows = [];
let _accessDoorGroupRows = [];
let _accessRuleRows = [];
let _accessGroupSelectedId = '';
let _accessDoorGroupSelectedId = '';
let _accessRuleSelectedId = '';
let _accessRuleDeleteConfirmId = '';
let _accessGroupDeleteConfirmId = '';
let _accessDoorGroupDeleteConfirmId = '';
// true quando o checklist de pessoas do modal de grupo nao carregou -- salvar
// nesse estado enviaria member_ids incompleto e o backend apagaria os membros
// que faltaram (set_group_members faz DELETE + reinsert).
let _accessGroupPeopleLoadFailed = false;

function bindAccessGroups() {
  document.getElementById('btnAccessGroupNew')?.addEventListener('click', () => openAccessGroupModal());
  document.getElementById('btnAccessDoorGroupNew')?.addEventListener('click', () => openAccessDoorGroupModal());
  document.getElementById('btnAccessRuleNew')?.addEventListener('click', () => openAccessRuleModal());
  document.getElementById('accessGroupsBody')?.addEventListener('click', handleAccessGroupAction);
  document.getElementById('accessDoorGroupsBody')?.addEventListener('click', handleAccessDoorGroupAction);
  document.getElementById('accessGroupsSelectAll')?.addEventListener('change', toggleAccessGroupMasterCheck);
  document.getElementById('accessDoorGroupsSelectAll')?.addEventListener('change', toggleAccessDoorGroupMasterCheck);
  document.getElementById('btnAccessGroupsFooterRefresh')?.addEventListener('click', () => loadAccessGroups(true));
  document.getElementById('btnAccessDoorGroupsFooterRefresh')?.addEventListener('click', () => loadAccessGroups(true));
  document.getElementById('btnAccessGroupsFooterEdit')?.addEventListener('click', editSelectedAccessGroup);
  document.getElementById('btnAccessDoorGroupsFooterEdit')?.addEventListener('click', editSelectedAccessDoorGroup);
  document.getElementById('btnAccessGroupsFooterDelete')?.addEventListener('click', deleteSelectedAccessGroup);
  document.getElementById('btnAccessDoorGroupsFooterDelete')?.addEventListener('click', deleteSelectedAccessDoorGroup);
  document.getElementById('accessRulesBody')?.addEventListener('click', handleAccessRuleAction);
  document.getElementById('accessRulesSelectAll')?.addEventListener('change', toggleAccessRuleMasterCheck);
  document.getElementById('btnAccessRulesFooterRefresh')?.addEventListener('click', () => loadAccessRules(true));
  document.getElementById('btnAccessRulesFooterReuse')?.addEventListener('click', reuseSelectedAccessRule);
  document.getElementById('btnAccessRulesFooterEdit')?.addEventListener('click', editSelectedAccessRule);
  document.getElementById('btnAccessRulesFooterDelete')?.addEventListener('click', deleteSelectedAccessRule);
  document.getElementById('btnAccessGroupClose')?.addEventListener('click', closeAccessGroupModal);
  document.getElementById('btnAccessGroupCancel')?.addEventListener('click', closeAccessGroupModal);
  document.getElementById('accessGroupForm')?.addEventListener('submit', saveAccessGroupFromForm);
  document.getElementById('btnAccessDoorGroupClose')?.addEventListener('click', closeAccessDoorGroupModal);
  document.getElementById('btnAccessDoorGroupCancel')?.addEventListener('click', closeAccessDoorGroupModal);
  document.getElementById('accessDoorGroupForm')?.addEventListener('submit', saveAccessDoorGroupFromForm);
  document.getElementById('btnAccessRuleClose')?.addEventListener('click', closeAccessRuleModal);
  document.getElementById('btnAccessRuleCancel')?.addEventListener('click', closeAccessRuleModal);
  document.getElementById('accessRuleForm')?.addEventListener('submit', saveAccessRuleFromForm);
  bindAccessRuleModal();
}

async function loadAccessGroups(force = false) {
  try {
    // Busca dispositivos junto com os grupos -- sem isso, _accessDeviceRows so
    // seria preenchido ao visitar a aba Dispositivos. Se o usuario for direto de
    // Pessoas pra Grupos e editar um grupo de porta existente, o checklist de
    // dispositivos renderizaria vazio (nada marcado) e salvar sobrescreveria
    // device_ids com [] no backend, apagando os dispositivos reais do grupo.
    const [groupsRes, doorGroupsRes, devicesRes] = await Promise.all([
      apiJson('/api/access-control/groups', { forceRefresh: force, cacheTtl: 0 }),
      apiJson('/api/access-control/door-groups', { forceRefresh: force, cacheTtl: 0 }),
      apiJson('/api/access-control/devices', { forceRefresh: force, cacheTtl: 0 }),
    ]);
    _accessGroupRows = groupsRes?.groups || [];
    _accessDoorGroupRows = doorGroupsRes?.door_groups || [];
    _accessDeviceRows = devicesRes?.devices || [];
    if (_accessGroupSelectedId && !_accessGroupRows.some(row => row.id === _accessGroupSelectedId)) _accessGroupSelectedId = '';
    if (_accessDoorGroupSelectedId && !_accessDoorGroupRows.some(row => row.id === _accessDoorGroupSelectedId)) _accessDoorGroupSelectedId = '';
    renderAccessGroups(_accessGroupRows);
    renderAccessDoorGroups(_accessDoorGroupRows);
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel carregar grupos.', true);
  }
}

function renderAccessGroups(rows) {
  const body = document.getElementById('accessGroupsBody');
  if (!body) return;
  setText('accessGroupsCount', `${rows.length} grupo${rows.length === 1 ? '' : 's'}`);
  if (!rows.length) {
    _accessGroupSelectedId = '';
    body.innerHTML = '<tr class="empty-row"><td colspan="4">Nenhum grupo cadastrado.</td></tr>';
    syncAccessGroupFooterActions();
    scheduleResponsiveHydration(body);
    return;
  }
  body.innerHTML = rows.map(group => `
    <tr class="${group.id === _accessGroupSelectedId ? 'selected' : ''}" data-access-group-row="${esc(group.id)}">
      <td class="access-checkbox-cell"><input type="checkbox" data-access-group-check="${esc(group.id)}" aria-label="Selecionar ${esc(group.name)}" ${group.id === _accessGroupSelectedId ? 'checked' : ''}></td>
      <td><strong>${esc(group.name)}</strong></td>
      <td>${esc(group.site || '-')}</td>
      <td>${(group.member_ids || []).length}</td>
    </tr>
  `).join('');
  syncAccessGroupFooterActions();
  scheduleResponsiveHydration(body);
  lucide.createIcons();
}

function renderAccessDoorGroups(rows) {
  const body = document.getElementById('accessDoorGroupsBody');
  if (!body) return;
  setText('accessDoorGroupsCount', `${rows.length} grupo${rows.length === 1 ? '' : 's'} de porta`);
  if (!rows.length) {
    _accessDoorGroupSelectedId = '';
    body.innerHTML = '<tr class="empty-row"><td colspan="4">Nenhum grupo de porta cadastrado.</td></tr>';
    syncAccessDoorGroupFooterActions();
    scheduleResponsiveHydration(body);
    return;
  }
  body.innerHTML = rows.map(group => `
    <tr class="${group.id === _accessDoorGroupSelectedId ? 'selected' : ''}" data-access-door-group-row="${esc(group.id)}">
      <td class="access-checkbox-cell"><input type="checkbox" data-access-door-group-check="${esc(group.id)}" aria-label="Selecionar ${esc(group.name)}" ${group.id === _accessDoorGroupSelectedId ? 'checked' : ''}></td>
      <td><strong>${esc(group.name)}</strong></td>
      <td>${esc(group.site || '-')}</td>
      <td>${(group.device_ids || []).length}</td>
    </tr>
  `).join('');
  syncAccessDoorGroupFooterActions();
  scheduleResponsiveHydration(body);
  lucide.createIcons();
}

async function loadAccessRules(force = false) {
  try {
    // Busca grupos e grupos de porta junto com as regras -- sem isso a tabela de
    // regras so teria os IDs crus (people_group_id/door_group_id) pra mostrar,
    // caso o usuario abra a aba Regras sem antes visitar a aba Grupos.
    const [groupsRes, doorGroupsRes, rulesRes] = await Promise.all([
      apiJson('/api/access-control/groups', { forceRefresh: force, cacheTtl: 0 }),
      apiJson('/api/access-control/door-groups', { forceRefresh: force, cacheTtl: 0 }),
      apiJson('/api/access-control/rules', { forceRefresh: force, cacheTtl: 0 }),
    ]);
    _accessGroupRows = groupsRes?.groups || [];
    _accessDoorGroupRows = doorGroupsRes?.door_groups || [];
    _accessRuleRows = rulesRes?.rules || [];
    if (_accessRuleSelectedId && !_accessRuleRows.some(row => row.id === _accessRuleSelectedId)) _accessRuleSelectedId = '';
    renderAccessRules(_accessRuleRows);
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel carregar regras.', true);
  }
}

const ACCESS_WEEKDAY_LABELS = { 1: 'Seg', 2: 'Ter', 3: 'Qua', 4: 'Qui', 5: 'Sex', 6: 'Sab', 7: 'Dom' };

function accessRuleWeekdaysLabel(weekdays) {
  const digits = String(weekdays || '').split('').filter(d => ACCESS_WEEKDAY_LABELS[d]);
  if (digits.length === 7) return 'Todos os dias';
  if (!digits.length) return '-';
  return digits.map(d => ACCESS_WEEKDAY_LABELS[d]).join(', ');
}

function renderAccessRules(rows) {
  const body = document.getElementById('accessRulesBody');
  if (!body) return;
  setText('accessRulesCount', `${rows.length} regra${rows.length === 1 ? '' : 's'}`);
  if (!rows.length) {
    _accessRuleSelectedId = '';
    body.innerHTML = '<tr class="empty-row"><td colspan="6">Nenhuma regra cadastrada.</td></tr>';
    syncAccessRuleFooterActions();
    scheduleResponsiveHydration(body);
    return;
  }
  const groupName = id => _accessGroupRows.find(g => g.id === id)?.name || id || '-';
  const doorGroupName = id => _accessDoorGroupRows.find(g => g.id === id)?.name || id || '-';
  body.innerHTML = rows.map(rule => `
    <tr class="${rule.id === _accessRuleSelectedId ? 'selected' : ''}" data-access-rule-row="${esc(rule.id)}">
      <td class="access-checkbox-cell"><input type="checkbox" data-access-rule-check="${esc(rule.id)}" aria-label="Selecionar regra" ${rule.id === _accessRuleSelectedId ? 'checked' : ''}></td>
      <td><strong>${esc(accessRuleLabel(rule))}</strong></td>
      <td>${esc(groupName(rule.people_group_id))}</td>
      <td>${esc(doorGroupName(rule.door_group_id))}</td>
      <td>${esc(accessRuleWeekdaysLabel(rule.weekdays))}</td>
      <td>${esc(rule.time_start || '00:00')} - ${esc(rule.time_end || '23:59')}</td>
    </tr>
  `).join('');
  syncAccessRuleFooterActions();
  scheduleResponsiveHydration(body);
  lucide.createIcons();
}

function selectedAccessRule() {
  return _accessRuleRows.find(row => row.id === _accessRuleSelectedId) || null;
}

function accessRuleLabel(rule) {
  if (rule?.name) return rule.name;
  const groupName = _accessGroupRows.find(g => g.id === rule?.people_group_id)?.name || 'grupo';
  const doorGroupName = _accessDoorGroupRows.find(g => g.id === rule?.door_group_id)?.name || 'portas';
  return `${groupName} -> ${doorGroupName}`;
}

function syncAccessRuleFooterActions() {
  const rule = selectedAccessRule();
  const hasSelection = Boolean(rule);
  const hint = document.getElementById('accessRulesFooterHint');
  if (hint) hint.textContent = hasSelection ? `Selecionada: ${accessRuleLabel(rule)}` : 'Selecione uma regra para editar ou excluir.';
  const master = document.getElementById('accessRulesSelectAll');
  if (master) {
    master.checked = hasSelection;
    master.disabled = !_accessRuleRows.length;
  }
  ['btnAccessRulesFooterReuse', 'btnAccessRulesFooterEdit', 'btnAccessRulesFooterDelete'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !hasSelection;
  });
}

function toggleAccessRuleMasterCheck(event) {
  const firstRule = _accessRuleRows[0];
  _accessRuleSelectedId = event.target.checked && firstRule ? firstRule.id : '';
  renderAccessRules(_accessRuleRows);
}

function editSelectedAccessRule() {
  const rule = selectedAccessRule();
  if (!rule) {
    showToast('Selecione uma regra para editar.', true);
    return;
  }
  openAccessRuleModal(rule);
}

function reuseSelectedAccessRule() {
  const rule = selectedAccessRule();
  if (!rule) {
    showToast('Selecione uma regra para reaproveitar.', true);
    return;
  }
  openAccessRuleModal(rule, { reuse: true });
}

async function deleteSelectedAccessRule() {
  const rule = selectedAccessRule();
  if (!rule) {
    showToast('Selecione uma regra para excluir.', true);
    return;
  }
  if (_accessRuleDeleteConfirmId !== rule.id) {
    _accessRuleDeleteConfirmId = rule.id;
    showToast(`Clique novamente para excluir ${accessRuleLabel(rule)}.`);
    setTimeout(() => {
      if (_accessRuleDeleteConfirmId === rule.id) _accessRuleDeleteConfirmId = '';
    }, 4500);
    return;
  }
  try {
    const res = await api(`/api/access-control/rules/${encodeURIComponent(rule.id)}`, { method: 'DELETE' });
    await jsonOrReadableError(res, 'Nao foi possivel excluir a regra.');
    _accessRuleDeleteConfirmId = '';
    _accessRuleSelectedId = '';
    await loadAccessRules(true);
    showToast('Regra excluida.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel excluir a regra.', true);
  }
}

function handleAccessRuleAction(event) {
  const check = event.target.closest?.('[data-access-rule-check]');
  if (check) {
    _accessRuleSelectedId = check.checked ? (check.dataset.accessRuleCheck || '') : '';
    renderAccessRules(_accessRuleRows);
    return;
  }
  const row = event.target.closest?.('[data-access-rule-row]');
  if (!row) return;
  _accessRuleSelectedId = row.dataset.accessRuleRow || '';
  renderAccessRules(_accessRuleRows);
}

function handleAccessGroupAction(event) {
  const check = event.target.closest?.('[data-access-group-check]');
  if (check) {
    _accessGroupSelectedId = check.checked ? (check.dataset.accessGroupCheck || '') : '';
    renderAccessGroups(_accessGroupRows);
    return;
  }
  const row = event.target.closest?.('[data-access-group-row]');
  if (!row) return;
  _accessGroupSelectedId = row.dataset.accessGroupRow || '';
  renderAccessGroups(_accessGroupRows);
}

function handleAccessDoorGroupAction(event) {
  const check = event.target.closest?.('[data-access-door-group-check]');
  if (check) {
    _accessDoorGroupSelectedId = check.checked ? (check.dataset.accessDoorGroupCheck || '') : '';
    renderAccessDoorGroups(_accessDoorGroupRows);
    return;
  }
  const row = event.target.closest?.('[data-access-door-group-row]');
  if (!row) return;
  _accessDoorGroupSelectedId = row.dataset.accessDoorGroupRow || '';
  renderAccessDoorGroups(_accessDoorGroupRows);
}

function selectedAccessGroup() {
  return _accessGroupRows.find(row => row.id === _accessGroupSelectedId) || null;
}

function selectedAccessDoorGroup() {
  return _accessDoorGroupRows.find(row => row.id === _accessDoorGroupSelectedId) || null;
}

function syncAccessGroupFooterActions() {
  const group = selectedAccessGroup();
  const hasSelection = Boolean(group);
  const hint = document.getElementById('accessGroupsFooterHint');
  if (hint) hint.textContent = hasSelection ? `Selecionado: ${group.name}` : 'Selecione um grupo para editar ou excluir.';
  const master = document.getElementById('accessGroupsSelectAll');
  if (master) {
    master.checked = hasSelection;
    master.disabled = !_accessGroupRows.length;
  }
  ['btnAccessGroupsFooterEdit', 'btnAccessGroupsFooterDelete'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !hasSelection;
  });
}

function syncAccessDoorGroupFooterActions() {
  const group = selectedAccessDoorGroup();
  const hasSelection = Boolean(group);
  const hint = document.getElementById('accessDoorGroupsFooterHint');
  if (hint) hint.textContent = hasSelection ? `Selecionado: ${group.name}` : 'Selecione um grupo de porta para editar ou excluir.';
  const master = document.getElementById('accessDoorGroupsSelectAll');
  if (master) {
    master.checked = hasSelection;
    master.disabled = !_accessDoorGroupRows.length;
  }
  ['btnAccessDoorGroupsFooterEdit', 'btnAccessDoorGroupsFooterDelete'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !hasSelection;
  });
}

function toggleAccessGroupMasterCheck(event) {
  const firstGroup = _accessGroupRows[0];
  _accessGroupSelectedId = event.target.checked && firstGroup ? firstGroup.id : '';
  renderAccessGroups(_accessGroupRows);
}

function toggleAccessDoorGroupMasterCheck(event) {
  const firstGroup = _accessDoorGroupRows[0];
  _accessDoorGroupSelectedId = event.target.checked && firstGroup ? firstGroup.id : '';
  renderAccessDoorGroups(_accessDoorGroupRows);
}

function editSelectedAccessGroup() {
  const group = selectedAccessGroup();
  if (!group) {
    showToast('Selecione um grupo para editar.', true);
    return;
  }
  openAccessGroupModal(group);
}

function editSelectedAccessDoorGroup() {
  const group = selectedAccessDoorGroup();
  if (!group) {
    showToast('Selecione um grupo de porta para editar.', true);
    return;
  }
  openAccessDoorGroupModal(group);
}

async function deleteSelectedAccessGroup() {
  const group = selectedAccessGroup();
  if (!group) {
    showToast('Selecione um grupo para excluir.', true);
    return;
  }
  if (_accessGroupDeleteConfirmId !== group.id) {
    _accessGroupDeleteConfirmId = group.id;
    showToast(`Clique novamente para excluir ${group.name}.`);
    setTimeout(() => {
      if (_accessGroupDeleteConfirmId === group.id) _accessGroupDeleteConfirmId = '';
    }, 4500);
    return;
  }
  try {
    const res = await api(`/api/access-control/groups/${encodeURIComponent(group.id)}`, { method: 'DELETE' });
    await jsonOrReadableError(res, 'Nao foi possivel excluir o grupo.');
    _accessGroupDeleteConfirmId = '';
    _accessGroupSelectedId = '';
    await loadAccessGroups(true);
    showToast('Grupo excluido.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel excluir o grupo.', true);
  }
}

async function deleteSelectedAccessDoorGroup() {
  const group = selectedAccessDoorGroup();
  if (!group) {
    showToast('Selecione um grupo de porta para excluir.', true);
    return;
  }
  if (_accessDoorGroupDeleteConfirmId !== group.id) {
    _accessDoorGroupDeleteConfirmId = group.id;
    showToast(`Clique novamente para excluir ${group.name}.`);
    setTimeout(() => {
      if (_accessDoorGroupDeleteConfirmId === group.id) _accessDoorGroupDeleteConfirmId = '';
    }, 4500);
    return;
  }
  try {
    const res = await api(`/api/access-control/door-groups/${encodeURIComponent(group.id)}`, { method: 'DELETE' });
    await jsonOrReadableError(res, 'Nao foi possivel excluir o grupo de porta.');
    _accessDoorGroupDeleteConfirmId = '';
    _accessDoorGroupSelectedId = '';
    await loadAccessGroups(true);
    showToast('Grupo de porta excluido.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel excluir o grupo de porta.', true);
  }
}

function accessGroupPersonSearchText(person) {
  return [
    person.full_name,
    person.enrollment_code,
    person.document_id,
    person.guardian_phone,
    person.class_name,
    person.site,
  ].filter(Boolean).join(' ').toLowerCase();
}

function accessGroupPersonMatchesFilters(person) {
  const search = document.getElementById('accessGroupMemberSearch')?.value?.trim().toLowerCase() || '';
  const type = document.getElementById('accessGroupAutoType')?.value || '';
  const site = document.getElementById('accessGroupAutoSite')?.value || '';
  const className = document.getElementById('accessGroupAutoClass')?.value || '';
  const status = document.getElementById('accessGroupAutoStatus')?.value || '';
  if (search && !accessGroupPersonSearchText(person).includes(search)) return false;
  if (type && String(person.person_type || 'student') !== type) return false;
  if (site && String(person.site || '') !== site) return false;
  if (className && String(person.class_name || '') !== className) return false;
  if (status === 'active' && person.active === false) return false;
  if (status === 'inactive' && person.active !== false) return false;
  return true;
}

function accessGroupPersonMeta(person) {
  return [accessPersonTypeLabel(person.person_type), person.enrollment_code, person.class_name, person.site]
    .filter(Boolean)
    .join(' | ') || 'Sem detalhes';
}

function accessGroupPersonRow(person, selected = false) {
  return `
    <button class="access-smart-member ${selected ? 'selected' : ''}" type="button" data-access-group-person="${esc(person.id)}" title="${esc(person.full_name || '')}">
      ${accessPersonTypeIcon(person.person_type)}
      <span>
        <strong>${esc(person.full_name || 'Pessoa sem nome')}</strong>
        <small>${esc(accessGroupPersonMeta(person))}</small>
      </span>
      <i data-lucide="${selected ? 'check' : 'plus'}"></i>
    </button>
  `;
}

function fillAccessGroupSmartOptions() {
  const currentSite = document.getElementById('accessGroupSite')?.value?.trim() || '';
  const sites = [...new Set(_accessGroupPeopleRows.map(p => String(p.site || '').trim()).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, 'pt'));
  const classes = [...new Set(_accessGroupPeopleRows.map(p => String(p.class_name || '').trim()).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, 'pt', { numeric: true }));
  const siteSelect = document.getElementById('accessGroupAutoSite');
  const classSelect = document.getElementById('accessGroupAutoClass');
  if (siteSelect) {
    siteSelect.innerHTML = `<option value="">Todos os sites</option>${sites.map(site => `<option value="${esc(site)}">${esc(site)}</option>`).join('')}`;
    siteSelect.value = sites.includes(currentSite) ? currentSite : '';
  }
  if (classSelect) {
    classSelect.innerHTML = `<option value="">Todas as turmas</option>${classes.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('')}`;
  }
}

function renderAccessGroupSmartMembers() {
  const resultEl = document.getElementById('accessGroupMemberResults');
  const selectedEl = document.getElementById('accessGroupSelectedMembers');
  const previewEl = document.getElementById('accessGroupSmartPreview');
  const filtered = _accessGroupPeopleRows.filter(accessGroupPersonMatchesFilters);
  const selectedRows = _accessGroupPeopleRows.filter(p => _accessGroupSelectedPeople.has(p.id));
  const notSelectedFiltered = filtered.filter(p => !_accessGroupSelectedPeople.has(p.id));
  if (previewEl) previewEl.textContent = `${filtered.length} pessoa(s) no filtro | ${selectedRows.length} pessoa(s) no grupo`;
  setText('accessGroupFilteredCount', `${filtered.length} no filtro`);
  setText('accessGroupSelectedCount', `${selectedRows.length} selecionada(s)`);
  if (resultEl) {
    resultEl.innerHTML = notSelectedFiltered.slice(0, 80).map(p => accessGroupPersonRow(p, false)).join('')
      || '<p class="muted-block">Nenhuma pessoa encontrada com esses filtros.</p>';
  }
  if (selectedEl) {
    selectedEl.innerHTML = selectedRows.slice(0, 120).map(p => accessGroupPersonRow(p, true)).join('')
      || '<p class="muted-block">Nenhuma pessoa adicionada ainda.</p>';
  }
  lucide.createIcons();
}

function bindAccessGroupSmartBuilder() {
  ['accessGroupMemberSearch', 'accessGroupAutoType', 'accessGroupAutoSite', 'accessGroupAutoClass', 'accessGroupAutoStatus'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', renderAccessGroupSmartMembers);
    document.getElementById(id)?.addEventListener('change', renderAccessGroupSmartMembers);
  });
  document.getElementById('btnAccessGroupAddFiltered')?.addEventListener('click', () => {
    _accessGroupPeopleRows.filter(accessGroupPersonMatchesFilters).forEach(person => _accessGroupSelectedPeople.add(person.id));
    renderAccessGroupSmartMembers();
  });
  document.getElementById('btnAccessGroupClearSelected')?.addEventListener('click', () => {
    _accessGroupSelectedPeople.clear();
    renderAccessGroupSmartMembers();
  });
  document.getElementById('btnAccessGroupToggleFilters')?.addEventListener('click', event => {
    const controls = document.getElementById('accessGroupSmartControls');
    if (!controls) return;
    const collapsed = controls.classList.toggle('collapsed');
    event.currentTarget.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    event.currentTarget.innerHTML = collapsed
      ? '<i data-lucide="sliders-horizontal"></i> Filtros'
      : '<i data-lucide="chevron-up"></i> Ocultar filtros';
    lucide.createIcons();
  });
  const checklist = document.getElementById('accessGroupMembersChecklist');
  if (checklist && !checklist.dataset.smartBound) {
    checklist.dataset.smartBound = '1';
    checklist.addEventListener('click', event => {
    const item = event.target.closest?.('[data-access-group-person]');
    if (!item) return;
    const id = item.dataset.accessGroupPerson;
    if (_accessGroupSelectedPeople.has(id)) _accessGroupSelectedPeople.delete(id);
    else _accessGroupSelectedPeople.add(id);
    renderAccessGroupSmartMembers();
    });
  }
}

async function openAccessGroupModal(group = null) {
  const item = group || {};
  setText('accessGroupModalTitle', item.id ? 'Editar grupo' : 'Novo grupo');
  document.getElementById('accessGroupId').value = item.id || '';
  document.getElementById('accessGroupName').value = item.name || '';
  document.getElementById('accessGroupSite').value = item.site || '';
  const checklist = document.getElementById('accessGroupMembersChecklist');
  const memberIds = new Set(item.member_ids || []);
  _accessGroupPeopleRows = [];
  _accessGroupSelectedPeople = new Set(item.member_ids || []);
  if (checklist) checklist.innerHTML = '<p class="muted-block">Carregando pessoas...</p>';
  document.getElementById('modalAccessGroup')?.classList.remove('hidden');
  lucide.createIcons();

  // O modal fica visivel (linha acima) antes do await abaixo terminar --
  // desabilita o Salvar enquanto a lista de pessoas carrega para o operador
  // nao conseguir salvar com o checklist ainda em "Carregando pessoas...",
  // o que postaria member_ids: [] e apagaria a composicao real do grupo.
  const groupSaveBtn = document.getElementById('btnAccessGroupSave');
  if (groupSaveBtn) groupSaveBtn.disabled = true;

  // Busca a lista COMPLETA de pessoas aqui, sem search/active -- NAO reusa
  // _accessPeopleRows, que carrega o resultado ja filtrado pelos controles da
  // aba Pessoas. Como saveAccessGroupFromForm envia os marcados como a lista
  // COMPLETA de member_ids e o backend faz DELETE + reinsert, montar o
  // checklist a partir de uma lista filtrada apagava do grupo todo mundo que
  // nao casava com o filtro ativo (ex: busca "Silva" na aba Pessoas, edita o
  // nome de um grupo de 30 pessoas, salva -> sobram so os "Silva").
  _accessGroupPeopleLoadFailed = false;
  let people = [];
  try {
    const res = await apiJson('/api/access-control/people', { forceRefresh: true, cacheTtl: 0 });
    // apiJson NAO lanca em resposta HTTP nao-2xx -- res.ok=false ou um 401
    // (onde api() ja devolve null) voltam como res == null, sem excecao. So
    // depender do try/catch aqui deixava esse caso passar como sucesso: res
    // era null, res?.people virava [] "silenciosamente" e o guard
    // _accessGroupPeopleLoadFailed nunca disparava -- exatamente o mesmo
    // data-loss que esse guard deveria evitar, so que por outro caminho.
    if (!res || !Array.isArray(res.people)) {
      throw new Error('Nao foi possivel carregar a lista de pessoas.');
    }
    people = res.people;
    _accessGroupPeopleRows = people;
  } catch (err) {
    _accessGroupPeopleLoadFailed = true;
    if (checklist) checklist.innerHTML = '<p class="muted-block">Nao foi possivel carregar a lista de pessoas. Feche e reabra o grupo para tentar de novo.</p>';
    showToast(err?.message || 'Nao foi possivel carregar a lista de pessoas.', true);
    return;
  } finally {
    if (groupSaveBtn) groupSaveBtn.disabled = false;
  }
  if (checklist) {
    checklist.classList.remove('access-selection-list');
    checklist.innerHTML = `
      <div class="access-smart-group-builder">
        <div class="access-smart-summary">
          <div>
            <strong>Pessoas do grupo</strong>
            <small id="accessGroupSmartPreview">${people.length} pessoa(s) disponivel(is) | ${memberIds.size} pessoa(s) no grupo</small>
          </div>
          <div class="access-smart-summary-actions">
            <span class="pill neutral" id="accessGroupSelectedCount">${memberIds.size} selecionada(s)</span>
            <button class="secondary-action" id="btnAccessGroupToggleFilters" type="button" aria-expanded="false" aria-controls="accessGroupSmartControls"><i data-lucide="sliders-horizontal"></i> Filtros</button>
          </div>
        </div>
        <div class="access-smart-controls collapsed" id="accessGroupSmartControls">
          <div class="access-smart-filters">
            <label class="search-box">
              <i data-lucide="search"></i>
              <input id="accessGroupMemberSearch" type="search" placeholder="Buscar nome, matricula, documento ou WhatsApp">
            </label>
            <select id="accessGroupAutoType">
              <option value="">Todos os tipos</option>
              <option value="student">Alunos</option>
              <option value="employee">Funcionarios</option>
              <option value="visitor">Visitantes</option>
            </select>
            <select id="accessGroupAutoSite"><option value="">Todos os sites</option></select>
            <select id="accessGroupAutoClass"><option value="">Todas as turmas</option></select>
            <select id="accessGroupAutoStatus">
              <option value="">Todos</option>
              <option value="active">Ativos</option>
              <option value="inactive">Inativos</option>
            </select>
            <button class="secondary-action" id="btnAccessGroupAddFiltered" type="button"><i data-lucide="list-plus"></i> Adicionar filtrados</button>
            <button class="ghost-action" id="btnAccessGroupClearSelected" type="button">Limpar selecionados</button>
          </div>
          <div class="access-smart-actions">
            <span id="accessGroupFilteredCount">${people.length} no filtro</span>
          </div>
        </div>
        <div class="access-smart-columns">
          <section>
            <div class="access-smart-column-title"><span>Encontrados</span><small>Adicionar</small></div>
            <div class="access-smart-list" id="accessGroupMemberResults"></div>
          </section>
          <section>
            <div class="access-smart-column-title"><span>No grupo</span><small>Remover</small></div>
            <div class="access-smart-list" id="accessGroupSelectedMembers"></div>
          </section>
        </div>
      </div>
    `;
    fillAccessGroupSmartOptions();
    bindAccessGroupSmartBuilder();
    renderAccessGroupSmartMembers();
  }
}

function closeAccessGroupModal() {
  document.getElementById('modalAccessGroup')?.classList.add('hidden');
}

async function saveAccessGroupFromForm(event) {
  event.preventDefault();
  const btn = document.getElementById('btnAccessGroupSave');
  const oldHtml = btn?.innerHTML;
  if (_accessGroupPeopleLoadFailed) {
    showToast('A lista de pessoas nao carregou -- feche e reabra o grupo antes de salvar.', true);
    return;
  }
  const memberIds = Array.from(_accessGroupSelectedPeople);
  const payload = {
    id: document.getElementById('accessGroupId').value.trim(),
    name: document.getElementById('accessGroupName').value.trim(),
    site: document.getElementById('accessGroupSite').value.trim(),
    member_ids: memberIds,
  };
  if (!payload.name) {
    showToast('Informe o nome do grupo.', true);
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Salvando';
    lucide.createIcons();
  }
  try {
    const res = await api('/api/access-control/groups', { method: 'POST', body: JSON.stringify(payload) });
    await jsonOrReadableError(res, 'Nao foi possivel salvar o grupo.');
    closeAccessGroupModal();
    await loadAccessGroups(true);
    showToast('Grupo salvo.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel salvar o grupo.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="save"></i> Salvar grupo';
      lucide.createIcons();
    }
  }
}

const ACCESS_DOOR_GROUP_DEVICE_RENDER_LIMIT = 200;
let _accessDoorGroupSelectedIds = new Set();
let _accessDoorGroupDeviceSearchTimer = null;

function accessDoorGroupDeviceFilters() {
  return {
    term: (document.getElementById('accessDoorGroupDeviceSearch')?.value || '').trim().toLowerCase(),
    site: document.getElementById('accessDoorGroupDeviceSite')?.value || '',
    model: document.getElementById('accessDoorGroupDeviceModel')?.value || '',
    status: document.getElementById('accessDoorGroupDeviceStatus')?.value || '',
    selection: document.getElementById('accessDoorGroupDeviceSelection')?.value || '',
  };
}

function accessDoorGroupDeviceStatusKey(device) {
  return String(device?.status || '').trim().toLowerCase() || 'desconhecido';
}

function accessDoorGroupFilteredDevices() {
  const f = accessDoorGroupDeviceFilters();
  return _accessDeviceRows.filter(device => {
    if (f.site && (String(device.site || '').trim() || '__none__') !== f.site) return false;
    if (f.model && (String(device.model || '').trim() || '__none__') !== f.model) return false;
    if (f.status && accessDoorGroupDeviceStatusKey(device) !== f.status) return false;
    if (f.selection === 'selected' && !_accessDoorGroupSelectedIds.has(device.id)) return false;
    if (f.selection === 'unselected' && _accessDoorGroupSelectedIds.has(device.id)) return false;
    if (!f.term) return true;
    const haystack = [device.name, device.host, device.model, device.site, accessDeviceConnectorLabel(device)]
      .filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(f.term);
  });
}

function syncAccessDoorGroupCounters() {
  const selected = _accessDoorGroupSelectedIds.size;
  setText('accessDoorGroupSelectedCountText', selected);
  setText('accessDoorGroupSelectedPill', `${selected} selecionado(s)`);
}

function renderAccessDoorGroupDeviceList() {
  const list = document.getElementById('accessDoorGroupDeviceList');
  if (!list) return;
  if (!_accessDeviceRows.length) {
    setText('accessDoorGroupFilteredCount', '0 no filtro');
    list.innerHTML = '<p class="muted-block">Cadastre dispositivos primeiro.</p>';
    return;
  }
  const filtered = accessDoorGroupFilteredDevices();
  const visible = filtered.slice(0, ACCESS_DOOR_GROUP_DEVICE_RENDER_LIMIT);
  setText('accessDoorGroupFilteredCount', `${filtered.length} no filtro`);
  if (!filtered.length) {
    list.innerHTML = '<p class="muted-block">Nenhum dispositivo encontrado com esses filtros.</p>';
    return;
  }
  const cards = visible.map(d => {
    const checked = _accessDoorGroupSelectedIds.has(d.id);
    const details = [d.host, d.model, d.site].filter(Boolean).join(' | ') || 'Sem detalhes';
    return `
      <label class="access-door-device-card ${checked ? 'selected' : ''}" for="accessDoorGroupDevice-${esc(d.id)}">
        <input id="accessDoorGroupDevice-${esc(d.id)}" type="checkbox" value="${esc(d.id)}" ${checked ? 'checked' : ''}>
        <span>
          <strong>${esc(d.name || 'Dispositivo sem nome')}</strong>
          <small>${esc(details)}</small>
        </span>
        <i data-lucide="${checked ? 'check-circle-2' : 'circle'}"></i>
      </label>
    `;
  }).join('');
  const overflow = filtered.length > visible.length
    ? `<p class="muted-block access-door-device-overflow">Mostrando ${visible.length} de ${filtered.length}. Refine a busca para ver o restante -- os selecionados fora da lista continuam salvos.</p>`
    : '';
  list.innerHTML = cards + overflow;
  lucide.createIcons();
}

function accessDoorGroupDeviceOptions(field, allLabel, noneLabel) {
  const values = new Set(_accessDeviceRows.map(device => String(device[field] || '').trim()));
  const named = Array.from(values).filter(Boolean).sort((a, b) => a.localeCompare(b, 'pt-BR'));
  const options = [`<option value="">${allLabel}</option>`];
  named.forEach(value => options.push(`<option value="${esc(value)}">${esc(value)}</option>`));
  if (values.has('')) options.push(`<option value="__none__">${noneLabel}</option>`);
  return options.join('');
}

function accessDoorGroupStatusOptions() {
  const values = Array.from(new Set(_accessDeviceRows.map(accessDoorGroupDeviceStatusKey)))
    .sort((a, b) => a.localeCompare(b, 'pt-BR'));
  return [`<option value="">Todos os status</option>`]
    .concat(values.map(value => `<option value="${esc(value)}">${esc(value)}</option>`))
    .join('');
}

function openAccessDoorGroupModal(doorGroup = null) {
  const item = doorGroup || {};
  setText('accessDoorGroupModalTitle', item.id ? 'Editar grupo de porta' : 'Novo grupo de porta');
  document.getElementById('accessDoorGroupId').value = item.id || '';
  document.getElementById('accessDoorGroupName').value = item.name || '';
  document.getElementById('accessDoorGroupSite').value = item.site || '';
  const checklist = document.getElementById('accessDoorGroupDevicesChecklist');
  _accessDoorGroupSelectedIds = new Set(item.device_ids || []);
  if (checklist) {
    checklist.classList.remove('access-selection-list');
    checklist.innerHTML = `
      <div class="access-door-group-builder">
        <div class="access-door-group-summary">
          <div>
            <strong>Dispositivos do grupo</strong>
            <small><span id="accessDoorGroupAvailableCount">${_accessDeviceRows.length}</span> disponivel(is) | <span id="accessDoorGroupSelectedCountText">${_accessDoorGroupSelectedIds.size}</span> selecionado(s)</small>
          </div>
          <span class="pill neutral" id="accessDoorGroupSelectedPill">${_accessDoorGroupSelectedIds.size} selecionado(s)</span>
        </div>
        <div class="access-smart-filters access-door-device-filters">
          <label class="search-box">
            <i data-lucide="search"></i>
            <input id="accessDoorGroupDeviceSearch" type="search" placeholder="Buscar nome, IP, modelo, site ou conector">
          </label>
          <select id="accessDoorGroupDeviceSite">${accessDoorGroupDeviceOptions('site', 'Todos os sites', 'Sem site')}</select>
          <select id="accessDoorGroupDeviceModel">${accessDoorGroupDeviceOptions('model', 'Todos os modelos', 'Sem modelo')}</select>
          <select id="accessDoorGroupDeviceStatus">${accessDoorGroupStatusOptions()}</select>
          <select id="accessDoorGroupDeviceSelection">
            <option value="">Selecionados e nao selecionados</option>
            <option value="selected">Somente selecionados</option>
            <option value="unselected">Somente nao selecionados</option>
          </select>
          <button class="secondary-action" id="btnAccessDoorGroupSelectFiltered" type="button"><i data-lucide="list-plus"></i> Selecionar filtrados</button>
          <button class="ghost-action" id="btnAccessDoorGroupClearSelected" type="button">Limpar selecionados</button>
        </div>
        <div class="access-smart-actions">
          <span id="accessDoorGroupFilteredCount">${_accessDeviceRows.length} no filtro</span>
        </div>
        <div class="access-door-device-list" id="accessDoorGroupDeviceList"></div>
      </div>
    `;
    const rerender = () => renderAccessDoorGroupDeviceList();
    ['accessDoorGroupDeviceSite', 'accessDoorGroupDeviceModel', 'accessDoorGroupDeviceStatus', 'accessDoorGroupDeviceSelection']
      .forEach(id => document.getElementById(id)?.addEventListener('change', rerender));
    document.getElementById('accessDoorGroupDeviceSearch')?.addEventListener('input', () => {
      clearTimeout(_accessDoorGroupDeviceSearchTimer);
      _accessDoorGroupDeviceSearchTimer = setTimeout(rerender, 150);
    });
    document.getElementById('accessDoorGroupDeviceList')?.addEventListener('change', event => {
      const input = event.target.closest('input[type="checkbox"]');
      if (!input) return;
      if (input.checked) _accessDoorGroupSelectedIds.add(input.value);
      else _accessDoorGroupSelectedIds.delete(input.value);
      const card = input.closest('.access-door-device-card');
      card?.classList.toggle('selected', input.checked);
      const icon = card?.querySelector('i, svg');
      if (icon) {
        icon.outerHTML = `<i data-lucide="${input.checked ? 'check-circle-2' : 'circle'}"></i>`;
        lucide.createIcons();
      }
      syncAccessDoorGroupCounters();
    });
    document.getElementById('btnAccessDoorGroupSelectFiltered')?.addEventListener('click', () => {
      accessDoorGroupFilteredDevices().forEach(device => _accessDoorGroupSelectedIds.add(device.id));
      syncAccessDoorGroupCounters();
      renderAccessDoorGroupDeviceList();
    });
    document.getElementById('btnAccessDoorGroupClearSelected')?.addEventListener('click', () => {
      _accessDoorGroupSelectedIds.clear();
      syncAccessDoorGroupCounters();
      renderAccessDoorGroupDeviceList();
    });
    renderAccessDoorGroupDeviceList();
  }
  document.getElementById('modalAccessDoorGroup')?.classList.remove('hidden');
  lucide.createIcons();
}

function closeAccessDoorGroupModal() {
  document.getElementById('modalAccessDoorGroup')?.classList.add('hidden');
}

async function saveAccessDoorGroupFromForm(event) {
  event.preventDefault();
  const btn = document.getElementById('btnAccessDoorGroupSave');
  const oldHtml = btn?.innerHTML;
  const deviceIds = Array.from(_accessDoorGroupSelectedIds);
  const payload = {
    id: document.getElementById('accessDoorGroupId').value.trim(),
    name: document.getElementById('accessDoorGroupName').value.trim(),
    site: document.getElementById('accessDoorGroupSite').value.trim(),
    device_ids: deviceIds,
  };
  if (!payload.name) {
    showToast('Informe o nome do grupo de porta.', true);
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Salvando';
    lucide.createIcons();
  }
  try {
    const res = await api('/api/access-control/door-groups', { method: 'POST', body: JSON.stringify(payload) });
    await jsonOrReadableError(res, 'Nao foi possivel salvar o grupo de porta.');
    closeAccessDoorGroupModal();
    await loadAccessGroups(true);
    showToast('Grupo de porta salvo.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel salvar o grupo de porta.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="save"></i> Salvar grupo de porta';
      lucide.createIcons();
    }
  }
}

function accessRuleSelectedWeekdays() {
  const raw = document.getElementById('accessRuleWeekdays')?.value || '';
  return String(raw).split('').filter(d => ACCESS_WEEKDAY_LABELS[d]);
}

function accessFirstPeopleGroupWithoutRule(currentPeopleGroupId = '') {
  const usedGroupIds = new Set(_accessRuleRows.map(rule => rule.people_group_id).filter(Boolean));
  return _accessGroupRows.find(group => group.id !== currentPeopleGroupId && !usedGroupIds.has(group.id)) || null;
}

function setAccessRuleWeekdays(digits) {
  const ordered = Object.keys(ACCESS_WEEKDAY_LABELS).filter(d => digits.includes(d));
  const input = document.getElementById('accessRuleWeekdays');
  if (input) input.value = ordered.join('');
  renderAccessRuleWeekdayPicker();
}

function renderAccessRuleWeekdayPicker() {
  const picker = document.getElementById('accessRuleWeekdayPicker');
  if (!picker) return;
  const selected = accessRuleSelectedWeekdays();
  picker.innerHTML = Object.entries(ACCESS_WEEKDAY_LABELS).map(([digit, label]) => `
    <button class="access-weekday-chip ${selected.includes(digit) ? 'selected' : ''}" type="button"
      data-access-weekday="${digit}" aria-pressed="${selected.includes(digit) ? 'true' : 'false'}">${label}</button>
  `).join('');
  updateAccessRuleSummary();
}

function updateAccessRuleSummary() {
  const summary = document.getElementById('accessRuleSummary');
  if (!summary) return;
  const peopleGroup = document.getElementById('accessRulePeopleGroup');
  const doorGroup = document.getElementById('accessRuleDoorGroup');
  const days = accessRuleSelectedWeekdays();
  if (!days.length) {
    summary.textContent = 'Escolha pelo menos um dia da semana.';
    summary.classList.add('warn');
    return;
  }
  summary.classList.remove('warn');
  const start = document.getElementById('accessRuleTimeStart')?.value || '00:00';
  const end = document.getElementById('accessRuleTimeEnd')?.value || '23:59';
  const people = peopleGroup?.selectedOptions?.[0]?.textContent?.trim() || 'o grupo';
  const doors = doorGroup?.selectedOptions?.[0]?.textContent?.trim() || 'as portas';
  const when = days.length === 7 ? 'todos os dias' : days.map(d => ACCESS_WEEKDAY_LABELS[d]).join(', ');
  const name = document.getElementById('accessRuleName')?.value.trim();
  const prefix = name ? `${name}: ` : '';
  summary.textContent = `${prefix}${people} pode abrir ${doors} ${when}, das ${start} as ${end}.`;
}

function handleAccessRuleWeekdayClick(event) {
  const chip = event.target.closest?.('[data-access-weekday]');
  if (!chip) return;
  const digit = chip.dataset.accessWeekday;
  const selected = accessRuleSelectedWeekdays();
  setAccessRuleWeekdays(selected.includes(digit) ? selected.filter(d => d !== digit) : selected.concat(digit));
}

function handleAccessRuleWeekdayPreset(event) {
  const btn = event.target.closest?.('[data-access-weekday-preset]');
  if (!btn) return;
  setAccessRuleWeekdays(btn.dataset.accessWeekdayPreset.split(''));
}

function handleAccessRuleTimePreset(event) {
  const btn = event.target.closest?.('[data-access-time-preset]');
  if (!btn) return;
  const [start, end] = btn.dataset.accessTimePreset.split('-');
  document.getElementById('accessRuleTimeStart').value = start;
  document.getElementById('accessRuleTimeEnd').value = end;
  updateAccessRuleSummary();
}

function bindAccessRuleModal() {
  document.getElementById('accessRuleTimePresets')?.addEventListener('click', handleAccessRuleTimePreset);
  document.getElementById('accessRuleName')?.addEventListener('input', updateAccessRuleSummary);
  document.getElementById('accessRuleWeekdayPicker')?.addEventListener('click', handleAccessRuleWeekdayClick);
  document.querySelector('.access-weekday-presets')?.addEventListener('click', handleAccessRuleWeekdayPreset);
  ['accessRulePeopleGroup', 'accessRuleDoorGroup', 'accessRuleTimeStart', 'accessRuleTimeEnd']
    .forEach(id => document.getElementById(id)?.addEventListener('change', updateAccessRuleSummary));
}

function openAccessRuleModal(rule = null, options = {}) {
  const item = rule || {};
  const isReuse = Boolean(options && options.reuse);
  const suggestedGroup = isReuse ? accessFirstPeopleGroupWithoutRule(item.people_group_id) : null;
  setText('accessRuleModalTitle', item.id && !isReuse ? 'Editar regra' : isReuse ? 'Reaproveitar regra' : 'Nova regra');
  document.getElementById('accessRuleId').value = item.id && !isReuse ? item.id : '';
  const peopleSelect = document.getElementById('accessRulePeopleGroup');
  const doorSelect = document.getElementById('accessRuleDoorGroup');
  if (peopleSelect) {
    const usedGroupIds = new Set(_accessRuleRows.map(rule => rule.people_group_id).filter(Boolean));
    peopleSelect.innerHTML = _accessGroupRows.map(g => {
      const available = isReuse && g.id !== item.people_group_id && !usedGroupIds.has(g.id);
      const suffix = available ? ' (sem regra)' : '';
      return `<option value="${esc(g.id)}">${esc(g.name)}${suffix}</option>`;
    }).join('')
      || '<option value="">Cadastre um grupo primeiro</option>';
    peopleSelect.value = (suggestedGroup || {}).id || item.people_group_id || '';
  }
  if (doorSelect) {
    doorSelect.innerHTML = _accessDoorGroupRows.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('')
      || '<option value="">Cadastre um grupo de porta primeiro</option>';
    doorSelect.value = item.door_group_id || '';
  }
  document.getElementById('accessRuleName').value = isReuse && item.name ? `${item.name} - copia` : item.name || '';
  document.getElementById('accessRuleWeekdays').value = item.weekdays || '1234567';
  // Regra sem horario vale o dia inteiro -- a tabela ja mostra 00:00-23:59 nesse
  // caso, entao o modal abre com os mesmos valores em vez de dois campos vazios.
  document.getElementById('accessRuleTimeStart').value = item.time_start || '00:00';
  document.getElementById('accessRuleTimeEnd').value = item.time_end || '23:59';
  renderAccessRuleWeekdayPicker();
  document.getElementById('modalAccessRule')?.classList.remove('hidden');
  lucide.createIcons();
}

function closeAccessRuleModal() {
  document.getElementById('modalAccessRule')?.classList.add('hidden');
}

async function saveAccessRuleFromForm(event) {
  event.preventDefault();
  const btn = document.getElementById('btnAccessRuleSave');
  const oldHtml = btn?.innerHTML;
  const payload = {
    id: document.getElementById('accessRuleId').value.trim(),
    people_group_id: document.getElementById('accessRulePeopleGroup').value,
    door_group_id: document.getElementById('accessRuleDoorGroup').value,
    name: document.getElementById('accessRuleName').value.trim(),
    weekdays: document.getElementById('accessRuleWeekdays').value.trim() || '1234567',
    time_start: document.getElementById('accessRuleTimeStart').value,
    time_end: document.getElementById('accessRuleTimeEnd').value,
  };
  if (!payload.people_group_id || !payload.door_group_id) {
    showToast('Escolha o grupo de pessoas e o grupo de portas.', true);
    return;
  }
  if (!accessRuleSelectedWeekdays().length) {
    showToast('Escolha pelo menos um dia da semana.', true);
    return;
  }
  if (payload.time_start && payload.time_end && payload.time_end < payload.time_start) {
    showToast('O horario fim precisa ser depois do horario inicio.', true);
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Salvando';
    lucide.createIcons();
  }
  try {
    const res = await api('/api/access-control/rules', { method: 'POST', body: JSON.stringify(payload) });
    await jsonOrReadableError(res, 'Nao foi possivel salvar a regra.');
    closeAccessRuleModal();
    await loadAccessRules(true);
    showToast('Regra salva.');
  } catch (err) {
    showToast(err?.message || 'Nao foi possivel salvar a regra.', true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml || '<i data-lucide="save"></i> Salvar regra';
      lucide.createIcons();
    }
  }
}
