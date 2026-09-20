/*
   SightOps  Frontend SPA
    */

// O login nao pode depender do CDN de icones. Se o Lucide falhar, a UI segue viva.
window.lucide = window.lucide || { createIcons() {} };

//  Estado global
let _token = null;
let _currentView = 'dashboard';

// Abre a UI web de um dispositivo (camera/NVR). Se o SightOps Agent estiver
// rodando no PC (127.0.0.1:47600), abre DIRETO pelo tunel (transparente, sem
// reconstrucao). Senao, cai no proxy antigo -- sem regressao. localhost e
// contexto seguro, entao o fetch http a partir do https e permitido.
const SIGHTOPS_AGENT_URL = 'http://127.0.0.1:47600';
// Abre um tunel do agente ate <ip>:<port> e devolve { url, port } locais
// (127.0.0.1:porta), ou null se o agente nao estiver rodando/recusar. Serve
// tanto pra web (navegador) quanto pra porta nao-web, como o Winbox (8291).
async function deviceTunnelOpen(ip, port) {
  ip = String(ip || '').trim();
  if (!ip) return null;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2500);
    const r = await fetch(`${SIGHTOPS_AGENT_URL}/health`, { signal: ctrl.signal });
    clearTimeout(t);
    const health = r.ok ? await r.json() : null;
    if (!health || !health.ok) return null;
  } catch (e) { return null; }
  // O cookie de sessao e httponly (JS nao le); pega um token curto pro agente.
  let token = _token || '';
  if (!token) {
    const tr = await apiJson('/api/auth/agent-token', { cacheTtl: 0, forceRefresh: true }).catch(() => null);
    token = (tr && tr.token) || '';
  }
  const wsbase = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;
  const q = `wsbase=${encodeURIComponent(wsbase)}&ip=${encodeURIComponent(ip)}`
    + `&port=${encodeURIComponent(port || 80)}&token=${encodeURIComponent(token)}`;
  const res = await fetch(`${SIGHTOPS_AGENT_URL}/open?${q}`);
  const data = await res.json();
  return (data && data.ok && data.url) ? data : null;
}

async function openDeviceWeb(ip, port) {
  ip = String(ip || '').trim();
  if (!ip) return;
  const proxyUrl = `${API_BASE}/api/maintenance/web/${encodeURIComponent(ip)}/`;
  let health = null, openData = null, diagErr = '';
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2500);
    const r = await fetch(`${SIGHTOPS_AGENT_URL}/health`, { signal: ctrl.signal });
    clearTimeout(t);
    health = r.ok ? await r.json() : null;
  } catch (e) { diagErr = 'health:' + ((e && e.message) || String(e)); }
  if (health && health.ok) {
    try {
      // O cookie de sessao e httponly (JS nao le); pega um token curto pro agente.
      let token = _token || '';
      if (!token) {
        const tr = await apiJson('/api/auth/agent-token', { cacheTtl: 0, forceRefresh: true }).catch(() => null);
        token = (tr && tr.token) || '';
      }
      const wsbase = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;
      const q = `wsbase=${encodeURIComponent(wsbase)}&ip=${encodeURIComponent(ip)}`
        + `&port=${encodeURIComponent(port || 80)}&token=${encodeURIComponent(token)}`;
      const res = await fetch(`${SIGHTOPS_AGENT_URL}/open?${q}`);
      openData = await res.json();
      if (openData && openData.ok && openData.url) {
        // window.open com 'noopener' devolve null POR ESPECIFICACAO -- nao da
        // pra usar o retorno pra detectar popup bloqueado. Checar isso abria a
        // aba do agente E a do proxy (duas saidas). Se o agente deu a url, a
        // janela e dele: abre e encerra aqui.
        window.open(openData.url, '_blank', 'noopener');
        return;
      } else if (openData) { diagErr = 'open:' + (openData.error || 'sem url'); }
    } catch (e) { diagErr = 'open:' + ((e && e.message) || String(e)); }
  }
  console.warn('[SightOps Agent] DIAG health=', JSON.stringify(health), '| open=', JSON.stringify(openData), '| erro=', diagErr, '-> caindo no proxy');
  window.open(proxyUrl, '_blank', 'noopener');
}
let _scanWs = null;
let _camAuthAction = null;
let _currentUser = null; // ultimo /api/auth/me: role, is_platform_admin, acting_as, enabled_modules...
let _moduleCatalogCache = null; // /api/auth/modules/catalog, buscado uma vez por sessao
let _shellMode = 'client'; // 'owner' (painel do dono) ou 'client' (painel operacional) -- so admin de plataforma pode estar em 'owner'

const SIGHTOPS_CLIENT_BUILD = 'production-zabbix-access-20260811a';
const DISABLED_VIEWS = new Set(['access-whatsapp-triage']);

function resetStaleClientState() {
  const key = 'sightops.client.build';
  let current = '';
  try { current = localStorage.getItem(key) || ''; } catch {}
  if (current === SIGHTOPS_CLIENT_BUILD) return;

  try {
    Object.keys(sessionStorage || {}).forEach(k => {
      if (k.startsWith('so_') || k.startsWith('sightops.')) sessionStorage.removeItem(k);
    });
  } catch {}
  try { localStorage.removeItem('so_token'); } catch {}
  try { localStorage.setItem(key, SIGHTOPS_CLIENT_BUILD); } catch {}
}

resetStaleClientState();

// Cache curto, somente em memoria e por sessao autenticada. Evita repetir
// respostas grandes ao alternar entre telas sem guardar dados entre usuarios.
const _apiJsonCache = new Map();
const API_JSON_CACHE_TTL_MS = 30000;

function clearApiJsonCache(match = '') {
  if (!match) {
    _apiJsonCache.clear();
    return;
  }
  for (const key of _apiJsonCache.keys()) {
    if (key.includes(match)) _apiJsonCache.delete(key);
  }
}

//  Helpers HTTP
async function api(path, opts = {}) {
  const { skipLogout, ...fetchOpts } = opts;
  // FormData precisa que o browser calcule o boundary sozinho no
  // Content-Type -- se a gente fixar 'application/json' aqui, o upload
  // (ex.: import de planilha) vira bytes multipart com header de json e
  // o backend nunca acha o campo do arquivo (422 "Field required").
  const isFormData = fetchOpts.body instanceof FormData;
  const headers = { ...(isFormData ? {} : { 'Content-Type': 'application/json' }), ...(fetchOpts.headers || {}) };
  if (_token) headers['Authorization'] = `Bearer ${_token}`;
  const res = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin', ...fetchOpts, headers });
  const method = String(fetchOpts.method || 'GET').toUpperCase();
  if (res.ok && !['GET', 'HEAD'].includes(method)) clearApiJsonCache();
  if (res.status === 401 && !skipLogout) {
    _token = null;
    try { localStorage.removeItem('so_token'); } catch {}
    showLoginScreen();
    return null;
  }
  return res;
}

async function apiJson(path, opts = {}) {
  const { cacheTtl = API_JSON_CACHE_TTL_MS, forceRefresh = false, ...requestOpts } = opts;
  const method = String(requestOpts.method || 'GET').toUpperCase();
  const canCache = method === 'GET' && cacheTtl > 0;
  const cacheKey = `${_token || 'anonymous'}:${path}`;
  const now = Date.now();
  const cached = _apiJsonCache.get(cacheKey);
  if (canCache && !forceRefresh && cached && cached.expiresAt > now) return cached.data;

  const res = await api(path, requestOpts);
  if (!res || !res.ok) return null;
  const data = await res.json();
  if (canCache) _apiJsonCache.set(cacheKey, { data, expiresAt: now + cacheTtl });
  return data;
}

async function jsonOrReadableError(res, fallback = 'Erro na requisicao.') {
  if (!res) throw new Error(fallback);
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    const clean = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 180);
    throw new Error(clean ? `Servidor retornou resposta inesperada: ${clean}` : fallback);
  }
  if (!res.ok || data?.ok === false) {
    throw new Error(data?.detail || data?.error || data?.msg || data?.message || fallback);
  }
  return data;
}

//  Confirmacao customizada
function showConfirm({ eyebrow = 'Confirmar', title = 'Tem certeza?', msg, label = 'Confirmar', danger = true } = {}) {
  return new Promise(resolve => {
    document.getElementById('confirmEyebrow').textContent = eyebrow;
    document.getElementById('confirmTitle').textContent   = title;
    document.getElementById('confirmMsg').textContent     = msg || '';
    const btn = document.getElementById('confirmOk');
    btn.innerHTML = `<i data-lucide="${danger ? 'trash-2' : 'check'}"></i> ${label}`;
    btn.style.background = danger ? 'var(--danger)' : 'var(--primary)';
    document.getElementById('modalConfirm').classList.remove('hidden');
    lucide.createIcons();

    const ok  = document.getElementById('confirmOk');
    const can = document.getElementById('confirmCancel');
    const close = (val) => {
      document.getElementById('modalConfirm').classList.add('hidden');
      ok.replaceWith(ok.cloneNode(true));
      can.replaceWith(can.cloneNode(true));
      resolve(val);
    };
    document.getElementById('confirmOk').addEventListener('click', () => close(true));
    document.getElementById('confirmCancel').addEventListener('click', () => close(false));
  });
}

//  Toast
let _toastTimer;
function showToast(msg, isError = false) {
  const el = document.getElementById('toast');
  const span = document.getElementById('toastMsg');
  span.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

window.addEventListener('error', (event) => {
  console.error('[SightOps UI]', event.error || event.message);
  if (_token) showToast(event.message || 'Erro na interface.', true);
});

window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  console.error('[SightOps UI promise]', reason);
  if (_token) showToast(reason?.message || 'Acao falhou na interface.', true);
});

//  Auth
async function login(user, pass) {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return { ok: false, msg: err.detail || 'Credenciais invalidas' };
  }
  const data = await res.json();
  _token = data.access_token || data.token || null;
  try { localStorage.removeItem('so_token'); } catch {}
  const me = await fetch(`${API_BASE}/api/auth/me`, { credentials: 'same-origin' });
  if (me.ok) return { ok: true };
  return { ok: false, msg: 'Sessao nao confirmada apos login' };
}

function logout() {
  api('/api/auth/logout', { method: 'POST', skipLogout: true }).catch(() => {});
  _token = null;
  try { localStorage.removeItem('so_token'); } catch {}
  showLoginScreen();
}

async function loadProfile() {
  const data = await apiJson('/api/auth/me', { forceRefresh: true });
  if (!data) return;
  const user = data.user || data;
  _currentUser = user;
  const name = user.full_name || user.username || user.email || '?';
  const role = user.role || user.perfil || '';
  document.getElementById('profileName').textContent = name;
  document.getElementById('profileRole').textContent = role;
  document.getElementById('profileAvatar').textContent = name[0].toUpperCase();
  applyModuleVisibility();
  applyPlatformAdminVisibility();
  renderActingAsBanner();
}

// Esconde do menu lateral qualquer tela que nao faca parte do "pacote" do
// cliente logado. `enabled_modules` nulo = sem restricao (mostra tudo,
// inclusive modulos criados depois que ninguem configurou ainda pra esse
// cliente). A chave de cada modulo E o data-view do item de menu -- ver
// MODULE_CATALOG em app/services/auth_store.py.
function applyModuleVisibility() {
  const enabled = _currentUser?.enabled_modules;
  document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
    const key = btn.dataset.view;
    const restricted = Array.isArray(enabled);
    const accessAddon = restricted && key === 'access-live' && enabled.includes('access-control');
    const visible = !DISABLED_VIEWS.has(key) && (!restricted || enabled.includes(key) || accessAddon);
    btn.classList.toggle('nav-item-hidden', !visible);
  });
  // Grupo "Snapshots": some por completo se as duas telas dele estiverem escondidas.
  const snapGroup = document.getElementById('ngSnapshots');
  if (snapGroup) {
    const subVisible = [...snapGroup.querySelectorAll('.nav-sub-item[data-view]')]
      .some(btn => !btn.classList.contains('nav-item-hidden'));
    snapGroup.classList.toggle('nav-item-hidden', !subVisible);
  }
}

// Painel do Dono (menu, badge, atalhos) so existe pra quem administra a
// PLATAFORMA -- um cliente comum nunca deve ver nem saber que esse modo
// existe. Revalida o _shellMode atual contra o usuario logado (chamada toda
// vez que o perfil e recarregado, ex: apos "Operar como").
function applyPlatformAdminVisibility() {
  setShellMode(_shellMode);
}

// Alterna entre o painel operacional (o que um cliente real ve) e o painel
// do dono (administracao da plataforma: clientes, modulos, auditoria).
// Diferente de "Operar como" (acting_as): essa troca e so uma lente local no
// navegador, sempre dentro do proprio tenant do dono, sem chamada de API nem
// reload. Enquanto acting_as estiver ativo (o dono esta de fato dentro dos
// dados de outro cliente), o modo fica travado em 'client'.
function setShellMode(mode) {
  const isPlatformAdmin = !!_currentUser?.is_platform_admin;
  const actingAs = !!_currentUser?.acting_as;
  _shellMode = (mode === 'owner' && isPlatformAdmin && !actingAs) ? 'owner' : 'client';

  const navClient = document.getElementById('navClientMode');
  const navOwner = document.getElementById('navOwnerMode');
  if (navClient) navClient.hidden = _shellMode === 'owner';
  if (navOwner) navOwner.hidden = !(_shellMode === 'owner' && isPlatformAdmin);

  document.body.classList.toggle('shell-owner', _shellMode === 'owner');
  setText('brandSubtitle', _shellMode === 'owner' ? 'Painel do Dono' : 'Gestao de CFTV');

  const badge = document.getElementById('ownerModeBadge');
  if (badge) badge.hidden = _shellMode !== 'owner';

  const backLink = document.getElementById('btnBackToOwnerPanel');
  if (backLink) backLink.hidden = !(isPlatformAdmin && !actingAs && _shellMode === 'client');

  lucide.createIcons();
}

function renderActingAsBanner() {
  const el = document.getElementById('actingAsBanner');
  if (!el) return;
  if (_currentUser?.acting_as) {
    setText('actingAsBannerText', `Operando como: ${_currentUser.effective_tenant_name || _currentUser.effective_tenant_slug || ''}`);
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

async function actAsTenant(tenantSlug) {
  const res = await api('/api/auth/act-as', { method: 'POST', body: JSON.stringify({ tenant_slug: tenantSlug || '' }) });
  const data = await jsonOrReadableError(res, 'Nao foi possivel trocar de cliente.');
  showToast(tenantSlug ? `Operando como ${data?.user?.effective_tenant_name || tenantSlug}.` : 'Voltou para a propria conta.');
  // Recarrega a pagina inteira: e a forma mais segura de garantir que TODA
  // tela (cada uma com seu proprio cache/estado em memoria) recarregue os
  // dados do tenant novo, em vez de misturar dados de dois clientes na
  // mesma sessao de navegador.
  window.location.reload();
}

//  Telas
function showLoginScreen() {
  document.getElementById('loginScreen').removeAttribute('hidden');
  document.getElementById('appShell').setAttribute('hidden', '');
  document.getElementById('loginError').hidden = true;
  document.getElementById('loginUser').value = '';
  document.getElementById('loginPassword').value = '';
  // Sem isso, o visual do Painel do Dono (fundo dourado do brand-mark, que a
  // tela de login tambem usa) ficava grudado na tela de login apos logout ou
  // expiracao de sessao (401), porque body.shell-owner nunca era removido.
  _currentUser = null;
  setShellMode('client');
}

async function showApp() {
  document.getElementById('loginScreen').setAttribute('hidden', '');
  document.getElementById('appShell').removeAttribute('hidden');
  await loadProfile();
  // Admin de plataforma cai direto no Painel do Dono, exceto se ja estiver
  // "operando como" um cliente (acting_as) -- ai o pouso e o mesmo do cliente.
  const landingIsOwner = !!(_currentUser?.is_platform_admin && !_currentUser?.acting_as);
  setShellMode(landingIsOwner ? 'owner' : 'client');
  navigateTo(landingIsOwner ? 'owner-overview' : 'dashboard');
  lucide.createIcons();
}

//  Navegacao
const VIEW_META = {
  dashboard:       { title: 'Dashboard',        sub: 'Visao geral do parque' },
  'inv-olt':       { title: 'Cameras IP', sub: 'Varredura, filtros e casamento OLT/Switch' },
  'inv-switch':    { title: 'Cameras IP  Switch', sub: 'Cameras via switch gerenciavel' },
  'inv-dvr':       { title: 'Inventario  DVR',  sub: 'Gravadores DVR' },
  'inv-nvr':       { title: 'Gravadores', sub: 'Canais NVR com cameras associadas' },
  'inv-windows':   { title: 'Inventario - Windows', sub: 'Hosts Windows' },
  'snap-cam':      { title: 'Snapshots  Cameras', sub: 'Fotos das cameras IP' },
  'snap-dvr':      { title: 'Snapshots  DVR',   sub: 'Fotos dos canais DVR' },
  'snap-nvr':      { title: 'Snapshots  NVR',   sub: 'Fotos dos canais NVR' },
  'mnt-cam':       { title: 'Manutencao  Cameras', sub: 'Operacoes em lote' },
  'mnt-nvr':       { title: 'Manutencao - Gravadores',  sub: 'Operacoes em lote' },
  playback:        { title: 'Reproducao',       sub: 'Busca de gravacoes por DVR' },
  'ia-nvr':        { title: 'IA  NVR',          sub: 'Indexacao e busca inteligente' },
  'access-live':    { title: 'Acesso ao Vivo', sub: 'Movimentacao em tempo real' },
  'access-messenger': { title: 'Messenger', sub: 'Conversas de WhatsApp enviadas e recebidas' },
  'access-whatsapp-triage': { title: 'Triagem WhatsApp', sub: 'Fila de cadastros recebidos por grupos' },
  'access-control': { title: 'Controle de Acesso', sub: 'Reconhecimento facial e eventos de entrada e saida' },
  'alert-live':     { title: 'Alerta ao Vivo', sub: 'Pedidos de ajuda do botao de panico' },
  'alert-members':  { title: 'Pessoas do Alerta', sub: 'Quem usa o app de panico' },
  'net-operate':   { title: 'Manutencao - Operacoes', sub: 'Ferramentas de diagnostico de rede' },
  planning:        { title: 'Projetos de CFTV', sub: 'Planejamento antes da implantacao' },
  'deploy-olt':    { title: 'Implantacao - OLT', sub: 'Cadastro das OLTs usadas na operacao' },
  'deploy-onu':    { title: 'Implantacao - ONU', sub: 'Provisionamento em campo' },
  'deploy-recorder': { title: 'Implantacao - Gravadores', sub: 'Cadastro de DVR/NVR' },
  'deploy-new':    { title: 'Implantacao - CFTV', sub: 'Assistente de campo' },
  olt:             { title: 'OLT',               sub: 'Coleta de MACs da OLT' },
  switch:          { title: 'Switch',            sub: 'Coleta de MACs do switch' },
  kmz:             { title: 'KMZ  Mapa',        sub: 'Localizacao das cameras' },
  'script-grafana':{ title: 'Scripts  Grafana', sub: '' },
  'script-zabbix': { title: 'Scripts  Zabbix',  sub: '' },
  connectors:      { title: 'Conectores',       sub: 'MikroTik RouterOS dos clientes' },
  monitoring:      { title: 'Monitoramento',     sub: 'Saude dos equipamentos e alertas' },
  tools:           { title: 'Ferramentas',       sub: '' },
  backup:          { title: 'Backup',            sub: 'Exportacao e importacao' },
  settings:        { title: 'Configuracoes',     sub: '' },
  'owner-overview':{ title: 'Visão geral',       sub: 'Administração da plataforma' },
  'owner-clients': { title: 'Clientes',          sub: 'Organizações SaaS' },
  'owner-audit':   { title: 'Auditoria',         sub: 'Eventos de todos os clientes' },
};

const VIEW_ID_MAP = {
  dashboard:        'viewDashboard',
  'inv-olt':        'viewInvOlt',
  'inv-switch':     'viewInvOlt',
  'inv-dvr':        'viewInvDvr',
  'inv-nvr':        'viewInvNvr',
  'inv-windows':    'viewInvWindows',
  'snap-cam':       'viewSnapCam',
  'snap-dvr':       'viewSnapDvr',
  'snap-nvr':       'viewSnapNvr',
  'mnt-cam':        'viewMntCam',
  'mnt-nvr':        'viewMntNvr',
  playback:         'viewPlayback',
  'ia-nvr':         'viewIaNvr',
  'access-live':     'viewAccessLive',
  'access-messenger': 'viewAccessMessenger',
  'access-whatsapp-triage': 'viewAccessWhatsappTriage',
  'access-control':  'viewAccessControl',
  'alert-live':      'viewAlertLive',
  'alert-members':   'viewAlertMembers',
  'net-operate':    'viewNetOperate',
  planning:         'viewPlanning',
  'deploy-olt':     'viewDeployOlt',
  'deploy-onu':     'viewDeployOnu',
  'deploy-recorder':'viewDeployRecorder',
  'deploy-new':     'viewDeployNew',
  olt:              'viewOlt',
  switch:           'viewSwitch',
  kmz:              'viewKmz',
  'script-grafana': 'viewScriptGrafana',
  'script-zabbix':  'viewScriptZabbix',
  connectors:       'viewConnectors',
  monitoring:       'viewMonitoring',
  settings:         'viewSettings',
  'owner-overview': 'viewOwnerOverview',
  'owner-clients':  'viewOwnerClients',
  'owner-audit':    'viewOwnerAudit',
};

function navigateTo(view) {
  // Painel do Dono e reservado a admin de plataforma -- se alguem tentar
  // (ex: digitando no console) sem ser, cai no Dashboard normal.
  if (String(view).startsWith('owner-') && !_currentUser?.is_platform_admin) view = 'dashboard';
  if (DISABLED_VIEWS.has(view)) view = 'dashboard';

  // Esconde todas as views
  document.querySelectorAll('[id^="view"]').forEach(el => el.classList.add('hidden'));

  // Mostra a view alvo
  const targetId = VIEW_ID_MAP[view];
  if (targetId) {
    const el = document.getElementById(targetId);
    if (el) el.classList.remove('hidden');
  }

  // Atualiza nav items
  document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });

  // Atualiza topbar
  const meta = VIEW_META[view] || { title: view, sub: '' };
  document.getElementById('topbarContext').querySelector('strong').textContent = meta.title;
  document.getElementById('topbarContext').querySelector('span').textContent = meta.sub;

  _currentView = view;

  // Fecha sidebar no mobile
  closeSidebar();

  // Carrega dados da view
  loadView(view);
}

function loadView(view) {
  switch (view) {
    case 'dashboard':   loadDashboard();    break;
    case 'inv-olt':     loadInvOlt();       break;
    case 'inv-switch':  setInvOltView('switch'); loadInvOlt(); break;
    case 'inv-dvr':     loadInvDvr();       break;
    case 'inv-nvr':     loadInvNvr();       break;
    case 'inv-windows': loadInvWindows();   break;
    case 'snap-cam':    loadSnapCam();      break;
    case 'snap-dvr':    loadSnapDvr();      break;
    case 'snap-nvr':    loadSnapNvr();      break;
    case 'mnt-cam':     loadMntCam();       break;
    case 'mnt-nvr':     loadMntNvr();       break;
    case 'playback':    loadPlayback();     break;
    case 'ia-nvr':      loadIaNvr();        break;
    case 'access-live':    loadAccessLive();    break;
    case 'access-messenger': loadAccessMessenger(); break;
    case 'access-whatsapp-triage': loadAccessWhatsappTriage(); break;
    case 'access-control': loadAccessControl(); break;
    case 'alert-live':     loadAlertLive();     break;
    case 'alert-members':  loadAlertMembers();  break;
    case 'olt':         loadOlt();          break;
    case 'switch':      loadSwitch();       break;
    case 'kmz':         loadKmz();          break;
    case 'net-operate': loadNetOperate();   break;
    case 'planning':    loadPlanning();     break;
    case 'deploy-olt':  loadDeployOlt();    break;
    case 'deploy-onu':  loadDeployOnu();    break;
    case 'deploy-recorder': loadDeployRecorder(); break;
    case 'deploy-new':  loadDeployNew();    break;
    case 'connectors':  loadConnectors();   break;
    case 'monitoring':  loadMonitoring();   break;
    case 'script-grafana': loadScriptGrafana(); break;
    case 'script-zabbix':  loadScriptZabbix();  break;
    case 'settings':
      loadSettings();
      break;
    case 'owner-overview': loadOwnerOverview(); break;
    case 'owner-clients':  loadOwnerClients();  break;
    case 'owner-audit':    loadOwnerAudit();    break;
    default:
      loadStaticView();
      break;
  }
  scheduleResponsiveHydration();
}

let _responsiveHydrationQueued = false;
function scheduleResponsiveHydration(root = document) {
  if (_responsiveHydrationQueued) return;
  _responsiveHydrationQueued = true;
  requestAnimationFrame(() => {
    _responsiveHydrationQueued = false;
    hydrateResponsiveTables(root);
  });
}

function hydrateResponsiveTables(root = document) {
  const scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll('table').forEach(table => {
    if (table.closest('.leaflet-container')) return;
    const headers = [...table.querySelectorAll('thead th')].map(th =>
      (th.textContent || '').replace(/\s+/g, ' ').trim()
    );
    if (!headers.length) return;
    table.classList.add('responsive-data-table');
    table.querySelectorAll('tbody tr').forEach(row => {
      row.querySelectorAll('td').forEach((td, idx) => {
        if (!td.dataset.label) td.dataset.label = headers[idx] || '';
      });
    });
  });
}

function loadStaticView() {
  lucide.createIcons();
}

function activateSettingsTab(tab = 'overview') {
  const available = [...document.querySelectorAll('[data-settings-panel]')].map(panel => panel.dataset.settingsPanel);
  if (!available.includes(tab)) tab = 'overview';
  document.querySelectorAll('[data-settings-tab]').forEach(button => {
    const active = button.dataset.settingsTab === tab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  });
  document.querySelectorAll('[data-settings-panel]').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.settingsPanel === tab);
  });
  sessionStorage.setItem('sightops.settings.tab', tab);
  scheduleResponsiveHydration(document.getElementById('viewSettings'));
  lucide.createIcons();
}

async function loadSettings() {
  const [me, users, telegram] = await Promise.all([
    apiJson('/api/auth/me'),
    apiJson('/api/auth/users'),
    apiJson('/api/monitoring/telegram', { forceRefresh: true }),
  ]);
  const currentUser = me?.user || {};
  const userRows = users?.users || [];

  const overview = document.getElementById('settingsOverviewStatus');
  if (overview) {
    const telegramState = telegram?.configured ? (telegram.enabled ? 'Ativo' : 'Configurado') : 'Não configurado';
    // Configuracoes so mostra o que e do proprio cliente -- contagem de
    // outros clientes/infraestrutura da plataforma vive no Painel do Dono.
    const stats = [
      ['Cliente atual', currentUser.tenant_name || currentUser.tenant_slug || '-'],
      ['Seu perfil', currentUser.role || '-'],
      ['Usuários da equipe', userRows.length],
      ['Notificações', telegramState],
    ];
    overview.innerHTML = stats
      .map(([label, value]) => `<div class="settings-status-card"><span>${esc(label)}</span><strong title="${esc(value)}">${esc(value)}</strong></div>`).join('');
  }

  setText('settingsUsersSummary', `${userRows.length} usuario${userRows.length === 1 ? '' : 's'} no cliente atual (${currentUser.tenant_name || currentUser.tenant_slug || '-'}).`);
  const usersBody = document.getElementById('settingsUsersBody');
  if (usersBody) {
    usersBody.innerHTML = userRows.length ? userRows.map(u => {
      const isSelf = Number(u.id) === Number(currentUser.id);
      const toggleLabel = Number(u.active) ? 'Desativar' : 'Ativar';
      const toggleIcon = Number(u.active) ? 'user-x' : 'user-check';
      const actions = `
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <button class="ghost-action" style="padding:4px 8px;font-size:11px" onclick="openEditUserModal(${Number(u.id)})"><i data-lucide="pencil"></i> Editar</button>
          ${isSelf ? '' : `<button class="ghost-action" style="padding:4px 8px;font-size:11px" onclick="toggleUserActive(${Number(u.id)}, ${Number(u.active) ? 'false' : 'true'})"><i data-lucide="${toggleIcon}"></i> ${toggleLabel}</button>
          <button class="ghost-action" style="padding:4px 8px;font-size:11px;color:var(--danger)" onclick="deleteUserRow(${Number(u.id)}, '${esc(u.username || '')}')"><i data-lucide="trash-2"></i> Excluir</button>`}
        </div>`;
      return `
      <tr>
        <td class="settings-cell-truncate" title="${esc(u.username || '')}"><strong>${esc(u.username || '')}</strong></td>
        <td class="settings-cell-truncate" title="${esc(u.full_name || '-')}">${esc(u.full_name || '-')}</td>
        <td class="settings-cell-center"><span class="badge badge-gray">${esc(u.role || '')}</span></td>
        <td class="settings-cell-center">${Number(u.active) ? '<span class="badge badge-green">Ativo</span>' : '<span class="badge badge-red">Inativo</span>'}</td>
        <td class="settings-cell-truncate" title="${esc(u.tenant_name || u.tenant_slug || '')}">${esc(u.tenant_name || u.tenant_slug || '')}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('') : '<tr class="empty-row"><td colspan="6">Nenhum usuario cadastrado.</td></tr>';
    lucide.createIcons();
  }
  if (telegram) {
    document.getElementById('telegramChatId').value = telegram.chat_id || '';
    document.getElementById('telegramWarnRx').value = telegram.warn_rx ?? -27;
    document.getElementById('telegramCriticalRx').value = telegram.critical_rx ?? -29;
    document.getElementById('telegramEnabled').checked = !!telegram.enabled;
    document.getElementById('telegramRecovery').checked = !!telegram.notify_recovery;
    const status = document.getElementById('telegramConfigStatus');
    status.textContent = telegram.configured ? (telegram.enabled ? 'Ativo' : 'Configurado') : 'Nao configurado';
    status.className = `badge ${telegram.configured ? 'badge-green' : 'badge-gray'}`;
  }

  scheduleResponsiveHydration(document.getElementById('viewSettings'));
  activateSettingsTab(sessionStorage.getItem('sightops.settings.tab') || 'overview');
  lucide.createIcons();
}

//  Painel do Dono (administracao da plataforma SaaS)

async function loadOwnerOverview() {
  if (!_currentUser?.is_platform_admin) return;
  const [tenants, authStatus, dbStatus] = await Promise.all([
    apiJson('/api/auth/tenants', { forceRefresh: true }),
    apiJson('/api/auth/status', { forceRefresh: true }),
    apiJson('/api/db/status'),
  ]);
  const tenantRows = tenants?.tenants || [];
  const auth = authStatus?.storage || authStatus || {};
  const db = dbStatus || {};

  const overview = document.getElementById('ownerOverviewStatus');
  if (overview) {
    const stats = [
      ['Clientes SaaS', tenantRows.length],
      ['Usuários (todos os clientes)', auth.users ?? '-'],
      ['Tokens ativos', auth.active_tokens ?? '-'],
      ['Auth backend', auth.backend || '-'],
      ['Banco principal', db.backend || '-'],
      ['Sites', db.sites ?? '-'],
      ['Auth obrigatória', authStatus?.auth_required ? 'sim' : 'não'],
      ['Modo legado', authStatus?.legacy_open ? 'sim' : 'não'],
    ];
    overview.innerHTML = stats
      .map(([label, value]) => `<div class="settings-status-card"><span>${esc(label)}</span><strong title="${esc(value)}">${esc(value)}</strong></div>`).join('');
  }

  const recent = [...tenantRows]
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
    .slice(0, 5);
  setText('ownerOverviewRecentSummary', `${tenantRows.length} cliente${tenantRows.length === 1 ? '' : 's'} no total.`);
  const body = document.getElementById('ownerOverviewRecentBody');
  if (body) {
    body.innerHTML = recent.length ? recent.map(t => `
      <tr>
        <td class="settings-cell-truncate" title="${esc(t.name || '')}"><strong>${esc(t.name || '')}</strong></td>
        <td class="settings-cell-truncate" title="${esc(t.slug || '')}"><span class="monospace">${esc(t.slug || '')}</span></td>
        <td class="settings-cell-center">${Number(t.active) ? '<span class="badge badge-green">Ativo</span>' : '<span class="badge badge-red">Inativo</span>'}</td>
        <td class="settings-cell-center">${esc(t.users ?? 0)}</td>
        <td class="settings-cell-date"><span class="text-muted">${esc(formatDateTimeShort(t.created_at || ''))}</span></td>
      </tr>`).join('') : '<tr class="empty-row"><td colspan="5">Nenhum cliente cadastrado.</td></tr>';
  }
  scheduleResponsiveHydration(document.getElementById('viewOwnerOverview'));
  lucide.createIcons();
}

async function loadOwnerClients() {
  if (!_currentUser?.is_platform_admin) return;
  const [me, tenants] = await Promise.all([
    apiJson('/api/auth/me'),
    apiJson('/api/auth/tenants', { forceRefresh: true }),
  ]);
  const currentUser = me?.user || _currentUser || {};
  const tenantRows = tenants?.tenants || [];

  setText('settingsTenantsSummary', `${tenantRows.length} cliente${tenantRows.length === 1 ? '' : 's'} cadastrado${tenantRows.length === 1 ? '' : 's'}.`);
  const tenantsBody = document.getElementById('settingsTenantsBody');
  if (tenantsBody) {
    if (!tenantsBody.dataset.tenantActionsBound) {
      tenantsBody.addEventListener('click', handleTenantActionClick);
      tenantsBody.dataset.tenantActionsBound = '1';
    }
    tenantsBody.innerHTML = tenantRows.length ? tenantRows.map(t => {
      const modulesLabel = Array.isArray(t.enabled_modules) ? `${t.enabled_modules.length} módulo(s)` : 'Sem restrição';
      const isEffectiveCurrent = (currentUser.effective_tenant_slug || currentUser.tenant_slug) === t.slug;
      const isHomeTenant = currentUser.tenant_slug === t.slug;
      const canSuspendOrDelete = !isEffectiveCurrent && !isHomeTenant;
      const statusHtml = `
        <span class="tenant-status-stack">
          ${Number(t.active) ? '<span class="badge badge-green">Ativo</span>' : '<span class="badge badge-red">Pausado</span>'}
          ${isEffectiveCurrent ? '<span class="badge badge-gray tenant-here-badge">Voce esta aqui</span>' : ''}
        </span>`;
      const actions = `
        <div class="tenant-actions">
          <button class="tenant-action-btn" type="button" data-tenant-action="edit" data-tenant-id="${Number(t.id)}" data-tenant-name="${esc(t.name || t.slug || '')}" data-tenant-active="${Number(t.active) ? '1' : '0'}" title="Editar cliente"><i data-lucide="pencil"></i><span>Editar</span></button>
          <button class="tenant-action-btn" type="button" data-tenant-action="modules" data-tenant-id="${Number(t.id)}" data-tenant-name="${esc(t.name || t.slug || '')}" title="${esc(modulesLabel)}"><i data-lucide="sliders-horizontal"></i><span>Módulos</span></button>
          <button class="tenant-action-btn" type="button" data-tenant-action="zabbix" data-tenant-id="${Number(t.id)}" data-tenant-name="${esc(t.name || t.slug || '')}" data-tenant-slug="${esc(t.slug || '')}" title="Acesso Zabbix"><i data-lucide="eye"></i><span>Zabbix</span></button>
          ${canSuspendOrDelete ? `<button class="tenant-action-btn" type="button" data-tenant-action="toggle" data-tenant-id="${Number(t.id)}" data-tenant-name="${esc(t.name || t.slug || '')}" data-tenant-next-active="${Number(t.active) ? '0' : '1'}" title="${Number(t.active) ? 'Pausar cliente' : 'Ativar cliente'}"><i data-lucide="${Number(t.active) ? 'pause-circle' : 'play-circle'}"></i><span>${Number(t.active) ? 'Pausar' : 'Ativar'}</span></button>` : ''}
          ${canSuspendOrDelete ? `<button class="tenant-action-btn danger" type="button" data-tenant-action="delete" data-tenant-id="${Number(t.id)}" data-tenant-name="${esc(t.name || t.slug || '')}" title="Excluir cliente"><i data-lucide="trash-2"></i><span>Excluir</span></button>` : ''}
        </div>`;
      return `
      <tr>
        <td class="settings-cell-truncate" title="${esc(t.name || '')}"><strong>${esc(t.name || '')}</strong></td>
        <td class="settings-cell-truncate" title="${esc(t.slug || '')}"><span class="monospace">${esc(t.slug || '')}</span></td>
        <td class="settings-cell-center">${statusHtml}</td>
        <td class="settings-cell-center">${esc(t.users ?? 0)}</td>
        <td class="settings-cell-date"><span class="text-muted">${esc(formatDateTimeShort(t.created_at || ''))}</span></td>
        <td class="settings-cell-actions">${actions}</td>
      </tr>`;
    }).join('') : '<tr class="empty-row"><td colspan="6">Nenhum cliente cadastrado.</td></tr>';
  }
  scheduleResponsiveHydration(document.getElementById('viewOwnerClients'));
  lucide.createIcons();
}

function handleTenantActionClick(event) {
  const btn = event.target.closest?.('[data-tenant-action]');
  if (!btn) return;
  const action = btn.dataset.tenantAction || '';
  const tenantId = Number(btn.dataset.tenantId || 0);
  const tenantName = btn.dataset.tenantName || '';
  if (!tenantId) return;
  if (action === 'edit') {
    openTenantEditModal(tenantId, tenantName, btn.dataset.tenantActive === '1');
  } else if (action === 'modules') {
    openTenantModulesModal(tenantId, tenantName);
  } else if (action === 'zabbix') {
    openTenantZabbixModal(tenantId, tenantName, btn.dataset.tenantSlug || '');
  } else if (action === 'toggle') {
    toggleTenantActive(tenantId, btn.dataset.tenantNextActive === '1', tenantName);
  } else if (action === 'delete') {
    deleteTenantRow(tenantId, tenantName);
  }
}

let _tenantZabbixTarget = null; // { id, name, slug }

function closeTenantZabbixModal() {
  _tenantZabbixTarget = null;
  document.getElementById('modalTenantZabbix')?.classList.add('hidden');
}

function openTenantZabbixModal(tenantId, tenantName, tenantSlug) {
  _tenantZabbixTarget = { id: Number(tenantId), name: tenantName || '', slug: tenantSlug || '' };
  const slug = String(tenantSlug || tenantName || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'cliente';
  const usernameInput = document.getElementById('tenantZabbixUser');
  const passwordInput = document.getElementById('tenantZabbixPass');
  const subtitle = document.getElementById('tenantZabbixSubtitle');
  const err = document.getElementById('tenantZabbixErro');
  if (usernameInput) usernameInput.value = `${slug}.viewer`;
  if (passwordInput) passwordInput.value = '';
  if (subtitle) subtitle.textContent = tenantName || tenantSlug || '';
  if (err) {
    err.hidden = true;
    err.textContent = '';
  }
  document.getElementById('modalTenantZabbix')?.classList.remove('hidden');
  usernameInput?.focus();
  lucide.createIcons();
}

async function saveTenantZabbixAccess() {
  if (!_tenantZabbixTarget) return;
  const username = document.getElementById('tenantZabbixUser')?.value.trim() || '';
  const password = document.getElementById('tenantZabbixPass')?.value || '';
  const err = document.getElementById('tenantZabbixErro');
  if (!username || password.length < 8) {
    if (err) {
      err.textContent = 'Informe usuario e senha com pelo menos 8 caracteres.';
      err.hidden = false;
    }
    return;
  }
  try {
    const res = await api(`/api/auth/tenants/${Number(_tenantZabbixTarget.id)}/zabbix-access`, {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    const data = await jsonOrReadableError(res, 'Nao foi possivel provisionar acesso Zabbix.');
    closeTenantZabbixModal();
    showToast(`Zabbix liberado: ${data.zabbix_user} -> ${data.zabbix_hostgroup}.`);
  } catch (e) {
    if (err) {
      err.textContent = e?.message || 'Nao foi possivel provisionar acesso Zabbix.';
      err.hidden = false;
    } else {
      showToast(e?.message || 'Nao foi possivel provisionar acesso Zabbix.', true);
    }
  }
}

async function loadOwnerAudit() {
  if (!_currentUser?.is_platform_admin) return;
  const data = await apiJson('/api/auth/audit/platform', { forceRefresh: true });
  const events = data?.events || [];
  setText('ownerAuditSummary', `${events.length} evento${events.length === 1 ? '' : 's'} recente${events.length === 1 ? '' : 's'}.`);
  const body = document.getElementById('ownerAuditBody');
  if (body) {
    body.innerHTML = events.length ? events.map(e => `
      <tr>
        <td class="settings-cell-date"><span class="text-muted">${esc(formatDateTimeShort(e.created_at || ''))}</span></td>
        <td class="settings-cell-truncate" title="${esc(e.tenant_name || e.tenant_slug || '')}">${esc(e.tenant_name || e.tenant_slug || '-')}</td>
        <td>${esc(e.action || '')}</td>
        <td class="settings-cell-truncate" title="${esc(e.resource_type || '')} ${esc(e.resource_id || '')}">${esc(e.resource_type || '')} ${esc(e.resource_id || '')}</td>
        <td class="settings-cell-truncate" title="${esc(e.detail_json || '')}">${esc(e.detail_json || '-')}</td>
      </tr>`).join('') : '<tr class="empty-row"><td colspan="5">Nenhum evento registrado.</td></tr>';
  }
  scheduleResponsiveHydration(document.getElementById('viewOwnerAudit'));
  lucide.createIcons();
}

async function saveTelegramSettings() {
  const payload = {
    bot_token: document.getElementById('telegramBotToken')?.value || '',
    chat_id: document.getElementById('telegramChatId')?.value.trim() || '',
    warn_rx: Number(document.getElementById('telegramWarnRx')?.value || -27),
    critical_rx: Number(document.getElementById('telegramCriticalRx')?.value || -29),
    enabled: !!document.getElementById('telegramEnabled')?.checked,
    notify_recovery: !!document.getElementById('telegramRecovery')?.checked,
  };
  const res = await api('/api/monitoring/telegram', { method: 'PUT', body: JSON.stringify(payload) });
  const data = await jsonOrReadableError(res, 'Nao foi possivel salvar o Telegram.');
  document.getElementById('telegramBotToken').value = '';
  showToast(data.configured ? 'Telegram configurado.' : 'Configuracao salva; informe token e chat.');
  await loadSettings();
}

async function testTelegramSettings() {
  await saveTelegramSettings();
  const res = await api('/api/monitoring/telegram/test', { method: 'POST' });
  await jsonOrReadableError(res, 'Nao foi possivel enviar a mensagem de teste.');
  showToast('Mensagem de teste enviada ao Telegram.');
}

async function createTenantFromSettings() {
  const name = document.getElementById('tenantName')?.value.trim() || '';
  const slug = document.getElementById('tenantSlug')?.value.trim() || '';
  const owner_username = document.getElementById('tenantOwnerUser')?.value.trim() || '';
  const owner_password = document.getElementById('tenantOwnerPass')?.value || '';
  if (!name) {
    showToast('Informe o nome do cliente.', true);
    return;
  }
  const res = await api('/api/auth/tenants', {
    method: 'POST',
    body: JSON.stringify({ name, slug, owner_username, owner_password }),
  });
  if (!res?.ok) {
    const err = await res?.json().catch(() => ({}));
    showToast(err?.detail || 'Nao foi possivel criar o cliente.', true);
    return;
  }
  ['tenantName', 'tenantSlug', 'tenantOwnerUser', 'tenantOwnerPass'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  showToast('Cliente criado.');
  loadOwnerClients();
}

let _tenantEditTarget = null; // { id, active }

function closeTenantEditModal() {
  document.getElementById('modalTenantEdit')?.classList.add('hidden');
  _tenantEditTarget = null;
}

function openTenantEditModal(tenantId, tenantName, active) {
  _tenantEditTarget = { id: Number(tenantId), active: !!active };
  const nameInput = document.getElementById('tenantEditName');
  const activeInput = document.getElementById('tenantEditActive');
  const errorEl = document.getElementById('tenantEditErro');
  if (nameInput) nameInput.value = tenantName || '';
  if (activeInput) activeInput.checked = !!active;
  if (errorEl) {
    errorEl.textContent = '';
    errorEl.hidden = true;
  }
  document.getElementById('modalTenantEdit')?.classList.remove('hidden');
  setTimeout(() => nameInput?.focus(), 0);
  lucide.createIcons();
}

async function saveTenantEdit() {
  if (!_tenantEditTarget) return;
  const name = document.getElementById('tenantEditName')?.value.trim() || '';
  const active = !!document.getElementById('tenantEditActive')?.checked;
  const errEl = document.getElementById('tenantEditErro');
  if (!name) {
    if (errEl) {
      errEl.textContent = 'Informe o nome do cliente.';
      errEl.hidden = false;
    }
    return;
  }
  const btn = document.getElementById('btnSaveTenantEdit');
  if (btn) btn.disabled = true;
  const res = await api(`/api/auth/tenants/${Number(_tenantEditTarget.id)}`, {
    method: 'PATCH',
    body: JSON.stringify({ name, active }),
  });
  if (btn) btn.disabled = false;
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    if (errEl) {
      errEl.textContent = data?.detail || 'Nao foi possivel salvar o cliente.';
      errEl.hidden = false;
    }
    return;
  }
  closeTenantEditModal();
  clearApiJsonCache('/api/auth/tenants');
  showToast('Cliente atualizado.');
  await loadOwnerClients();
  if (_currentView === 'owner-overview') await loadOwnerOverview();
}

async function toggleTenantActive(tenantId, active, tenantName) {
  const action = active ? 'ativar' : 'pausar';
  const ok = await showConfirm({
    eyebrow: 'Cliente SaaS',
    title: `${active ? 'Ativar' : 'Pausar'} ${tenantName}?`,
    msg: active
      ? 'O cliente volta a poder operar normalmente no SightOps.'
      : 'O cliente fica bloqueado para operacao ate ser ativado novamente.',
    label: active ? 'Ativar cliente' : 'Pausar cliente',
    danger: !active,
  });
  if (!ok) return;
  const res = await api(`/api/auth/tenants/${Number(tenantId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ active: !!active }),
  });
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    showToast(data?.detail || `Nao foi possivel ${action} o cliente.`, true);
    return;
  }
  clearApiJsonCache('/api/auth/tenants');
  showToast(active ? 'Cliente ativado.' : 'Cliente pausado.');
  await loadOwnerClients();
  if (_currentView === 'owner-overview') await loadOwnerOverview();
}

async function deleteTenantRow(tenantId, tenantName) {
  const ok = await showConfirm({
    eyebrow: 'Cliente SaaS',
    title: `Excluir ${tenantName}?`,
    msg: 'Isso remove o cliente, usuarios dele e inventarios vinculados a esse slug quando estiverem no banco compartilhado.',
    label: 'Excluir cliente',
    danger: true,
  });
  if (!ok) return;
  const res = await api(`/api/auth/tenants/${Number(tenantId)}`, { method: 'DELETE' });
  if (!res?.ok) {
    const err = await res?.json().catch(() => ({}));
    showToast(err?.detail || 'Nao foi possivel excluir o cliente.', true);
    return;
  }
  clearApiJsonCache('/api/auth/tenants');
  showToast('Cliente excluido.');
  await loadOwnerClients();
  if (_currentView === 'owner-overview') await loadOwnerOverview();
}

let _tenantModulesTarget = null; // { id, name }

async function _getModuleCatalog() {
  if (_moduleCatalogCache) return _moduleCatalogCache;
  const data = await apiJson('/api/auth/modules/catalog');
  _moduleCatalogCache = data?.modules || [];
  return _moduleCatalogCache;
}

async function openTenantModulesModal(tenantId, tenantName) {
  const tenants = await apiJson('/api/auth/tenants', { forceRefresh: true });
  const tenant = (tenants?.tenants || []).find(t => Number(t.id) === Number(tenantId));
  const current = Array.isArray(tenant?.enabled_modules) ? tenant.enabled_modules : null;
  const catalog = await _getModuleCatalog();

  _tenantModulesTarget = { id: tenantId, name: tenantName };
  setText('tenantModulesSubtitle', tenantName);
  document.getElementById('tenantModulesErro').hidden = true;

  const bySection = {};
  catalog.forEach(m => {
    bySection[m.section] = bySection[m.section] || [];
    bySection[m.section].push(m);
  });
  const body = document.getElementById('tenantModulesBody');
  body.innerHTML = Object.entries(bySection).map(([section, mods]) => `
    <div style="margin-bottom:12px">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:6px">${esc(section)}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        ${mods.map(m => `
          <label style="display:flex;align-items:center;gap:6px;font-size:13px">
            <input type="checkbox" class="tenant-module-chk" value="${esc(m.key)}" ${(!current || current.includes(m.key)) ? 'checked' : ''}>
            ${esc(m.label)}
          </label>
        `).join('')}
      </div>
    </div>
  `).join('');

  const unrestrictedChk = document.getElementById('tenantModulesUnrestricted');
  unrestrictedChk.checked = current === null;
  body.style.opacity = current === null ? '.45' : '1';
  body.style.pointerEvents = current === null ? 'none' : 'auto';

  document.getElementById('modalTenantModules').classList.remove('hidden');
  lucide.createIcons();
}

function _tenantModulesToggleUnrestricted() {
  const body = document.getElementById('tenantModulesBody');
  const on = document.getElementById('tenantModulesUnrestricted').checked;
  body.style.opacity = on ? '.45' : '1';
  body.style.pointerEvents = on ? 'none' : 'auto';
}

async function saveTenantModules() {
  if (!_tenantModulesTarget) return;
  const unrestricted = document.getElementById('tenantModulesUnrestricted').checked;
  const modules = unrestricted
    ? []
    : [...document.querySelectorAll('.tenant-module-chk:checked')].map(c => c.value);
  const btn = document.getElementById('btnSaveTenantModules');
  btn.disabled = true;
  const res = await api(`/api/auth/tenants/${_tenantModulesTarget.id}/modules`, {
    method: 'PUT',
    body: JSON.stringify({ modules, unrestricted }),
  });
  btn.disabled = false;
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    const el = document.getElementById('tenantModulesErro');
    el.textContent = data?.detail || 'Não foi possível salvar os módulos.';
    el.hidden = false;
    return;
  }
  showToast(`Módulos de ${_tenantModulesTarget.name} atualizados.`);
  document.getElementById('modalTenantModules').classList.add('hidden');
  // Se o admin estiver operando como esse cliente agora, o proprio menu dele
  // muda -- recarrega pra refletir na hora, nao so na proxima troca de tela.
  if ((_currentUser?.effective_tenant_id) === _tenantModulesTarget.id) {
    await loadProfile();
  }
  loadOwnerClients();
}

async function createUserFromSettings() {
  const username = document.getElementById('newUserName')?.value.trim() || '';
  const password = document.getElementById('newUserPass')?.value || '';
  const role = document.getElementById('newUserRole')?.value || 'viewer';
  const full_name = document.getElementById('newUserFullName')?.value.trim() || '';
  if (!username || !password) {
    showToast('Informe usuario e senha.', true);
    return;
  }
  const res = await api('/api/auth/users', {
    method: 'POST',
    body: JSON.stringify({ username, password, role, full_name }),
  });
  if (!res?.ok) {
    const err = await res?.json().catch(() => ({}));
    showToast(err?.detail || 'Nao foi possivel criar o usuario.', true);
    return;
  }
  ['newUserName', 'newUserPass', 'newUserFullName'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  showToast('Usuario criado.');
  loadSettings();
}

let _editUserTarget = null; // { id, username }

async function openEditUserModal(userId) {
  const data = await apiJson('/api/auth/users', { forceRefresh: true });
  const u = (data?.users || []).find(row => Number(row.id) === Number(userId));
  if (!u) {
    showToast('Usuario nao encontrado.', true);
    return;
  }
  _editUserTarget = { id: u.id, username: u.username };
  setText('editUserSubtitle', u.username || '');
  document.getElementById('editUserFullName').value = u.full_name || '';
  document.getElementById('editUserEmail').value = u.email || '';
  document.getElementById('editUserRole').value = u.role || 'viewer';
  document.getElementById('editUserErro').hidden = true;
  document.getElementById('modalEditUser').classList.remove('hidden');
  lucide.createIcons();
}

async function saveEditUser() {
  if (!_editUserTarget) return;
  const full_name = document.getElementById('editUserFullName')?.value.trim() || '';
  const email = document.getElementById('editUserEmail')?.value.trim() || '';
  const role = document.getElementById('editUserRole')?.value || 'viewer';
  const btn = document.getElementById('btnSaveEditUser');
  btn.disabled = true;
  const res = await api(`/api/auth/users/${_editUserTarget.id}`, {
    method: 'POST',
    body: JSON.stringify({ full_name, email, role }),
  });
  btn.disabled = false;
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    const el = document.getElementById('editUserErro');
    el.textContent = data?.detail || 'Nao foi possivel salvar o usuario.';
    el.hidden = false;
    return;
  }
  showToast(`Usuario ${_editUserTarget.username} atualizado.`);
  document.getElementById('modalEditUser').classList.add('hidden');
  loadSettings();
}

async function toggleUserActive(userId, active) {
  const res = await api(`/api/auth/users/${userId}/active`, {
    method: 'POST',
    body: JSON.stringify({ active }),
  });
  const data = await jsonOrReadableError(res, 'Nao foi possivel alterar o status do usuario.').catch(err => {
    showToast(err.message, true);
    return null;
  });
  if (!data) return;
  showToast(active ? 'Usuario ativado.' : 'Usuario desativado.');
  loadSettings();
}

async function deleteUserRow(userId, username) {
  const ok = await showConfirm({
    eyebrow: 'Usuários',
    title: `Excluir ${username}?`,
    msg: 'Apaga a conta definitivamente. Nao da pra desfazer.',
    label: 'Excluir',
    danger: true,
  });
  if (!ok) return;
  const res = await api(`/api/auth/users/${userId}`, { method: 'DELETE' });
  const data = await jsonOrReadableError(res, 'Nao foi possivel excluir o usuario.').catch(err => {
    showToast(err.message, true);
    return null;
  });
  if (!data) return;
  showToast(`Usuario ${username} excluido.`);
  loadSettings();
}

function loadScriptGrafana() {
  const log = document.getElementById('grafanaLog');
  if (log && !log.textContent.trim()) log.textContent = 'Aguardando configuracao.';
  lucide.createIcons();
}

function loadScriptZabbix() {
  const source = document.getElementById('zbxSource');
  if (source) source.dispatchEvent(new Event('change'));
  const log = document.getElementById('zabbixLog');
  if (log && !log.textContent.trim()) log.textContent = 'Aguardando configuracao.';
  lucide.createIcons();
}

//  Sidebar mobile
function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('mobileBackdrop').classList.add('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('mobileBackdrop').classList.remove('open');
}

//  Dashboard
//  Dashboard Drawer
let _dashDrawerData = null;

function _openDashDrawer(eyebrow, title) {
  document.getElementById('dashDrawerEyebrow').textContent = eyebrow;
  document.getElementById('dashDrawerTitle').textContent = title;
  document.getElementById('dashDrawerBody').innerHTML = '<div style="padding:32px 20px;text-align:center;color:var(--muted);font-size:13px">Carregando</div>';
  document.getElementById('dashDrawerFilters').innerHTML = '';
  const drawer  = document.getElementById('dashDrawer');
  const overlay = document.getElementById('dashDrawerOverlay');
  drawer.classList.remove('hidden');
  overlay.classList.remove('hidden');
  requestAnimationFrame(() => requestAnimationFrame(() => drawer.classList.add('open')));
  lucide.createIcons();
}

function closeDashDrawer() {
  const drawer  = document.getElementById('dashDrawer');
  const overlay = document.getElementById('dashDrawerOverlay');
  drawer.classList.remove('open');
  setTimeout(() => { drawer.classList.add('hidden'); overlay.classList.add('hidden'); }, 270);
}

function _drawerGoToInventory(view, searchValue, camMode) {
  closeDashDrawer();
  if (view === 'inv-olt' && searchValue) {
    _pendingOpenCamIp = searchValue;
  }
  setTimeout(() => {
    navigateTo(view);
    if (view === 'inv-olt' && camMode) {
      setTimeout(() => setInvOltView(camMode), 120);
    }
    if (searchValue && view !== 'inv-olt') {
      setTimeout(() => {
        const inputMap = { 'inv-dvr': 'searchInvDvr', 'inv-nvr': 'searchInvNvr' };
        const inp = document.getElementById(inputMap[view]);
        if (inp) inp.value = searchValue;
      }, 300);
    }
  }, 150);
}

function _drawerStatusDot(status) {
  const s = (status || '').toLowerCase();
  const online  = ['online','ok','up','ativo','active'].includes(s);
  const offline = ['offline','down','inativo','inactive','auth_failed','timeout','erro','error'].includes(s);
  const color = online ? 'var(--primary)' : offline ? 'var(--danger)' : 'var(--muted)';
  return `<span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0;display:inline-block"></span>`;
}

