function uniqueCoveredCidrs(values) {
  const infos = values.map(parseCidrInfo).filter(Boolean);
  const seen = new Set();
  const unique = infos.filter(info => {
    if (seen.has(info.cidr)) return false;
    seen.add(info.cidr);
    return true;
  });
  return unique.filter(info => !unique.some(other =>
    other.cidr !== info.cidr &&
    other.prefix < info.prefix &&
    other.start <= info.start &&
    other.end >= info.end
  )).sort((a, b) => cidrSortValue(a.cidr) - cidrSortValue(b.cidr)).map(info => info.cidr);
}

function splitCidrValues(value) {
  if (Array.isArray(value)) return value.flatMap(item => splitCidrValues(item));
  if (typeof value !== 'string') return [];
  return value.split(/[\s,;|]+/).map(item => item.trim()).filter(Boolean);
}

function looksLikeWanInterface(value) {
  const name = String(value || '').trim().toLowerCase();
  return name === 'ether1'
    || name.includes('wan')
    || name.includes('internet')
    || name.includes('pppoe')
    || name.startsWith('lte');
}

function cidrSortValue(cidr) {
  const [ip, prefix = '0'] = String(cidr || '').split('/');
  const parts = ip.split('.').map(part => Number(part) || 0);
  return parts.reduce((acc, part) => (acc * 256) + part, 0) * 100 + (Number(prefix) || 0);
}

function connectorDetectedLans(row) {
  const inv = row?.inventory || {};
  const host = row?.host || {};
  const tunnel = row?.tunnel || {};
  const trusted = [];
  const addValue = value => {
    splitCidrValues(value).forEach(item => {
      const cidr = normalizePrivateCidr(item);
      if (cidr) trusted.push(cidr);
    });
  };
  addValue(host.lan_networks);
  addValue(tunnel.client_lans);
  const addressSample = String(inv.address_sample || inv.ip_address_sample || '');
  let trustedFromAddressSample = false;
  addressSample.split(/[;\n\r]+/).forEach(item => {
    const parts = String(item || '').split('|');
    const first = parts[0]?.trim();
    const iface = parts[1]?.trim();
    if (looksLikeWanInterface(iface)) return;
    const cidr = normalizePrivateCidr(first);
    if (cidr) {
      trusted.push(cidr);
      trustedFromAddressSample = true;
    }
  });
  if (!trustedFromAddressSample) ['lan_networks', 'networks', 'routes'].forEach(key => addValue(inv[key]));

  const trustedClean = uniqueCoveredCidrs(trusted);
  if (trustedClean.length) return trustedClean;

  const values = [];
  ['dhcp_sample', 'arp_sample', 'neighbor_sample'].forEach(key => {
    String(inv[key] || '').match(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g)?.forEach(ip => {
      const cidr = private24FromIp(ip);
      if (cidr) values.push(cidr);
    });
  });
  const seen = new Set();
  return values.map(v => String(v || '').trim()).filter(v => {
    if (!/^\d{1,3}(?:\.\d{1,3}){3}\/\d{1,2}$/.test(v)) return false;
    if (v === '10.250.0.0/24') return false;
    if (seen.has(v)) return false;
    seen.add(v);
    return true;
  }).sort((a, b) => cidrSortValue(a) - cidrSortValue(b));
}

async function loadConnectors(forceRefresh = false) {
  closeConnectorActionMenu();
  const data = await apiJson('/api/connectors', { forceRefresh });
  const rows = data?.connectors || [];
  _connectors = rows;
  const tbody = document.getElementById('connectorsTable');
  const summary = document.getElementById('connectorsSummary');
  if (summary) {
    const online = rows.filter(r => String(r.status).toLowerCase() === 'online').length;
    summary.textContent = `${rows.length} conector(es), ${online} online.`;
  }
  if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">Nenhum conector criado.</td></tr>';
  } else {
    tbody.innerHTML = rows.map(row => `
      <tr>
        <td class="connector-name-cell" title="${esc(row.name || row.id)}"><strong>${esc(row.name || row.id)}</strong></td>
        <td class="connector-text-cell" title="${esc(row.client || '-')}">${esc(row.client || '-')}</td>
        <td class="connector-text-cell" title="${esc(row.site || '-')}">${esc(row.site || '-')}</td>
        <td class="connector-status-cell">${statusBadge(row.status)}</td>
        <td class="connector-host-cell" title="${esc(`${connectorHostLabel(row.host)} ${connectorInventoryLabel(row) || ''}`.trim())}"><strong class="connector-host-name">${esc(connectorHostLabel(row.host))}</strong>${connectorInventoryLabel(row) ? `<span class="connector-host-stats">${esc(connectorInventoryLabel(row))}</span>` : ''}</td>
        <td class="connector-ip-cell monospace" title="${esc(row.remote_ip || '-')}">${esc(row.remote_ip || '-')}</td>
        <td class="connector-date-cell" title="${esc(formatDateTimeShort(row.last_seen))}">${esc(formatDateTimeShort(row.last_seen))}</td>
        <td class="connector-actions-cell">
          <button type="button" class="connector-action-trigger" data-conn-menu="${esc(row.id)}" title="Abrir acoes" aria-label="Abrir acoes"><i data-lucide="ellipsis"></i></button>
        </td>
      </tr>`).join('');
  }
  const sel = document.getElementById('connJobConnector');
  if (sel) {
    // "Tarefas remotas" (Enviar ping/Coletar LAN por fila de job) so vale
    // pro fluxo MikroTik -- o Ruijie fala direto (sem agente/fila), usa o
    // "Coletar LAN" do menu de acoes de cada linha, nao esse painel.
    const jobRows = rows.filter(row => String(row.type || 'routeros').toLowerCase() === 'routeros');
    sel.innerHTML = jobRows.map(row => `<option value="${esc(row.id)}">${esc(row.name || row.id)} - ${esc(row.site || '')}</option>`).join('');
  }
  lucide.createIcons();
}

function downloadConnectorAgent(connectorId) {
  if (!connectorId) return;
  const row = connectorById(connectorId);
  const fallbackType = String(connectorId) === String(_lastCreatedConnectorId) ? _lastCreatedConnectorType : '';
  const isRouter = String(row?.type || fallbackType).toLowerCase() === 'routeros';
  const publicUrl = document.getElementById('connPublicUrl')?.value.trim() || 'http://201.182.184.84:18080';
  const params = new URLSearchParams();
  if (_token) params.set('auth_token', _token);
  if (publicUrl) params.set('base_url', publicUrl.replace(/\/+$/, ''));
  const endpoint = isRouter ? 'routeros-script' : 'agent-script';
  const query = params.toString() ? `?${params.toString()}` : '';
  window.open(`${API_BASE}/api/connectors/${encodeURIComponent(connectorId)}/${endpoint}${query}`, '_blank');
}

async function downloadConnectorVpn(connectorId) {
  if (!connectorId) return;
  openConnectorVpnModal(connectorId, '201.182.184.84:51820');
}

function looksLikeValidOvpnConfig(text) {
  // O eWeb exporta um .tar (client.ovpn + ca.crt + ca.key) -- se alguem
  // escolher o .tar inteiro em vez do client.ovpn de dentro dele, o
  // FileReader le os bytes binarios "como texto" e produz um cabecalho de
  // tar (ustar, permissoes tipo "0000777", nome de arquivo com \0 no meio)
  // colado na frente da config real. Bug real ja visto em producao.
  if (/\bustar\b/.test(text)) return false;
  if (/[\x00-\x08\x0e-\x1f]/.test(text.slice(0, 512))) return false; // bytes de controle = binario
  return /^\s*(client|dev\s+tun|dev\s+tap|#|;)/m.test(text);
}

function loadVpnConfigFile(input, targetTextareaId, fileNameLabelId) {
  const file = input.files && input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const text = String(reader.result || '').trim();
    const label = fileNameLabelId ? document.getElementById(fileNameLabelId) : null;
    if (!looksLikeValidOvpnConfig(text)) {
      input.value = '';
      if (label) label.textContent = 'Nenhum arquivo escolhido';
      showToast(`"${file.name}" nao parece ser o client.ovpn (pode ser o .tar inteiro) -- extraia o .tar e escolha o arquivo client.ovpn de dentro dele.`, true);
      return;
    }
    const textarea = document.getElementById(targetTextareaId);
    if (textarea) textarea.value = text;
    if (label) label.textContent = `${file.name} carregado`;
  };
  reader.onerror = () => showToast('Nao consegui ler o arquivo selecionado.', true);
  reader.readAsText(file);
}

function updateConnectorTypeFields() {
  const type = document.getElementById('connType')?.value || 'routeros';
  const isRuijie = type === 'ruijie';
  document.getElementById('connFieldsRouteros')?.classList.toggle('hidden', isRuijie);
  document.getElementById('connFieldsRouterosAccess')?.classList.toggle('hidden', isRuijie);
  document.getElementById('connFieldsRuijieHost')?.classList.toggle('hidden', !isRuijie);
  document.getElementById('connFieldsRuijieUser')?.classList.toggle('hidden', !isRuijie);
  document.getElementById('connFieldsRuijiePass')?.classList.toggle('hidden', !isRuijie);
  document.getElementById('connFieldsRuijieVpnUser')?.classList.toggle('hidden', !isRuijie);
  document.getElementById('connFieldsRuijieVpnPass')?.classList.toggle('hidden', !isRuijie);
  document.getElementById('connFieldsRuijieVpnConfig')?.classList.toggle('hidden', !isRuijie);
}

function resetConnectorCreateForm() {
  ['connName', 'connClient', 'connSite', 'connGatewayHost', 'connGatewayPassword', 'connVpnUsername', 'connVpnPassword', 'connVpnConfig', 'connVpnConfigFile'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const vpnFileLabel = document.getElementById('connVpnConfigFileName');
  if (vpnFileLabel) vpnFileLabel.textContent = 'Nenhum arquivo escolhido';
  const type = document.getElementById('connType');
  if (type) type.value = 'routeros';
  const accessMode = document.getElementById('connAccessMode');
  if (accessMode) accessMode.value = 'cgnat';
  const gwUser = document.getElementById('connGatewayUser');
  if (gwUser) gwUser.value = 'admin';
  updateConnectorTypeFields();
  document.getElementById('connCreatedBox')?.classList.add('hidden');
}

function openConnectorCreateModal() {
  const modal = document.getElementById('modalConnectorCreate');
  const body = document.getElementById('connectorCreateModalBody');
  const card = document.getElementById('connectorCreateCard');
  if (!modal || !body || !card) return;
  resetConnectorCreateForm();
  body.appendChild(card);
  modal.classList.remove('hidden');
  document.body.classList.add('modal-open');
  setTimeout(() => document.getElementById('connName')?.focus(), 0);
  lucide.createIcons();
}

function closeConnectorCreateModal() {
  const modal = document.getElementById('modalConnectorCreate');
  const parking = document.querySelector('#viewConnectors .connectors-layout');
  const card = document.getElementById('connectorCreateCard');
  modal?.classList.add('hidden');
  if (parking && card) parking.prepend(card);
  document.body.classList.remove('modal-open');
}

function closeConnectorActionMenu() {
  document.getElementById('connectorFloatingMenu')?.remove();
}

function openConnectorActionMenu(event, connectorId, trigger) {
  event.preventDefault();
  event.stopPropagation();
  closeConnectorActionMenu();
  const isRuijie = String(connectorById(connectorId)?.type || '').toLowerCase() === 'ruijie';
  const menu = document.createElement('div');
  menu.id = 'connectorFloatingMenu';
  menu.className = 'connector-floating-menu';
  menu.innerHTML = isRuijie ? `
    <button type="button" data-action="ruijie-lan"><i data-lucide="scan-search"></i><span>Coletar LAN</span></button>
    <button type="button" data-action="ruijie-vpn"><i data-lucide="shield"></i><span>Configurar VPN</span></button>
    <button type="button" data-action="router-winbox"><i data-lucide="terminal"></i><span>Acessar via Winbox</span></button>
    <button type="button" data-action="pc-agent"><i data-lucide="monitor-down"></i><span>Baixar agente (PC)</span></button>
    <button type="button" class="danger" data-action="delete"><i data-lucide="trash-2"></i><span>Excluir</span></button>` : `
    <button type="button" data-action="download"><i data-lucide="download"></i><span>Baixar script</span></button>
    <button type="button" data-action="vpn"><i data-lucide="shield"></i><span>Configurar VPN</span></button>
    <button type="button" data-action="router-winbox"><i data-lucide="terminal"></i><span>Acessar via Winbox</span></button>
    <button type="button" data-action="pc-agent"><i data-lucide="monitor-down"></i><span>Baixar agente (PC)</span></button>
    <button type="button" class="danger" data-action="delete"><i data-lucide="trash-2"></i><span>Excluir</span></button>`;
  document.body.appendChild(menu);
  const rect = trigger.getBoundingClientRect();
  const menuWidth = 210;
  const menuHeight = 220;
  menu.style.left = `${Math.max(8, Math.min(window.innerWidth - menuWidth - 8, rect.right - menuWidth))}px`;
  menu.style.top = `${rect.bottom + menuHeight + 8 <= window.innerHeight ? rect.bottom + 6 : Math.max(8, rect.top - menuHeight - 6)}px`;
  menu.addEventListener('click', ev => {
    ev.stopPropagation();
    const action = ev.target.closest('button')?.dataset.action;
    closeConnectorActionMenu();
    if (action === 'download') downloadConnectorAgent(connectorId);
    if (action === 'vpn') downloadConnectorVpn(connectorId);
    if (action === 'ruijie-lan') collectRuijieLanInventory(connectorId);
    if (action === 'ruijie-vpn') openRuijieVpnModal(connectorId);
    if (action === 'router-winbox') openConnectorWinbox(connectorId);
    if (action === 'pc-agent') downloadPcAgent();
    if (action === 'delete') deleteConnector(connectorId);
  });
  lucide.createIcons();
}

// IP do proprio MikroTik do conector: o endereco dele no tunel WireGuard
// (tunnel.client_address, ex. 10.201.0.17/31). A API autoriza esse IP pro
// tenant dono do conector e o alcanca pelo IP virtual (vnat).
function connectorRouterIp(connectorId) {
  const row = connectorById(connectorId) || {};
  const addr = String(row?.tunnel?.client_address || '').trim();
  return addr.split('/')[0].trim();
}

// A porta do Winbox varia por roteador (o padrao 8291 quase sempre e trocado),
// entao ela e perguntada uma vez por conector e fica lembrada neste navegador.
const WINBOX_PORT_KEY = 'sightops.winboxPort';

function winboxPortMap() {
  try { return JSON.parse(localStorage.getItem(WINBOX_PORT_KEY) || '{}') || {}; }
  catch (err) { return {}; }
}

// Ordem: o que o proprio MikroTik reportou no heartbeat (agente 0.7+), senao
// o que foi escolhido a mao neste navegador, senao o padrao de fabrica.
function connectorWinboxPortFromRouter(connectorId) {
  const inv = connectorById(connectorId)?.inventory || {};
  const desabilitado = String(inv.winbox_disabled || '').trim().toLowerCase() === 'true';
  const porta = String(inv.winbox_port || '').trim();
  return (porta && !desabilitado) ? porta : '';
}

function connectorWinboxPort(connectorId) {
  return connectorWinboxPortFromRouter(connectorId)
    || String(winboxPortMap()[connectorId] || '').trim()
    || '8291';
}

function rememberWinboxPort(connectorId, porta) {
  try {
    const mapa = winboxPortMap();
    mapa[connectorId] = String(porta);
    localStorage.setItem(WINBOX_PORT_KEY, JSON.stringify(mapa));
  } catch (err) { /* navegador sem storage: segue sem lembrar */ }
}

// Modal no padrao da pagina (o prompt do navegador destoava da UI).
// Resolve com a porta escolhida, ou com 0 se cancelar.
function askWinboxPort(nome, padrao) {
  return new Promise(resolve => {
    const back = document.getElementById('modalWinboxPort');
    const input = document.getElementById('winboxPortInput');
    const quem = document.getElementById('winboxPortWho');
    const btnOk = document.getElementById('btnWinboxPortOk');
    const btnCancel = document.getElementById('btnWinboxPortCancel');
    const btnClose = document.getElementById('btnWinboxPortClose');
    if (!back || !input || !btnOk) { resolve(Number(padrao) || 0); return; }

    if (quem) quem.textContent = nome;
    input.value = padrao || '8291';
    back.classList.remove('hidden');
    lucide.createIcons();
    setTimeout(() => { input.focus(); input.select(); }, 30);

    const fechar = (valor) => {
      back.classList.add('hidden');
      btnOk.removeEventListener('click', onOk);
      btnCancel?.removeEventListener('click', onCancel);
      btnClose?.removeEventListener('click', onCancel);
      back.removeEventListener('mousedown', onFundo);
      document.removeEventListener('keydown', onTecla);
      resolve(valor);
    };
    const onOk = () => {
      const n = Number(String(input.value || '').trim());
      if (!Number.isInteger(n) || n < 1 || n > 65535) {
        showToast('Porta invalida. Use um numero de 1 a 65535.', true);
        input.focus();
        return;
      }
      fechar(n);
    };
    const onCancel = () => fechar(0);
    const onFundo = (ev) => { if (ev.target === back) fechar(0); };
    const onTecla = (ev) => {
      if (ev.key === 'Escape') fechar(0);
      if (ev.key === 'Enter' && !back.classList.contains('hidden')) { ev.preventDefault(); onOk(); }
    };
    btnOk.addEventListener('click', onOk);
    btnCancel?.addEventListener('click', onCancel);
    btnClose?.addEventListener('click', onCancel);
    back.addEventListener('mousedown', onFundo);
    document.addEventListener('keydown', onTecla);
  });
}

// Winbox nao e web: abre o tunel ate a porta do Winbox e entrega o endereco
// local pra colar (o mesmo 127.0.0.1:porta que antes exigia um ssh -L na mao).
async function openConnectorWinbox(connectorId) {
  const ip = connectorRouterIp(connectorId);
  if (!ip) {
    showToast('Este conector nao tem IP de tunel (VPN) configurado.', true);
    return;
  }
  const nome = connectorById(connectorId)?.name || 'este conector';
  // Se o roteador ja informou a porta no heartbeat, nao pergunta nada.
  const doRouter = connectorWinboxPortFromRouter(connectorId);
  let porta = Number(doRouter);
  if (!doRouter) {
    porta = await askWinboxPort(nome, connectorWinboxPort(connectorId));
    if (!porta) return;
    rememberWinboxPort(connectorId, porta);
  }
  let aberto = null;
  try {
    aberto = await deviceTunnelOpen(ip, porta);
  } catch (err) {
    aberto = null;
  }
  if (!aberto) {
    showToast('Agente do PC nao respondeu. Baixe e execute o agente (menu do conector).', true);
    return;
  }
  const endereco = `127.0.0.1:${aberto.port}`;
  try { await navigator.clipboard.writeText(endereco); } catch (err) { /* sem clipboard: so mostra */ }
  showToast(`Winbox: conecte em ${endereco} (copiado) -- porta ${porta} do roteador`);
}

function downloadPcAgent() {
  // Agente de acesso web direto (.exe Windows), servido estatico pelo frontend
  // em <base>/downloads/. Instalado uma vez, o botao Web abre a UI do
  // dispositivo direto pelo tunel (sem SSH/proxy). Ver agent/README.md.
  const a = document.createElement('a');
  a.href = 'downloads/sightops-agent.exe';
  a.download = 'sightops-agent.exe';
  document.body.appendChild(a);
  a.click();
  a.remove();
  if (typeof showToast === 'function') {
    showToast('Baixando o SightOps Agent. Instale/execute uma vez; depois o botao Web abre os dispositivos direto.', false);
  }
}

function openConnectorVpnModal(connectorId, endpointDefault = '201.182.184.84:51820') {
  const modal = document.getElementById('modalConnectorVpn');
  if (!modal) {
    prepareConnectorVpn(connectorId, endpointDefault, '__auto__', 'auto');
    return;
  }
  const row = connectorById(connectorId);
  const detected = connectorDetectedLans(row);
  document.getElementById('connectorVpnId').value = connectorId;
  document.getElementById('connectorVpnEndpoint').value = endpointDefault;
  document.getElementById('connectorVpnLans').value = detected.join(', ');
  const autoRadio = document.querySelector('input[name="connectorVpnLanMode"][value="auto"]');
  const manualRadio = document.querySelector('input[name="connectorVpnLanMode"][value="manual"]');
  if (autoRadio) autoRadio.disabled = !detected.length;
  (detected.length ? autoRadio : manualRadio)?.click();
  const preview = document.getElementById('connectorVpnDetectedPreview');
  if (preview) {
    preview.textContent = detected.length
      ? `Redes detectadas pelo MikroTik: ${detected.join(', ')}.`
      : 'Nenhuma rede com mascara confiavel foi detectada. Atualize/reinstale o script RouterOS 0.6 ou informe uma rede manualmente.';
  }
  updateConnectorVpnLanMode();
  modal.classList.remove('hidden');
  lucide.createIcons();
  setTimeout(() => document.getElementById('connectorVpnEndpoint')?.focus(), 50);
}

function closeConnectorVpnModal() {
  document.getElementById('modalConnectorVpn')?.classList.add('hidden');
}

function updateConnectorVpnLanMode() {
  const mode = document.querySelector('input[name="connectorVpnLanMode"]:checked')?.value || 'auto';
  const manual = mode === 'manual';
  document.getElementById('connectorVpnLansGroup')?.classList.toggle('hidden', !manual);
  document.getElementById('connectorVpnAutoHelp')?.classList.toggle('hidden', manual);
  const autoRadio = document.querySelector('input[name="connectorVpnLanMode"][value="auto"]');
  const autoCard = autoRadio?.closest('label');
  if (autoCard) {
    autoCard.style.opacity = autoRadio?.disabled ? '0.55' : '1';
    autoCard.style.cursor = autoRadio?.disabled ? 'not-allowed' : 'pointer';
  }
}

async function submitConnectorVpnModal() {
  const connectorId = document.getElementById('connectorVpnId')?.value || '';
  const endpoint = document.getElementById('connectorVpnEndpoint')?.value.trim() || '';
  const lanMode = document.querySelector('input[name="connectorVpnLanMode"]:checked')?.value || 'auto';
  const clientLans = lanMode === 'auto' ? '__auto__' : (document.getElementById('connectorVpnLans')?.value.trim() || '');
  if (!connectorId) return;
  if (!endpoint) {
    showToast('Informe o endpoint publico do WireGuard.', true);
    document.getElementById('connectorVpnEndpoint')?.focus();
    return;
  }
  if (lanMode === 'manual' && !clientLans) {
    showToast('Informe pelo menos uma rede LAN do cliente.', true);
    document.getElementById('connectorVpnLans')?.focus();
    return;
  }
  await prepareConnectorVpn(connectorId, endpoint, clientLans, lanMode);
}

async function prepareConnectorVpn(connectorId, endpoint, clientLans, lanMode = 'manual') {
  const btn = document.getElementById('confirmConnectorVpnModal');
  const oldHtml = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle"></i> Preparando';
    lucide.createIcons();
  }
  const res = await api(`/api/connectors/${encodeURIComponent(connectorId)}/wireguard`, {
    method: 'POST',
    body: JSON.stringify({ endpoint, client_lans: clientLans, lan_mode: lanMode }),
  });
  if (btn) {
    btn.disabled = false;
    btn.innerHTML = oldHtml || '<i data-lucide="shield"></i> Preparar VPN';
    lucide.createIcons();
  }
  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || 'Erro ao preparar VPN.', true);
    return;
  }
  const params = new URLSearchParams();
  if (_token) params.set('auth_token', _token);
  const query = params.toString() ? `?${params.toString()}` : '';
  showToast('VPN preparada. Baixando script RouterOS.');
  closeConnectorVpnModal();
  window.open(`${API_BASE}/api/connectors/${encodeURIComponent(connectorId)}/wireguard-routeros-script${query}`, '_blank');
  await loadConnectors();
}

async function createConnectorFromForm() {
  const type = document.getElementById('connType')?.value || 'routeros';
  const payload = {
    type,
    name: document.getElementById('connName')?.value.trim() || '',
    client: document.getElementById('connClient')?.value.trim() || '',
    site: document.getElementById('connSite')?.value.trim() || '',
  };
  if (type === 'ruijie') {
    payload.gateway_host = document.getElementById('connGatewayHost')?.value.trim() || '';
    payload.gateway_user = document.getElementById('connGatewayUser')?.value.trim() || 'admin';
    payload.gateway_password = document.getElementById('connGatewayPassword')?.value || '';
    if (!payload.gateway_host || !payload.gateway_password) {
      showToast('Informe o IP/host e a senha do gateway Ruijie.', true);
      return;
    }
    const vpnUser = document.getElementById('connVpnUsername')?.value.trim() || '';
    const vpnPass = document.getElementById('connVpnPassword')?.value || '';
    const vpnConfig = document.getElementById('connVpnConfig')?.value.trim() || '';
    if (vpnUser || vpnPass || vpnConfig) {
      if (!vpnUser || !vpnPass || !vpnConfig) {
        showToast('Pra salvar a VPN junto, informe usuario, senha e a configuracao (.ovpn).', true);
        return;
      }
      payload.vpn_username = vpnUser;
      payload.vpn_password = vpnPass;
      payload.vpn_config = vpnConfig;
    }
  } else {
    payload.public_base_url = document.getElementById('connPublicUrl')?.value.trim() || '';
    payload.access_mode = document.getElementById('connAccessMode')?.value || 'cgnat';
  }
  const btn = document.getElementById('btnCreateConnector');
  if (btn) { btn.disabled = true; btn.textContent = 'Criando'; }
  const res = await api('/api/connectors', { method: 'POST', body: JSON.stringify(payload) });
  const body = await res?.json().catch(() => ({}));
  if (btn) {
    btn.disabled = false;
    btn.innerHTML = '<i data-lucide="plus"></i> Criar conector';
    lucide.createIcons();
  }
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || 'Erro ao criar conector.', true);
    return;
  }
  const conn = body.connector || {};
  _lastCreatedConnectorId = conn.id || '';
  _lastCreatedConnectorType = conn.type || payload.type || '';
  document.getElementById('connCreatedBox')?.classList.remove('hidden');
  const txt = document.getElementById('connCreatedText');
  const typeText = type === 'ruijie'
    ? 'O SightOps ja fala direto com o gateway -- use "Coletar LAN" na lista de conectores.'
    : (payload.access_mode === 'public'
      ? 'Baixe o script e cole no MikroTik com IP publico.'
      : 'Baixe o script e cole no MikroTik; ele abre a ponte CGNAT automaticamente.');
  if (txt) txt.textContent = `${conn.name || conn.id} criado. ${typeText}`;
  document.getElementById('btnDownloadCreatedAgent')?.classList.toggle('hidden', type === 'ruijie');
  ['connName', 'connClient', 'connSite', 'connGatewayHost', 'connGatewayPassword', 'connVpnUsername', 'connVpnPassword', 'connVpnConfig'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  showToast('Conector criado.');
  await loadConnectors();
}

function openRuijieVpnModal(connectorId) {
  const modal = document.getElementById('modalRuijieVpn');
  if (!modal) return;
  const row = connectorById(connectorId);
  document.getElementById('ruijieVpnUsername').value = row?.vpn_username || '';
  document.getElementById('ruijieVpnPassword').value = '';
  document.getElementById('ruijieVpnConfig').value = row?.vpn_config || '';
  const fileInput = document.getElementById('ruijieVpnConfigFile');
  if (fileInput) fileInput.value = '';
  const fileLabel = document.getElementById('ruijieVpnConfigFileName');
  if (fileLabel) fileLabel.textContent = row?.vpn_config ? 'Configuracao ja salva (escolha outro arquivo pra trocar)' : 'Nenhum arquivo escolhido';
  modal.dataset.connectorId = connectorId;
  modal.classList.remove('hidden');
  lucide.createIcons();
}

function closeRuijieVpnModal() {
  document.getElementById('modalRuijieVpn')?.classList.add('hidden');
}

async function submitRuijieVpnModal() {
  const modal = document.getElementById('modalRuijieVpn');
  const connectorId = modal?.dataset.connectorId || '';
  const vpn_username = document.getElementById('ruijieVpnUsername')?.value.trim() || '';
  const vpn_password = document.getElementById('ruijieVpnPassword')?.value || '';
  const vpn_config = document.getElementById('ruijieVpnConfig')?.value.trim() || '';
  if (!connectorId) return;
  if (!vpn_username || !vpn_password || !vpn_config) {
    showToast('Informe usuario, senha e a configuracao (.ovpn) da VPN.', true);
    return;
  }
  const res = await api(`/api/connectors/${encodeURIComponent(connectorId)}/ruijie/vpn`, {
    method: 'POST',
    body: JSON.stringify({ vpn_username, vpn_password, vpn_config }),
  });
  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || 'Erro ao salvar VPN.', true);
    return;
  }
  showToast('VPN salva no conector.');
  closeRuijieVpnModal();
  await loadConnectors();
}

async function collectRuijieLanInventory(connectorId) {
  if (!connectorId) return;
  showToast('Consultando o gateway Ruijie...');
  const res = await api(`/api/connectors/${encodeURIComponent(connectorId)}/ruijie/lan-inventory`, { method: 'POST' });
  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || 'Erro ao coletar inventario do gateway Ruijie.', true);
    return;
  }
  showToast(`Inventario coletado: ${body.count || 0} dispositivo(s) na LAN.`);
  await loadConnectors();
}

async function deleteConnector(connectorId) {
  if (!connectorId) return;
  if (!await showConfirm({ title: 'Apagar conector', msg: 'Apagar este conector e seus jobs?', label: 'Apagar' })) return;
  const res = await api(`/api/connectors/${encodeURIComponent(connectorId)}`, { method: 'DELETE' });
  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || 'Erro ao apagar conector.', true);
    return;
  }
  showToast('Conector apagado.');
  await loadConnectors();
}

async function sendConnectorPingJob() {
  const connectorId = document.getElementById('connJobConnector')?.value || '';
  const raw = document.getElementById('connJobTargets')?.value.trim() || '';
  const targets = raw.split(/[\s,;]+/).map(x => x.trim()).filter(Boolean);
  if (!connectorId) { showToast('Crie ou selecione um conector.', true); return; }
  if (!targets.length) { showToast('Informe ao menos um IP ou host.', true); return; }
  const res = await api('/api/connectors/jobs', {
    method: 'POST',
    body: JSON.stringify({ connector_id: connectorId, type: 'ping_many', payload: { targets } }),
  });
  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || 'Erro ao criar job.', true);
    return;
  }
  showToast('Job enviado. O conector executa no proximo ciclo.');
  await loadConnectorJobs(connectorId);
}

async function sendConnectorLanInventoryJob() {
  const connectorId = document.getElementById('connJobConnector')?.value || '';
  if (!connectorId) { showToast('Crie ou selecione um conector.', true); return; }
  const res = await api('/api/connectors/jobs', {
    method: 'POST',
    body: JSON.stringify({ connector_id: connectorId, type: 'lan_inventory', payload: {} }),
  });
  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    showToast(body?.detail || 'Erro ao criar job.', true);
    return;
  }
  showToast('Coleta LAN enviada. O MikroTik executa no proximo ciclo.');
  await loadConnectorJobs(connectorId);
}

async function loadConnectorJobs(connectorId) {
  const log = document.getElementById('connectorJobsLog');
  if (!log || !connectorId) return;
  const data = await apiJson(`/api/connectors/${encodeURIComponent(connectorId)}/jobs`);
  const jobs = data?.jobs || [];
  if (!jobs.length) {
    log.textContent = 'Nenhum job para este conector.';
    return;
  }
  log.innerHTML = jobs.slice(0, 12).map(job => {
    const items = job?.result?.items || job?.result?.result?.items || [];
    const routerosPing = job?.result?.routeros_ping || job?.result?.result?.routeros_ping || '';
    const inventory = job?.result?.inventory || job?.result?.result?.inventory || null;
    const inventoryText = inventory
      ? [
          `DHCP: ${inventory.dhcp_leases ?? 0}`,
          `ARP: ${inventory.arp_entries ?? 0}`,
          `Vizinhos: ${inventory.neighbors ?? 0}`,
        ].join('<br>')
      : '';
    let resultText = '';
    if (routerosPing) {
      resultText = routerosPing.split(/[;,]/).filter(Boolean).map(item => {
          const separator = item.includes('=') ? '=' : ':';
          const [target, ok] = item.split(separator);
          const normalized = String(ok || '').toLowerCase();
          return `${normalized === 'true' || normalized === '1' ? 'OK' : 'FAIL'} ${esc(target || item)}`;
        }).join('<br>');
    } else if (Array.isArray(items) && items.length) {
      resultText = items.map(item => `${item.online ? 'OK' : 'FAIL'} ${item.target} ${item.rtt_ms ? item.rtt_ms + 'ms' : ''}`).join('<br>');
    } else if (inventoryText) {
      resultText = inventoryText;
    } else {
      resultText = esc(job.error || '');
    }
    return `<div class="connector-job-item">
      <div><strong>${esc(job.type)}</strong> <span class="badge ${job.status === 'done' ? 'badge-green' : job.status === 'failed' ? 'badge-red' : 'badge-amber'}">${esc(job.status)}</span></div>
      <div class="text-muted">${esc(formatDateTimeShort(job.created_at))}</div>
      <div class="monospace">${resultText || 'Aguardando MikroTik.'}</div>
    </div>`;
  }).join('');
}

// Modal ImgBB settings
async function openImgbbModal() {
  const data = await apiJson('/api/settings/imgbb');
  document.getElementById('imgbbApiKey').value = data?.api_key || data?.key || '';
  document.getElementById('imgbbTestResult').style.display = 'none';
  document.getElementById('imgbbErro').hidden = true;
  document.getElementById('modalImgbb').classList.remove('hidden');
  lucide.createIcons();
}

// Modal editar cameras (multiplas)
function openEditCamModal(cams, opts = {}) {
  const count = cams.length;
  document.getElementById('modalEditCamTitle').textContent =
    count === 1 ? `Editar  ${cams[0].ip}` : `Editar ${count} cameras`;
  document.getElementById('editCamErro').hidden = true;
  const applyDevice = document.getElementById('editCamApplyDevice');
  const deviceUser = document.getElementById('editCamDeviceUser');
  const devicePass = document.getElementById('editCamDevicePass');
  if (applyDevice) applyDevice.checked = !!opts.renameDevice;
  if (deviceUser) deviceUser.value = deviceUser.value || 'admin';
  if (devicePass) devicePass.value = '';
  if (devicePass && opts.renameDevice) setTimeout(() => devicePass.focus(), 80);

  const s = 'width:100%;padding:4px 6px;border:1px solid var(--border);border-radius:5px;font-size:12px;font-family:inherit;background:var(--surface);color:var(--text);outline:none;box-sizing:border-box';

  // As colunas extras dependem de onde a camera foi descoberta -- PON/ONU so
  // fazem sentido pra quem veio de OLT; quem veio de Switch precisa ver/editar
  // switch/porta/VLAN, nao campos de OLT vazios e sem uso.
  const mode = _invOltView || 'olt';
  const extraCols = mode === 'switch'
    ? [
        { label: 'Switch',    field: 'switch_name', width: '12%', placeholder: 'Nome do switch' },
        { label: 'Switch IP', field: 'switch_ip',    width: '11%', placeholder: '', mono: true },
        { label: 'Porta',     field: 'switch_port',  width: '6%',  placeholder: '', center: true },
        { label: 'VLAN',      field: 'switch_vlan',  width: '5%',  placeholder: '', center: true },
      ]
    : [
        { label: 'PON',        field: 'pon',        width: '5%',  placeholder: '', center: true },
        { label: 'ONU ID',     field: 'onu_id',     width: '6%',  placeholder: '', center: true },
        { label: 'ONU Name',   field: 'onu_name',   width: '12%', placeholder: 'gpon x onu y' },
        { label: 'ONU Serial', field: 'onu_serial', width: '11%', placeholder: 'ONU Serial', mono: true },
      ];

  const theadRow = document.querySelector('#editCamTable thead tr');
  const colgroup = document.querySelector('#editCamTable colgroup');
  if (theadRow) {
    theadRow.innerHTML = '<th>IP</th><th>Titulo</th><th>Fabricante</th><th>Modelo</th><th>Local</th><th>MAC</th>' +
      extraCols.map(c => `<th>${esc(c.label)}</th>`).join('');
  }
  if (colgroup) {
    colgroup.innerHTML = '<col style="width:9%"><col style="width:16%"><col style="width:9%"><col style="width:10%"><col style="width:9%"><col style="width:13%">' +
      extraCols.map(c => `<col style="width:${c.width}">`).join('');
  }

  document.getElementById('editCamTableBody').innerHTML = cams.map(c => `
    <tr data-key="${esc(_camKey(c))}" data-connector-id="${esc(c.remote_connector_id || c.connector_id || '')}" data-site="${esc(c.site || c.site_name || c.local || '')}" data-remote="${c.remote ? '1' : ''}">
      <td class="monospace" style="font-size:11px;color:var(--muted);white-space:nowrap">${esc(c.ip)}</td>
      <td><input data-ip="${esc(c.ip)}" data-field="titulo"     style="${s}" value="${esc(c.titulo    || '')}" placeholder="Titulo"></td>
      <td><input data-ip="${esc(c.ip)}" data-field="fabricante" style="${s}" value="${esc(c.fabricante|| '')}" placeholder="Fabricante"></td>
      <td><input data-ip="${esc(c.ip)}" data-field="model"      style="${s}" value="${esc(c.modelo || c.model || '')}" placeholder="Modelo"></td>
      <td><input data-ip="${esc(c.ip)}" data-field="local"      style="${s}" value="${esc(c.local     || '')}" placeholder="Local"></td>
      <td><input data-ip="${esc(c.ip)}" data-field="mac"        style="${s};font-family:monospace" value="${esc(c.mac       || '')}" placeholder="MAC"></td>
      ${extraCols.map(col => `<td><input data-ip="${esc(c.ip)}" data-field="${col.field}" style="${s}${col.center ? ';text-align:center' : ''}${col.mono ? ';font-family:monospace' : ''}" value="${esc(c[col.field] || '')}" placeholder="${esc(col.placeholder)}"></td>`).join('')}
    </tr>`).join('');

  document.getElementById('modalEditCam').classList.remove('hidden');
  lucide.createIcons();
}

function closeEditCamModal() {
  document.getElementById('modalEditCam').classList.add('hidden');
}

function applyCamPayloadsLocally(payloads) {
  ['basico', 'olt', 'switch'].forEach(mode => {
    if (!_invCam[mode]?.length) return;
    _invCam[mode] = _invCam[mode].map(cam => {
      const patch = payloads.find(p => _camKey(p) === _camKey(cam) || (!p.remote_connector_id && !p.connector_id && p.ip === cam.ip));
      return patch ? { ...cam, ...patch } : cam;
    });
    _camSessionSave(mode, _invCam[mode]);
  });
}

function applyCamStatusesLocally(statusByIp) {
  const patches = Object.entries(statusByIp || {})
    .filter(([ip, status]) => ip && status)
    .map(([ip, status]) => ({ ip, status }));
  if (patches.length) applyCamPayloadsLocally(patches);
  ['nvr', 'dvr'].forEach(type => {
    const store = type === 'dvr' ? _invDvr : _invNvr;
    ['basico', 'olt', 'switch'].forEach(mode => {
      if (!store?.[mode]?.length) return;
      store[mode] = store[mode].map(row => {
        const ip = row.camera_ip || row.ip_camera || row.host || '';
        const status = statusByIp?.[ip];
        return status ? { ...row, status } : row;
      });
      _recSessionSave(type, mode, store[mode]);
    });
  });
}

async function refreshCamSnapshotsAfterRename(payloads, user, pass, mode) {
  const patches = [];
  const failed = [];
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  await wait(1200);
  for (const cam of payloads || []) {
    const ip = String(cam?.ip || '').trim();
    if (!ip) continue;
    try {
      const res = await api('/api/cameras/snapshot/capture', {
        method: 'POST',
        body: JSON.stringify({ ip, user, password: pass, mode, remote_connector_id: cam.remote_connector_id || cam.connector_id || '' }),
      });
      const data = await res?.json().catch(() => ({}));
      if (!res?.ok || data?.ok === false || !data?.url) {
        failed.push({ ip, error: data?.detail || data?.error || 'falha ao capturar snapshot' });
        continue;
      }
      patches.push({ ip, snapshot_url: data.url, snapshot_path: data.filename || '' });
      if (_invOltActive && String(_invOltActive.ip || '') === ip) {
        _invOltActive.snapshot_url = data.url;
        const title = String(cam.titulo || cam.title || _invOltActive.titulo || ip);
        _invOltActive.titulo = title;
        const img = document.getElementById('cpSnapshot');
        const empty = document.getElementById('cpSnapshotEmpty');
        if (img) {
          img.src = `${API_BASE}${data.url}?t=${Date.now()}`;
          img.style.display = 'block';
        }
        if (empty) empty.style.display = 'none';
        setText('cpSnapshotTitle', title);
        setText('cpSnapshotTime', '');
      }
    } catch (e) {
      failed.push({ ip, error: e?.message || 'falha ao capturar snapshot' });
    }
  }
  return { patches, failed };
}

async function saveEditCam() {
  const rows = document.querySelectorAll('#editCamTableBody tr');
  const payloads = [];

  rows.forEach(tr => {
    const inputs = tr.querySelectorAll('input[data-ip]');
    if (!inputs.length) return;
    const ip = inputs[0].dataset.ip;
    const payload = {
      ip,
      inventory_key: tr.dataset.key || '',
      key: tr.dataset.key || '',
      remote_connector_id: tr.dataset.connectorId || '',
      connector_id: tr.dataset.connectorId || '',
      site: tr.dataset.site || '',
      site_name: tr.dataset.site || '',
      remote: tr.dataset.remote === '1',
    };
    inputs.forEach(inp => { payload[inp.dataset.field] = inp.value.trim(); });
    payloads.push(payload);
  });

  const btn = document.getElementById('saveEditCam');
  btn.disabled = true;
  btn.textContent = 'Salvando';

  const mode = _invOltView || 'olt';
  const res = await api(`/api/cameras/save?mode=${encodeURIComponent(mode)}`, {
    method: 'POST',
    body: JSON.stringify({ cameras: payloads }),
  });

  btn.disabled = false;
  btn.innerHTML = '<i data-lucide="check"></i> Salvar tudo';
  lucide.createIcons();

  const body = await res?.json().catch(() => ({}));
  if (!res?.ok || body?.ok === false) {
    const el = document.getElementById('editCamErro');
    const firstErr = { body };
    const detail = firstErr?.body?.detail || firstErr?.body?.error || 'linha nao encontrada no inventario atual';
    el.textContent = `${payloads.length} camera(s) nao foram salvas: ${detail}.`;
    el.hidden = false;
    return;
  }

  const shouldRenameDevice = !!document.getElementById('editCamApplyDevice')?.checked;
  let snapshotPatches = [];
  if (shouldRenameDevice) {
    const user = document.getElementById('editCamDeviceUser')?.value.trim() || 'admin';
    const pass = document.getElementById('editCamDevicePass')?.value || '';
    const el = document.getElementById('editCamErro');
    if (!pass) {
      el.textContent = 'Informe a senha para renomear no equipamento.';
      el.hidden = false;
      return;
    }
    // Feedback: pelo tunel isolado o rename no equipamento leva alguns segundos.
    // Sem mudar o botao, parecia que "nao fazia nada".
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-2"></i> Renomeando no equipamento...';
    lucide.createIcons();
    const renameRes = await api('/api/maintenance/batch/rename', {
      method: 'POST',
      body: JSON.stringify({
        user,
        pass,
        targets: payloads.map(p => ({ ip: p.ip, title: p.titulo || p.title || '', channel: 1, remote_connector_id: p.remote_connector_id || p.connector_id || '' })),
      }),
    });
    const renameBody = await renameRes?.json().catch(() => ({}));
    if (!renameRes?.ok || renameBody?.ok === false) {
      const failed = (renameBody?.results || []).filter(r => !r.ok);
      const first = failed[0] || {};
      el.textContent = renameBody?.error || first.error || 'Inventario salvo, mas o equipamento nao aceitou a renomeacao.';
      el.hidden = false;
      return;
    }
    // Rename OK. NAO trava a tela esperando o snapshot (lento pelo tunel):
    // avisa ja e captura em segundo plano; quando terminar, recarrega a lista.
    showToast(`${payloads.length} camera(s) renomeada(s)! Atualizando snapshot em 2o plano...`);
    refreshCamSnapshotsAfterRename(payloads, user, pass, mode)
      .then(() => { try { loadInvOlt(); } catch (_) {} })
      .catch(() => {});
  } else {
    showToast(`${payloads.length} camera(s) salva(s)!`);
  }
  closeEditCamModal();
  applyCamPayloadsLocally([...payloads, ...snapshotPatches]);
  clearApiJsonCache('/api/cameras');
  clearApiJsonCache('/api/dashboard');

  const statusFilter = document.getElementById('filterStatusOlt');
  const hidingFilters = new Set(['missing_data', 'default_title', 'no_olt', 'imgbb_down']);
  if (payloads.length > 1 && statusFilter && hidingFilters.has(statusFilter.value)) {
    statusFilter.value = '';
    showToast(`${payloads.length} camera(s) salva(s). Filtro alterado para Todas para mostrar o resultado.`);
  }

  await loadInvOlt();
}

//  Varredura WebSocket 
function _connectorLabel(row) {
  return `${row.name || row.client || 'Conector'} - ${row.site || row.client || '-'}`;
}

function _connectorNorm(value) {
  return String(value || '').trim().toLowerCase();
}

function _connectorIsOnline(row) {
  return _connectorNorm(row?.status) === 'online';
}

function _connectorHasTunnel(row) {
  // MikroTik reporta o tunel via tunnel/vpn/wireguard.enabled (populado pelo
  // agente). O Ruijie nao tem agente reportando status em tempo real -- a
  // config (.ovpn) salva no cadastro e o sinal disponivel de que o tunel
  // OpenVPN persistente (gerenciado pelo sightops_ruijie_vpn_sync.py) deve
  // estar de pe.
  if (String(row?.type || '').toLowerCase() === 'ruijie') {
    return Boolean(row?.vpn_config);
  }
  return Boolean(row?.tunnel?.enabled || row?.vpn?.enabled || row?.wireguard?.enabled);
}

function _routerConnectors() {
  // Apesar do nome, hoje cobre qualquer conector que possa servir de origem
  // de acesso pro scan/implantacao (MikroTik ou Ruijie com VPN).
  return (_connectors || []).filter(c => ['routeros', 'ruijie'].includes(_connectorNorm(c.type)));
}

function _connectorMatchesSite(row, site) {
  const wanted = _connectorNorm(site);
  if (!wanted) return false;
  return [row?.site, row?.name, row?.client]
    .map(_connectorNorm)
    .filter(Boolean)
    .includes(wanted);
}

function _findConnectorForSite(site) {
  const rows = _routerConnectors().filter(c => _connectorMatchesSite(c, site));
  if (!rows.length) return null;
  return rows.find(c => _connectorIsOnline(c) && _connectorHasTunnel(c))
    || rows.find(c => _connectorIsOnline(c))
    || rows[0];
}

function _connectorById(id) {
  const wanted = String(id || '');
  return _routerConnectors().find(c => String(c.id || c.connector_id || '') === wanted) || null;
}

function _networkContextForSite(site, selectedConnectorId = '') {
  const explicit = selectedConnectorId ? _connectorById(selectedConnectorId) : null;
  const connector = explicit || _findConnectorForSite(site);
  if (!connector) {
    return { origin: 'local', connector: null, connectorId: '', hasTunnel: false, online: false };
  }
  const connectorId = String(connector.id || connector.connector_id || '');
  return {
    origin: 'connector',
    connector,
    connectorId,
    hasTunnel: _connectorHasTunnel(connector),
    online: _connectorIsOnline(connector),
  };
}

async function refreshScanConnectors() {
  const sel = document.getElementById('scanConnector');
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

  const site = (document.getElementById('scanLocal')?.value || document.getElementById('filterSiteOlt')?.value || '').trim();
  const match = _findConnectorForSite(site);
  if (match?.id) sel.value = match.id;
}

function updateScanOriginUi() {
  const site = (document.getElementById('scanLocal')?.value || document.getElementById('filterSiteOlt')?.value || '').trim();
  const connector = document.getElementById('scanConnector');
  const context = _networkContextForSite(site, connector?.value || '');
  const originEl = document.getElementById('scanOrigin');
  if (originEl) originEl.value = context.connectorId ? 'connector' : 'local';
  if (connector) connector.disabled = false;
  const status = document.getElementById('scanConnectorStatus');
  if (status) {
    // Com VPN, o servidor testa na hora se alcanca a rede do cliente direto
    // (rapido, com snapshot) e so cai pro MikroTik (mais lento, so descoberta)
    // se a rede nao responder -- nao da pra saber qual vai ser aqui na tela
    // antes de rodar, entao o texto descreve as duas possibilidades.
    status.innerHTML = context.connectorId
      ? `${context.online ? '<b style="color:var(--primary)">Conector online</b>' : '<b style="color:var(--danger)">Conector offline</b>'} -- ${esc(_connectorLabel(context.connector))}${context.hasTunnel ? ' -- VPN ativa: tenta scan direto, com MikroTik como reserva se a rede nao responder.' : ' -- sem VPN: descoberta limitada pelo MikroTik.'}`
      : 'Sem conector para este site: usando servidor local.';
  }
  const remote = context.connectorId && !context.hasTunnel;
  ['scanSnapshot', 'scanImgbb'].forEach(id => {
    const input = document.getElementById(id);
    if (!input) return;
    input.disabled = remote;
    if (remote) input.checked = false;
    input.closest('.scan-task-row')?.classList.toggle('is-disabled', remote);
  });
}

function _scanModeForCurrentTab() {
  const mode = String(_invOltView || '').trim().toLowerCase();
  return ['basico', 'olt', 'switch'].includes(mode) ? mode : 'olt';
}

function _normalizeScanMode(value) {
  const mode = String(value || '').trim().toLowerCase();
  if (mode === 'basic' || mode === 'base' || mode === 'básico') return 'basico';
  if (mode === 'switch' || mode === 'sw') return 'switch';
  if (mode === 'olt') return 'olt';
  return _scanModeForCurrentTab();
}

function updateScanEnrichDefaults() {
  const mode = _normalizeScanMode(document.getElementById('scanMode')?.value || 'olt');
  const oltChk = document.getElementById('scanOltEnrich');
  const switchChk = document.getElementById('scanSwitchEnrich');
  if (oltChk) oltChk.checked = mode === 'olt';
  if (switchChk) switchChk.checked = mode === 'switch';
}

function openScanModal() {
  document.getElementById('scanLog').textContent = 'Aguardando inicio';
  document.getElementById('modalScan').classList.remove('hidden');
  const modeEl = document.getElementById('scanMode');
  if (modeEl) modeEl.value = _scanModeForCurrentTab();
  updateScanEnrichDefaults();
  refreshScanConnectors().finally(updateScanOriginUi);
}

function closeScanModal() {
  document.getElementById('modalScan').classList.add('hidden');
  if (_scanWs) { _scanWs.close(); _scanWs = null; }
}

function resetScanForm() {
  const ids = ['scanAlvo', 'scanSenha', 'scanLocal'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const user = document.getElementById('scanUsuario');
  if (user) user.value = 'admin';
  const mode = document.getElementById('scanMode');
  if (mode) mode.value = _scanModeForCurrentTab();
  const origin = document.getElementById('scanOrigin');
  if (origin) origin.value = 'local';
  const connector = document.getElementById('scanConnector');
  if (connector) connector.value = '';
  updateScanOriginUi();
  updateScanEnrichDefaults();
  const checks = {
    scanDiscover: true,
    scanSnapshot: false,
    scanImgbb: false,
    scanAppend: false,
    scanNat: false,
  };
  Object.entries(checks).forEach(([id, checked]) => {
    const el = document.getElementById(id);
    if (el) el.checked = checked;
  });
}

function _scanPayloadBase() {
  const alvo    = document.getElementById('scanAlvo').value.trim();
  const usuario = document.getElementById('scanUsuario').value.trim() || 'admin';
  const senha   = document.getElementById('scanSenha').value;
  const selectedSite = document.getElementById('filterSiteOlt')?.value || '';
  const local   = document.getElementById('scanLocal').value.trim() || selectedSite;
  const context = _networkContextForSite(local, document.getElementById('scanConnector')?.value || '');
  const origin = context.connectorId ? 'connector' : 'local';
  const connectorId = context.connectorId;
  if (!alvo) { showToast('Informe o alvo (IP, range ou CIDR)', true); return null; }
  if (origin === 'connector' && !context.online) {
    showToast('O conector selecionado esta offline.', true);
    return null;
  }
  return {
    alvo, usuario, senha,
    append_inventory: document.getElementById('scanAppend').checked,
    nat_mode:         document.getElementById('scanNat').checked,
    inventory_mode:   _normalizeScanMode(document.getElementById('scanMode')?.value),
    scan_origin:      origin,
    connector_id:     origin === 'connector' ? connectorId : '',
    remote_connector_id: origin === 'connector' ? connectorId : '',
    // Sem VPN, so resta o MikroTik -- forcamos aqui. Com VPN, deixamos em
    // aberto (o backend testa a rede na hora e decide sozinho, ver
    // _decide_remote_only em app/services/ws_scan_service.py).
    remote_only:      origin === 'connector' && !context.hasTunnel,
    ...(local && { set_local: true, local }),
  };
}

function _runWsScan(payload) {
  const log = document.getElementById('scanLog');
  log.innerHTML = '';
  appendLog(log, ` ${payload.alvo}`, 'info');
  let completed = false;
  const requestedMode = _normalizeScanMode(payload.inventory_mode || document.getElementById('scanMode')?.value || 'basico');

  if (_scanWs) _scanWs.close();
  // Deriva o WS do MESMO API_BASE do HTTP: no v3 (sub-path /v3-api) o WS tem que
  // ir pro backend do v3, senao o token do tenant cai no backend do PROD e volta
  // "token invalido". Prod: API_BASE == origin -> comportamento inalterado.
  const _wsBase = (typeof API_BASE === 'string' && API_BASE ? API_BASE : location.origin).replace(/^http/, 'ws');
  _scanWs = new WebSocket(`${_wsBase}/ws/scan`);

  _scanWs.onopen  = () => _scanWs.send(JSON.stringify(payload));
  _scanWs.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'done' || msg.type === 'inventory_updated') {
        completed = true;
        clearApiJsonCache('/api/cameras');
        clearApiJsonCache('/api/dashboard');
        _camSessionClear();
        appendLog(log, ' ' + (msg.message || 'Concluido'), 'ok');
        appendLog(log, ' Varredura concluida. Campos limpos.', 'ok');
        resetScanForm();
        showToast('Varredura concluida.');
        if (_currentView === 'inv-nvr') {
          loadInvNvr();
        } else if (_currentView === 'inv-dvr') {
          loadInvDvr();
        } else {
          const scanMode = requestedMode;
          (async () => {
            await _loadCamForMode(scanMode);
            if (scanMode === 'basico') {
              await Promise.allSettled([
                _loadCamForMode('olt'),
                _loadCamForMode('switch'),
              ]);
            }
            if (['basico', 'olt', 'switch'].includes(scanMode)) {
              _invOltView = scanMode;
              try { sessionStorage.setItem('so_cam_view', scanMode); } catch {}
            }
            updateCamTabs();
            populateCamSiteFilter();
            applyInvOltFilters();
          })();
        }
      } else if (msg.type === 'error') {
        appendLog(log, ' ' + (msg.message || 'Erro'), 'err');
      } else {
        appendLog(log, msg.message || JSON.stringify(msg), 'info');
      }
    } catch { appendLog(log, e.data, 'info'); }
  };
  _scanWs.onerror = () => appendLog(log, 'Erro WebSocket', 'err');
  _scanWs.onclose = () => {
    appendLog(log, completed ? ' Concluido ' : ' Encerrado ', completed ? 'ok' : 'info');
  };
}
