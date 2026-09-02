// Implantacao > OLT: cadastro das OLTs usadas na operacao.
//
// A senha nunca chega aqui -- a API devolve so `has_password`. Por isso o campo
// de senha nasce sempre vazio ao editar, e vazio significa "manter a atual"
// (ver olt_registry.save_olt no backend). Se um dia a tela passar a exibir a
// senha, o teste scripts/sightops_olt_routes_test.py quebra de proposito.

let _oltRegRows = [];
let _oltRegFiltered = [];
let _oltRegConnectors = [];
const _oltRegTestStates = new Map();
const _oltRegSyncStates = new Map();
let _oltRegSyncTimer = null;
let _oltRegSyncPanelDismissed = false;
const OLT_MODELS_BY_VENDOR = {
  Intelbras: ['8820i', '4840E'],
  Huawei: ['MA5608T', 'MA5680T', 'MA5800-X2', 'MA5800-X7', 'MA5800-X15', 'MA5800-X17'],
  ZTE: ['C300', 'C320', 'C600'],
  Parks: ['Fiberlink 1100', 'Fiberlink 2000'],
};

function _oltRegPopulateModels(selected = '') {
  const vendor = document.getElementById('oltRegVendor')?.value || '';
  const model = document.getElementById('oltRegModel');
  if (!model) return;
  const models = OLT_MODELS_BY_VENDOR[vendor] || [];
  model.disabled = !vendor;
  model.innerHTML = `<option value="">${vendor ? 'Escolha o modelo' : 'Escolha primeiro o fabricante'}</option>`
    + models.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
  if (selected && !models.some(value => value.toLowerCase() === String(selected).toLowerCase())) {
    model.insertAdjacentHTML('beforeend', `<option value="${esc(selected)}">${esc(selected)} (cadastrado)</option>`);
  }
  model.value = models.find(value => value.toLowerCase() === String(selected).toLowerCase()) || selected || '';
}

async function loadDeployOlt() {
  await Promise.all([_oltRegLoadConnectors(), _oltRegLoadSites(), _oltRegLoad()]);
}

async function _oltRegLoad() {
  const tbody = document.getElementById('oltRegRows');
  try {
    const res = await apiJson('/api/olt/registry');
    _oltRegRows = Array.isArray(res?.items) ? res.items : [];
    _oltRegRows.forEach(item => {
      const kind = String(item.last_test_status || '').toLowerCase();
      if (!['ok', 'error'].includes(kind)) return;
      _oltRegTestStates.set(Number(item.id), {
        kind,
        detail: item.last_test_detail || '',
        testedAt: item.last_tested_at || '',
      });
    });
  } catch (err) {
    _oltRegRows = [];
    if (tbody) tbody.innerHTML = `<tr><td colspan="9">Falha ao carregar: ${esc(err?.message || err)}</td></tr>`;
    return;
  }
  _oltRegApplyFilter();
}

// O seletor de conector reusa a lista de conectores ja existente. Falhar aqui
// nao pode impedir o cadastro: OLT alcancavel direto na rede nao usa conector.
async function _oltRegLoadConnectors() {
  const sel = document.getElementById('oltRegConnector');
  if (!sel) return;
  try {
    const res = await apiJson('/api/connectors');
    const itens = Array.isArray(res?.items) ? res.items : (Array.isArray(res?.connectors) ? res.connectors : []);
    _oltRegConnectors = itens;
    const atual = sel.value;
    sel.innerHTML = '<option value="">Acesso direto (sem conector)</option>'
      + itens.map(c => `<option value="${esc(c.id)}">${esc(c.name || c.id)}</option>`).join('');
    sel.value = atual;
    // A lista de OLTs e conectores carrega em paralelo. Renderiza novamente
    // para trocar o UUID tecnico pelo nome legivel assim que a rede chegar.
    if (_oltRegRows.length) _oltRegApplyFilter();
  } catch (_) {
    /* segue com a opcao de acesso direto */
  }
}

async function _oltRegLoadSites() {
  const list = document.getElementById('oltRegSiteList');
  if (!list) return;
  try {
    const res = await apiJson('/api/db/sites');
    const sites = Array.isArray(res?.sites) ? res.sites : [];
    list.innerHTML = sites.map(site => `<option value="${esc(site?.name || '')}"></option>`).join('');
  } catch (_) {
    list.innerHTML = '';
  }
}

function _oltRegConnectorLabel(id) {
  if (!id) return 'Acesso direto';
  const row = _oltRegConnectors.find(item => String(item.id || item.connector_id || '') === String(id));
  return row ? (row.name || row.client || row.id || id) : id;
}

function _oltRegApplyFilter() {
  const termo = (document.getElementById('oltRegSearch')?.value || '').trim().toLowerCase();
  _oltRegFiltered = !termo ? _oltRegRows : _oltRegRows.filter(o =>
    [o.name, o.host, o.site, o.vendor, o.model, o.username, o.notes, _oltRegConnectorLabel(o.connector_id)]
      .some(v => String(v || '').toLowerCase().includes(termo)));
  _oltRegRender();
}

function _oltRegRender() {
  const tbody = document.getElementById('oltRegRows');
  const contador = document.getElementById('oltRegCount');
  if (!tbody) return;

  if (contador) {
    const total = _oltRegRows.length;
    const vendo = _oltRegFiltered.length;
    contador.textContent = total === 0 ? 'Nenhuma OLT cadastrada'
      : vendo === total ? `${total} OLT(s)`
      : `${vendo} de ${total} OLT(s)`;
  }

  if (!_oltRegFiltered.length) {
    tbody.innerHTML = `<tr><td colspan="9">${
      _oltRegRows.length ? 'Nenhuma OLT corresponde a busca.'
                         : 'Nenhuma OLT cadastrada. Use o formulario ao lado.'}</td></tr>`;
    return;
  }

  tbody.innerHTML = _oltRegFiltered.map(o => `
    <tr>
      <td title="${esc(o.name)}"><strong>${esc(o.name)}</strong>${o.notes ? `<br><span class="text-muted">${esc(o.notes)}</span>` : ''}</td>
      <td title="${esc(o.host)}">${esc(o.host)}</td>
      <td title="${esc(o.site)}">${esc(o.site) || '<span class="text-muted">--</span>'}</td>
      <td><span class="olt-vendor-model" title="${esc([o.vendor, o.model].filter(Boolean).join(' / ') || 'Nao informado')}"><strong>${esc(o.vendor) || '--'}</strong><span>${esc(o.model) || 'sem modelo'}</span></span></td>
      <td title="${esc(_oltRegConnectorLabel(o.connector_id))}">${esc(_oltRegConnectorLabel(o.connector_id))}</td>
      <td>${o.has_password
            ? '<span class="badge badge-green">cadastrada</span>'
            : '<span class="badge badge-amber">sem senha</span>'}</td>
      <td class="olt-reg-status-cell">${o.active
            ? '<span class="badge badge-green">ativa</span>'
            : '<span class="badge badge-gray">inativa</span>'}</td>
      <td class="olt-reg-connection-cell">${_oltRegTestStatusHtml(o.id)}</td>
      <td class="olt-reg-row-actions-cell">
        <button type="button" class="olt-reg-action-trigger ${_oltRegSyncStates.get(Number(o.id))?.kind === 'loading' ? 'is-loading' : ''}" onclick="oltRegOpenMenu(event, ${Number(o.id)}, this)" title="Abrir acoes" aria-label="Abrir acoes" ${_oltRegSyncStates.get(Number(o.id))?.kind === 'loading' ? 'disabled' : ''}><i data-lucide="${_oltRegSyncStates.get(Number(o.id))?.kind === 'loading' ? 'loader-circle' : 'ellipsis'}"></i></button>
      </td>
    </tr>`).join('');
  lucide.createIcons();
}

function oltRegEdit(id) {
  const olt = _oltRegRows.find(o => Number(o.id) === Number(id));
  if (!olt) return;
  const set = (campo, valor) => { const el = document.getElementById(campo); if (el) el.value = valor ?? ''; };
  set('oltRegId', olt.id);
  set('oltRegName', olt.name);
  set('oltRegHost', olt.host);
  set('oltRegSite', olt.site);
  set('oltRegVendor', olt.vendor);
  _oltRegPopulateModels(olt.model);
  set('oltRegModel', olt.model);
  set('oltRegUsername', olt.username);
  set('oltRegPassword', '');          // nunca vem da API; vazio = manter
  set('oltRegConnector', olt.connector_id);
  set('oltRegNotes', olt.notes);
  const ativo = document.getElementById('oltRegActive');
  if (ativo) ativo.checked = !!olt.active;

  document.getElementById('oltRegFormTitle').textContent = `Editando: ${olt.name}`;
  document.getElementById('oltRegPassword').placeholder = olt.has_password
    ? 'deixe vazio para manter a atual'
    : 'sem senha cadastrada';
  document.getElementById('oltRegName')?.focus();
  oltRegOpenModal();
}

function oltRegOpenModal() {
  const modal = document.getElementById('modalOltRegistry');
  const body = document.getElementById('oltRegModalBody');
  const card = document.getElementById('oltRegFormCard');
  if (!modal || !body || !card) return;
  body.appendChild(card);
  modal.classList.remove('hidden');
  document.body.classList.add('modal-open');
  setTimeout(() => document.getElementById('oltRegName')?.focus(), 0);
  lucide.createIcons();
}

function oltRegCloseModal() {
  const modal = document.getElementById('modalOltRegistry');
  const parking = document.querySelector('#viewDeployOlt .connectors-layout');
  const card = document.getElementById('oltRegFormCard');
  modal?.classList.add('hidden');
  if (parking && card) parking.prepend(card);
  document.body.classList.remove('modal-open');
}

function oltRegNew() {
  oltRegResetForm();
  oltRegOpenModal();
}

function oltRegResetForm() {
  ['oltRegId', 'oltRegName', 'oltRegHost', 'oltRegSite', 'oltRegVendor', 'oltRegModel',
   'oltRegUsername', 'oltRegPassword', 'oltRegNotes'].forEach(campo => {
    const el = document.getElementById(campo);
    if (el) el.value = '';
  });
  const conector = document.getElementById('oltRegConnector');
  if (conector) conector.value = '';
  const ativo = document.getElementById('oltRegActive');
  if (ativo) ativo.checked = true;
  document.getElementById('oltRegFormTitle').textContent = 'Nova OLT';
  document.getElementById('oltRegPassword').placeholder = 'deixe vazio para manter a atual';
  _oltRegPopulateModels();
  _oltRegSetStatus('Preencha os dados e escolha Salvar ou Salvar e sincronizar. O teste de conexao fica nas acoes da OLT cadastrada.');
}

function _oltRegTestStatusHtml(id) {
  const state = _oltRegTestStates.get(Number(id));
  if (!state) return '<small class="olt-test-state muted">Nao testada</small>';
  if (state.kind === 'loading') return '<small class="olt-test-state loading"><i data-lucide="loader-circle"></i> Testando...</small>';
  const testedTitle = state.testedAt ? ` title="Ultimo teste: ${esc(new Date(state.testedAt).toLocaleString('pt-BR'))}"` : '';
  if (state.kind === 'ok') return `<small class="olt-test-state ok"${testedTitle}><i data-lucide="circle-check"></i> Conexao OK${state.detail ? ` - ${esc(state.detail)}` : ''}</small>`;
  return `<small class="olt-test-state error" title="${esc(state.detail || 'Falha na conexao')}"><i data-lucide="circle-x"></i> Falhou</small>`;
}

function oltRegCloseMenus() {
  document.getElementById('oltRegFloatingMenu')?.remove();
}

function oltRegOpenMenu(event, id, trigger) {
  event.preventDefault();
  event.stopPropagation();
  oltRegCloseMenus();
  const menu = document.createElement('div');
  menu.id = 'oltRegFloatingMenu';
  menu.className = 'olt-reg-floating-menu';
  menu.innerHTML = `
    <button type="button" data-action="test"><i data-lucide="plug-zap"></i><span>Testar conexao</span></button>
    <button type="button" data-action="sync"><i data-lucide="refresh-cw"></i><span>Sincronizar inventario</span></button>
    <button type="button" data-action="telemetry"><i data-lucide="activity"></i><span>Atualizar telemetria</span></button>
    <button type="button" data-action="edit"><i data-lucide="pencil"></i><span>Editar</span></button>
    <button type="button" class="danger" data-action="delete"><i data-lucide="trash-2"></i><span>Excluir</span></button>`;
  document.body.appendChild(menu);
  const rect = trigger.getBoundingClientRect();
  const menuWidth = 220;
  const menuHeight = 230;
  const left = Math.max(8, Math.min(window.innerWidth - menuWidth - 8, rect.right - menuWidth));
  const top = rect.bottom + menuHeight + 8 <= window.innerHeight
    ? rect.bottom + 6
    : Math.max(8, rect.top - menuHeight - 6);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.addEventListener('click', ev => {
    ev.stopPropagation();
    const action = ev.target.closest('button')?.dataset.action;
    oltRegCloseMenus();
    if (action === 'test') oltRegTest(id);
    if (action === 'sync') oltRegSync(id);
    if (action === 'telemetry') oltRegTelemetry(id);
    if (action === 'edit') oltRegEdit(id);
    if (action === 'delete') oltRegDelete(id);
  });
  lucide.createIcons();
}

function _oltRegPayload() {
  const val = campo => (document.getElementById(campo)?.value || '').trim();
  const id = val('oltRegId');
  const payload = {
    name: val('oltRegName'),
    host: val('oltRegHost'),
    vendor: val('oltRegVendor'),
    model: val('oltRegModel'),
    username: val('oltRegUsername'),
    password: document.getElementById('oltRegPassword')?.value || '',
    site: val('oltRegSite'),
    connector_id: val('oltRegConnector'),
    notes: val('oltRegNotes'),
    active: !!document.getElementById('oltRegActive')?.checked,
  };
  if (id) payload.id = Number(id);
  return payload;
}

function _oltRegSetStatus(message, isError = false, isSuccess = false) {
  const box = document.getElementById('oltRegStatus');
  if (!box) return;
  box.textContent = message;
  box.classList.toggle('error', !!isError);
  box.classList.toggle('success', !!isSuccess);
}

function _oltRegSyncProgress(olt, kind = 'loading', result = null) {
  if (kind !== 'loading' && _oltRegSyncPanelDismissed) return;
  let panel = document.getElementById('oltRegSyncProgress');
  if (!panel) {
    panel = document.createElement('aside');
    panel.id = 'oltRegSyncProgress';
    panel.className = 'olt-sync-progress';
    panel.setAttribute('role', 'status');
    panel.setAttribute('aria-live', 'polite');
    document.body.appendChild(panel);
  }
  clearInterval(_oltRegSyncTimer);
  const startedAt = Date.now();
  const title = esc(olt?.name || 'OLT');
  const host = esc(olt?.host || '');

  if (kind === 'loading') {
    _oltRegSyncPanelDismissed = false;
    const renderTime = () => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      const stage = seconds < 8 ? 'Conectando ao equipamento'
        : seconds < 35 ? 'Consultando PONs e ONUs'
        : 'Coletando MACs dos dispositivos';
      panel.innerHTML = `
        <button type="button" class="olt-sync-close" onclick="_oltRegCloseSyncProgress()" title="Fechar acompanhamento" aria-label="Fechar acompanhamento"><i data-lucide="x"></i></button>
        <div class="olt-sync-head"><span class="olt-sync-spinner"><i data-lucide="refresh-cw"></i></span><div><strong>Sincronizando inventario</strong><small>${title}${host ? ` - ${host}` : ''}</small></div></div>
        <div class="olt-sync-track"><span></span></div>
        <div class="olt-sync-stage"><span>${stage}</span><b>${seconds}s</b></div>
        <p>Essa coleta pode levar alguns minutos. Pode continuar nesta tela.</p>`;
      lucide.createIcons();
    };
    panel.className = 'olt-sync-progress is-loading';
    renderTime();
    _oltRegSyncTimer = setInterval(renderTime, 1000);
    return;
  }

  if (kind === 'success') {
    panel.className = 'olt-sync-progress is-success';
    panel.innerHTML = `
      <button type="button" class="olt-sync-close" onclick="_oltRegCloseSyncProgress()" title="Fechar" aria-label="Fechar"><i data-lucide="x"></i></button>
      <div class="olt-sync-head"><span class="olt-sync-result"><i data-lucide="circle-check"></i></span><div><strong>Inventario atualizado</strong><small>${title}</small></div></div>
      <p><b>${Number(result?.count || 0)}</b> itens lidos e <b>${Number(result?.count_all || 0)}</b> registros no inventario.</p>
      <button type="button" onclick="navigateTo('olt')">Abrir Inventario OLT</button>`;
  } else {
    panel.className = 'olt-sync-progress is-error';
    panel.innerHTML = `
      <button type="button" class="olt-sync-close" onclick="_oltRegCloseSyncProgress()" title="Fechar" aria-label="Fechar"><i data-lucide="x"></i></button>
      <div class="olt-sync-head"><span class="olt-sync-result"><i data-lucide="circle-x"></i></span><div><strong>Falha ao sincronizar</strong><small>${title}</small></div></div>
      <p>${esc(result?.message || String(result || 'Nao foi possivel concluir a coleta.'))}</p>
      <button type="button" onclick="this.closest('.olt-sync-progress').remove()">Fechar</button>`;
  }
  lucide.createIcons();
}

function _oltRegCloseSyncProgress() {
  _oltRegSyncPanelDismissed = true;
  clearInterval(_oltRegSyncTimer);
  _oltRegSyncTimer = null;
  document.getElementById('oltRegSyncProgress')?.remove();
}

function _oltRegSetSyncButtons(busy) {
  ['btnOltRegSave', 'btnOltRegSaveSync', 'btnOltRegCancel'].forEach(id => {
    const button = document.getElementById(id);
    if (button) button.disabled = !!busy;
  });
  const button = document.getElementById('btnOltRegSaveSync');
  if (!button) return;
  button.classList.toggle('is-loading', !!busy);
  button.innerHTML = busy
    ? '<i data-lucide="loader-circle"></i> Sincronizando...'
    : '<i data-lucide="refresh-cw"></i> Salvar e sincronizar';
  lucide.createIcons();
}

async function _oltRegAwaitSync(oltId, initial) {
  if (!initial?.accepted || initial?.status !== 'running') return initial;
  const started = Date.now();
  while (Date.now() - started < 30 * 60 * 1000) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    const state = await jsonOrReadableError(
      await api(`/api/olt/registry/${Number(oltId)}/sync-status`),
      'Nao foi possivel acompanhar a sincronizacao.'
    );
    if (state?.status === 'done') return state;
    if (state?.status === 'error') throw new Error(state?.error || 'A coleta da OLT falhou.');
    const elapsed = Number(state?.elapsed_s || Math.floor((Date.now() - started) / 1000));
    _oltRegSetStatus(`Sincronizando a OLT em segundo plano... ${elapsed}s. Pode manter esta tela aberta.`);
  }
  throw new Error('A sincronizacao continua em segundo plano, mas excedeu o tempo de acompanhamento da tela.');
}

async function _oltRegRequestSync(oltId) {
  const initial = await jsonOrReadableError(
    await api(`/api/olt/registry/${Number(oltId)}/sync`, { method: 'POST' }),
    'Nao foi possivel iniciar a sincronizacao da OLT.'
  );
  return _oltRegAwaitSync(oltId, initial);
}

async function _oltRegAwaitTelemetry(oltId, initial) {
  if (!initial?.accepted || initial?.status !== 'running') return initial;
  const started = Date.now();
  while (Date.now() - started < 30 * 60 * 1000) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    const state = await jsonOrReadableError(
      await api(`/api/olt/registry/${Number(oltId)}/telemetry-status`),
      'Nao foi possivel acompanhar a coleta de telemetria.'
    );
    if (state?.status === 'done') return state;
    if (state?.status === 'error') throw new Error(state?.error || 'A coleta de telemetria falhou.');
    const elapsed = Number(state?.elapsed_s || Math.floor((Date.now() - started) / 1000));
    _oltRegSetStatus(`Coletando telemetria em segundo plano... ${elapsed}s. Pode manter esta tela aberta.`);
  }
  throw new Error('A coleta de telemetria continua em segundo plano, mas excedeu o tempo de acompanhamento da tela.');
}

async function _oltRegRequestTelemetry(oltId) {
  const initial = await jsonOrReadableError(
    await api(`/api/olt/registry/${Number(oltId)}/telemetry`, { method: 'POST' }),
    'Nao foi possivel iniciar a coleta de telemetria.'
  );
  return _oltRegAwaitTelemetry(oltId, initial);
}

function _oltRegValidate(payload) {
  if (!payload.name) { showToast('Informe o nome da OLT.', true); return false; }
  if (!payload.host) { showToast('Informe o IP da OLT.', true); return false; }
  if (!payload.site) { showToast('Informe o site/local da OLT.', true); return false; }
  if (!payload.vendor) { showToast('Escolha o fabricante da OLT.', true); return false; }
  if (!payload.model) { showToast('Escolha o modelo da OLT.', true); return false; }
  return true;
}

async function _oltRegSave(syncAfter = false) {
  const payload = _oltRegPayload();
  if (!_oltRegValidate(payload)) return null;
  _oltRegSetStatus(syncAfter ? 'Salvando a OLT para iniciar a sincronizacao...' : 'Salvando a OLT...');
  try {
    const saved = await jsonOrReadableError(
      await api('/api/olt/registry', { method: 'POST', body: JSON.stringify(payload) }),
      'Nao foi possivel salvar a OLT.'
    );
    const olt = saved?.item || null;
    if (!olt?.id) throw new Error('A API nao devolveu a OLT salva.');
    if (syncAfter) {
      _oltRegSetSyncButtons(true);
      _oltRegSyncStates.set(Number(olt.id), { kind: 'loading' });
      _oltRegSyncProgress(olt, 'loading');
      _oltRegRender();
      _oltRegSetStatus('OLT salva. Consultando ONUs e dispositivos para atualizar o Inventario OLT...');
      const result = await _oltRegRequestSync(olt.id);
      _oltRegSetStatus(`Sincronizacao concluida: ${Number(result?.count || 0)} item(ns) lido(s), ${Number(result?.count_all || 0)} no inventario.`, false, true);
      _oltRegSyncStates.set(Number(olt.id), { kind: 'success' });
      _oltRegSyncProgress(olt, 'success', result);
      showToast('OLT salva e inventario sincronizado.');
    } else {
      _oltRegSetStatus('OLT salva. Use Sincronizar quando quiser atualizar o Inventario OLT.', false, true);
      showToast(payload.id ? 'OLT atualizada.' : 'OLT cadastrada.');
    }
    oltRegResetForm();
    oltRegCloseModal();
    await Promise.all([_oltRegLoad(), _oltRegLoadSites()]);
    return olt;
  } catch (err) {
    if (syncAfter) _oltRegSyncProgress(payload, 'error', err);
    _oltRegSetStatus(`Falha: ${err?.message || err}`, true);
    showToast(`Nao foi possivel concluir: ${err?.message || err}`, true);
    return null;
  } finally {
    if (syncAfter) {
      _oltRegSetSyncButtons(false);
      const id = Number(payload.id || 0);
      if (id) _oltRegSyncStates.delete(id);
    }
  }
}

async function oltRegSave() {
  return _oltRegSave(false);
}

async function oltRegSaveSync() {
  return _oltRegSave(true);
}

async function oltRegTest(id = 0) {
  let oltId = Number(id || document.getElementById('oltRegId')?.value || 0);
  if (!oltId) {
    showToast('Salve a OLT antes de testar a conexao.', true);
    _oltRegSetStatus('A OLT precisa estar salva antes do teste.', true);
    return;
  }
  _oltRegTestStates.set(oltId, { kind: 'loading' });
  _oltRegRender();
  _oltRegSetStatus('Testando acesso à OLT...');
  try {
    const result = await jsonOrReadableError(
      await api(`/api/olt/registry/${oltId}/test`, { method: 'POST' }),
      'Nao foi possivel testar a OLT.'
    );
    const detail = `${Number(result?.pons || 0)} PON(s)`;
    _oltRegTestStates.set(oltId, { kind: 'ok', detail });
    _oltRegRender();
    _oltRegSetStatus(`Conexao confirmada. ${Number(result?.pons || 0)} PON(s) consultada(s).`, false, true);
    showToast('Conexao com a OLT confirmada.');
  } catch (err) {
    _oltRegTestStates.set(oltId, { kind: 'error', detail: String(err?.message || err) });
    _oltRegRender();
    _oltRegSetStatus(`Falha no teste: ${err?.message || err}`, true);
    showToast(`Falha no teste: ${err?.message || err}`, true);
  }
}

async function oltRegSync(id) {
  const olt = _oltRegRows.find(item => Number(item.id) === Number(id));
  if (!olt || _oltRegSyncStates.get(Number(id))?.kind === 'loading') return;
  _oltRegSyncStates.set(Number(id), { kind: 'loading' });
  _oltRegSyncProgress(olt, 'loading');
  _oltRegRender();
  _oltRegSetStatus('Consultando a OLT e atualizando o inventario...');
  try {
    const result = await _oltRegRequestSync(id);
    _oltRegSetStatus(`Sincronizacao concluida: ${Number(result?.count || 0)} item(ns) lido(s), ${Number(result?.count_all || 0)} no inventario.`, false, true);
    _oltRegSyncProgress(olt, 'success', result);
    showToast('Inventario OLT atualizado.');
  } catch (err) {
    _oltRegSyncProgress(olt, 'error', err);
    _oltRegSetStatus(`Falha na sincronizacao: ${err?.message || err}`, true);
    showToast(`Falha na sincronizacao: ${err?.message || err}`, true);
  } finally {
    _oltRegSyncStates.delete(Number(id));
    _oltRegRender();
  }
}

async function oltRegTelemetry(id) {
  const olt = _oltRegRows.find(item => Number(item.id) === Number(id));
  if (!olt || _oltRegSyncStates.get(Number(id))?.kind === 'loading') return;
  _oltRegSyncStates.set(Number(id), { kind: 'loading' });
  _oltRegRender();
  _oltRegSetStatus('Consultando sinal/status de todas as ONUs da OLT...');
  try {
    const result = await _oltRegRequestTelemetry(id);
    _oltRegSetStatus(`Telemetria atualizada: ${Number(result?.onus || 0)} ONU(s) lida(s), ${Number(result?.with_signal || 0)} com sinal.`, false, true);
    showToast('Telemetria da OLT atualizada.');
  } catch (err) {
    _oltRegSetStatus(`Falha ao coletar telemetria: ${err?.message || err}`, true);
    showToast(`Falha ao coletar telemetria: ${err?.message || err}`, true);
  } finally {
    _oltRegSyncStates.delete(Number(id));
    _oltRegRender();
  }
}

async function oltRegDelete(id) {
  const olt = _oltRegRows.find(o => Number(o.id) === Number(id));
  if (!olt) return;
  const ok = await showConfirm({
    eyebrow: 'Remover OLT',
    title: `${olt.name} (${olt.host})`,
    msg: 'As ONUs ja coletadas dela continuam no inventario. Apenas o cadastro sai.',
    label: 'Remover',
    danger: true,
  });
  if (!ok) return;
  try {
    await jsonOrReadableError(
      await api(`/api/olt/registry/${Number(id)}`, { method: 'DELETE' }),
      'Nao foi possivel remover a OLT.'
    );
    showToast('OLT removida.');
    if (String(document.getElementById('oltRegId')?.value || '') === String(id)) oltRegResetForm();
    await _oltRegLoad();
  } catch (err) {
    showToast(`Nao foi possivel remover: ${err?.message || err}`, true);
  }
}

function bindDeployOlt() {
  document.getElementById('oltRegVendor')?.addEventListener('change', () => _oltRegPopulateModels());
  document.getElementById('btnOltRegSave')?.addEventListener('click', oltRegSave);
  document.getElementById('btnOltRegSaveSync')?.addEventListener('click', oltRegSaveSync);
  document.getElementById('btnOltRegNew')?.addEventListener('click', oltRegNew);
  document.getElementById('btnOltRegCancel')?.addEventListener('click', () => { oltRegCloseModal(); oltRegResetForm(); });
  document.getElementById('btnOltRegModalClose')?.addEventListener('click', oltRegCloseModal);
  document.getElementById('btnOltRegClear')?.addEventListener('click', () => { oltRegCloseModal(); oltRegResetForm(); });
  document.getElementById('oltRegSearch')?.addEventListener('input', _oltRegApplyFilter);
  if (!document.body.dataset.oltActionMenuBound) {
    document.body.dataset.oltActionMenuBound = '1';
    document.addEventListener('click', () => oltRegCloseMenus());
    window.addEventListener('resize', () => oltRegCloseMenus());
    window.addEventListener('scroll', () => oltRegCloseMenus(), true);
  }
}
