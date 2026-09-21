// Modulo Alerta (botao de panico) -- tela da central + cadastro de pessoas.
//
// Backend: app/api/endpoints/alert.py. App do celular: frontend/alerta/.
// A tela ao vivo faz poll a cada 3s so enquanto esta visivel; o badge do menu
// e a sirene rodam num poll leve (/api/alert/summary) em qualquer tela, pra
// central nao perder alerta porque estava olhando outra coisa.

let _alertMap = null;
let _alertMarkers = null;
let _alertTrail = null;
let _alertIncidents = [];
let _alertSelectedId = '';
let _alertDetail = null;
let _alertScope = 'live';
let _alertTimer = null;
let _alertBound = false;
let _alertLoading = false;
let _alertFittedOnce = false;
let _alertKnownIds = new Set();
let _alertNearbyKey = '';
let _alertNearbyHtml = '';
let _alertCamHandle = null;
let _alertMembers = [];
let _alertMembersBound = false;
let _alertSummaryTimer = null;
let _alertSirenTimer = null;
let _alertAudio = null;
let _alertConnectors = null;

const ALERT_REFRESH_MS = 3000;
const ALERT_SUMMARY_MS = 10000;

const ALERT_STATUS = {
  new: { label: 'sem atendimento', pill: 'danger' },
  acknowledged: { label: 'assumido', pill: 'amber' },
  dispatched: { label: 'equipe a caminho', pill: 'amber' },
  closed: { label: 'encerrado', pill: 'success' },
  cancelled: { label: 'cancelado pela pessoa', pill: 'neutral' },
};

function alertOpen(inc) {
  return ['new', 'acknowledged', 'dispatched'].includes(inc?.status);
}

// Som do alerta: cada computador da central escolhe o seu (localStorage).
const ALERT_SOUND_DEFAULTS = {
  enabled: true,
  siren: 'sweep',          // sweep | fast | beep | bell | none
  voice: '',               // '' = automatica pt-BR; 'off' = sem voz; senao voiceURI
  rate: 1,
  volume: 0.8,
  repeat: 15,              // segundos entre repeticoes da voz
  textPanic: 'Atenção! {nome} precisa de ajuda. {local}',
  textDuress: 'Atenção! Coação! {nome} está sob coação. {local}',
};

function alertSoundCfg() {
  let cfg = {};
  try { cfg = JSON.parse(localStorage.getItem('alert_sound_cfg') || '{}') || {}; } catch {}
  try { if (localStorage.getItem('alert_sound') === '0' && cfg.enabled === undefined) cfg.enabled = false; } catch {}
  return { ...ALERT_SOUND_DEFAULTS, ...cfg };
}

function alertSaveSoundCfg(cfg) {
  try { localStorage.setItem('alert_sound_cfg', JSON.stringify(cfg)); localStorage.removeItem('alert_sound'); } catch {}
}

function alertSoundOn() {
  return alertSoundCfg().enabled !== false;
}

function alertDate(v) {
  const d = v ? new Date(v) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

function alertClock(v) {
  const d = alertDate(v);
  return d ? d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '-';
}

function alertAgo(v) {
  const d = alertDate(v);
  if (!d) return '-';
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h${String(m % 60).padStart(2, '0')}` : d.toLocaleDateString('pt-BR');
}

function alertCpf(v) {
  const d = String(v || '').replace(/\D/g, '');
  return d.length === 11 ? `${d.slice(0, 3)}.${d.slice(3, 6)}.${d.slice(6, 9)}-${d.slice(9)}` : d;
}

function alertPhone(v) {
  const d = String(v || '').replace(/\D/g, '');
  if (d.length === 11) return `(${d.slice(0, 2)}) ${d.slice(2, 7)}-${d.slice(7)}`;
  if (d.length === 10) return `(${d.slice(0, 2)}) ${d.slice(2, 6)}-${d.slice(6)}`;
  return String(v || '');
}

// Ponto do alerta no mapa: GPS do celular; sem GPS, a lotacao cadastrada.
function alertPoint(inc) {
  if (inc?.last_lat != null && inc?.last_lon != null) return { lat: inc.last_lat, lon: inc.last_lon, gps: true, acc: inc.last_accuracy };
  const m = inc?.member || {};
  if (m.unit_lat != null && m.unit_lon != null) return { lat: m.unit_lat, lon: m.unit_lon, gps: false };
  return null;
}

function alertStatusPill(inc) {
  const st = ALERT_STATUS[inc.status] || { label: inc.status, pill: 'neutral' };
  return `<span class="pill ${st.pill}">${esc(st.label)}</span>`;
}

function alertKindPill(inc) {
  return inc.kind === 'duress'
    ? '<span class="pill alert-duress-pill">COACAO</span>'
    : '<span class="pill danger">panico</span>';
}

async function alertLoadConnectors() {
  if (_alertConnectors) return _alertConnectors;
  try {
    const data = await alertApiJson('/api/connectors');
    _alertConnectors = (data.connectors || [])
      .map((c) => ({ id: String(c.id || c.connector_id || ''), name: String(c.name || c.site || c.id || '') }))
      .filter((c) => c.id)
      .sort((a, b) => a.name.localeCompare(b.name, 'pt-BR'));
  } catch {
    _alertConnectors = [];
  }
  return _alertConnectors;
}

function alertConnectorName(id) {
  if (!id) return '';
  return (_alertConnectors || []).find((c) => c.id === id)?.name || 'local monitorado';
}

async function alertApiJson(path, opts = {}) {
  const res = await api(path, opts);
  if (!res) throw new Error('Sessao expirada.');
  return jsonOrReadableError(res);
}

// ---------------------------------------------------------------- sirene

function alertAudioCtx() {
  if (!_alertAudio) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    _alertAudio = new Ctx();
  }
  if (_alertAudio.state === 'suspended') _alertAudio.resume().catch(() => {});
  return _alertAudio;
}

// O navegador so libera audio depois de um clique na pagina.
document.addEventListener('click', () => alertAudioCtx(), { passive: true, capture: true });

function alertTone(ctx, { type = 'sine', from = 880, to = null, start = 0, dur = 0.3, gain = 0.18 }) {
  const t = ctx.currentTime + start;
  const osc = ctx.createOscillator();
  const g = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(from, t);
  if (to) osc.frequency.linearRampToValueAtTime(to, t + dur);
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(Math.max(gain, 0.0002), t + 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  osc.connect(g).connect(ctx.destination);
  osc.start(t);
  osc.stop(t + dur + 0.02);
}

const ALERT_SIRENS = {
  sweep: { label: 'Sirene (sobe e desce)', play: (c, v) => {
    alertTone(c, { type: 'square', from: 880, to: 1320, dur: 0.35, gain: 0.22 * v });
    alertTone(c, { type: 'square', from: 1320, to: 880, start: 0.35, dur: 0.35, gain: 0.22 * v });
  } },
  fast: { label: 'Alarme rapido', play: (c, v) => {
    [0, 0.2, 0.4].forEach((s) => alertTone(c, { type: 'square', from: 1050, start: s, dur: 0.14, gain: 0.22 * v }));
  } },
  beep: { label: 'Bipe', play: (c, v) => alertTone(c, { type: 'sine', from: 988, dur: 0.3, gain: 0.3 * v }) },
  bell: { label: 'Campainha', play: (c, v) => {
    alertTone(c, { type: 'triangle', from: 660, dur: 1.1, gain: 0.35 * v });
    alertTone(c, { type: 'sine', from: 1320, dur: 0.8, gain: 0.12 * v });
  } },
  none: { label: 'Sem sirene (so a voz)', play: () => {} },
};

function alertBeep(sirenKey) {
  const ctx = alertAudioCtx();
  if (!ctx || ctx.state !== 'running') return;
  const cfg = alertSoundCfg();
  (ALERT_SIRENS[sirenKey || cfg.siren] || ALERT_SIRENS.sweep).play(ctx, Math.max(0.05, Number(cfg.volume) || 0.8));
}

// ---- voz: "Atencao! Fulano precisa de ajuda." (sintese de voz do navegador)

let _alertNewList = [];              // alertas sem atendimento: sirene + voz repetindo
const _alertSpokenAt = new Map();    // id -> ultima vez que a voz falou dele
const _alertDuressSpoken = new Set(); // coacao: anuncia uma vez quando aparece
const ALERT_VOICE_REPEAT_MS = 15000;

function alertSpeechName(name) {
  // Nome em CAIXA ALTA a voz soletra; em "Titulo" ela le normal.
  return String(name || 'uma pessoa').toLowerCase().replace(/(^|\s)(\p{L})/gu, (m, sp, c) => sp + c.toUpperCase());
}

function alertVoices() {
  if (!('speechSynthesis' in window)) return [];
  const vs = speechSynthesis.getVoices() || [];
  const pt = vs.filter((v) => /^pt/i.test(v.lang));
  return pt.length ? pt : vs;
}

function alertVoice() {
  if (!('speechSynthesis' in window)) return null;
  const want = alertSoundCfg().voice;
  const vs = speechSynthesis.getVoices() || [];
  if (want && want !== 'off') {
    const chosen = vs.find((v) => v.voiceURI === want);
    if (chosen) return chosen;
  }
  return vs.find((v) => /pt-BR/i.test(v.lang) && /natural|online|francisca|thalita|antonio|google/i.test(v.name))
    || vs.find((v) => /pt-BR/i.test(v.lang))
    || vs.find((v) => /^pt/i.test(v.lang))
    || null;
}
if ('speechSynthesis' in window) speechSynthesis.onvoiceschanged = () => alertFillVoiceSelect();

function alertSpeak(text, force = false) {
  const cfg = alertSoundCfg();
  if (!('speechSynthesis' in window) || (!force && !cfg.enabled) || cfg.voice === 'off') return false;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = 'pt-BR';
  const v = alertVoice();
  if (v) u.voice = v;
  u.rate = Number(cfg.rate) || 1;
  u.volume = Math.min(1, Math.max(0.05, Number(cfg.volume) || 0.8));
  speechSynthesis.speak(u);
  return true;
}

function alertSpeechText(inc, cfg = alertSoundCfg()) {
  const tpl = inc.kind === 'duress' ? cfg.textDuress : cfg.textPanic;
  const unit = String(inc.member?.unit_name || '').trim();
  return String(tpl || '')
    .replace(/\{nome\}/gi, alertSpeechName(inc.member?.full_name))
    .replace(/\{local\}/gi, unit ? `${unit}.` : '')
    .replace(/\s+/g, ' ')
    .trim();
}

// Recebe a lista atual de alertas (tela ao vivo ou poll global) e decide o som.
function alertSetIncidentsForSound(incidents) {
  const list = incidents || [];
  _alertNewList = list.filter((i) => i.status === 'new');
  _alertNewList.forEach(alertNotify);
  list.filter((i) => i.kind === 'duress' && alertOpen(i)).forEach(alertNotify);
  const now = Date.now();
  _alertNewList.forEach((i) => {
    // Alerta novo: 2 toques de sirene e ja fala o nome.
    if (!_alertSpokenAt.has(i.id)) _alertSpokenAt.set(i.id, now - ALERT_VOICE_REPEAT_MS + 3000);
  });
  list.filter((i) => i.kind === 'duress' && alertOpen(i) && !_alertDuressSpoken.has(i.id)).forEach((i) => {
    _alertDuressSpoken.add(i.id);
    _alertSpokenAt.set(i.id, now);
    alertBeep();
    alertSpeak(alertSpeechText(i));
  });
  alertSetSiren(_alertNewList.length > 0);
  alertRenderSoundUnlock();
}

function alertSirenTick() {
  if ('speechSynthesis' in window && speechSynthesis.speaking) return; // nao atropela a voz
  const now = Date.now();
  const repeatMs = Math.max(5, Number(alertSoundCfg().repeat) || 15) * 1000;
  const due = _alertNewList.find((i) => now - (_alertSpokenAt.get(i.id) || 0) >= repeatMs);
  if (due && alertSoundOn()) {
    _alertSpokenAt.set(due.id, now);
    if (alertSpeak(alertSpeechText(due))) return;
  }
  alertBeep();
}

function alertSetSiren(on) {
  const want = on && alertSoundOn();
  if (want && !_alertSirenTimer) {
    _alertSirenTimer = true; // quem bate o compasso e o relogio de fundo (alertStartClock)
    alertSirenTick();
  } else if (!want) {
    _alertSirenTimer = null;
  }
}

// ---- aviso do Windows (Notification): aparece com o SightOps minimizado ou em outra aba
const _alertNotified = new Set();

function alertNotify(inc) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const key = `${inc.id}:${inc.kind}`;
  if (_alertNotified.has(key)) return;
  _alertNotified.add(key);
  if (!document.hidden && _currentView === 'alert-live') return; // ja esta olhando
  const m = inc.member || {};
  const duress = inc.kind === 'duress';
  try {
    const n = new Notification(duress ? 'COACAO — SightOps Alerta' : 'ALERTA DE PANICO — SightOps', {
      body: `${alertSpeechName(m.full_name)} ${duress ? 'esta sob coacao' : 'precisa de ajuda'}${m.unit_name ? ` · ${m.unit_name}` : ''}`,
      tag: key,
      requireInteraction: true,
    });
    n.onclick = () => { window.focus(); navigateTo('alert-live'); n.close(); };
  } catch { /* navegador sem suporte */ }
}

function alertNotifyStatusText() {
  if (!('Notification' in window)) return 'Este navegador nao suporta avisos do Windows.';
  if (Notification.permission === 'granted') return 'Avisos do Windows ATIVOS neste computador.';
  if (Notification.permission === 'denied') return 'Avisos bloqueados. Libere no cadeado ao lado do endereco do site.';
  return 'Avisos do Windows desligados.';
}

async function alertAskNotifyPermission() {
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default') {
    try { await Notification.requestPermission(); } catch {}
  }
  setText('alertNotifyStatus', alertNotifyStatusText());
}

// ---- som bloqueado pelo navegador: faixa "clique para ativar"

function alertSoundBlocked() {
  const noGesture = navigator.userActivation ? !navigator.userActivation.hasBeenActive : false;
  return noGesture || !_alertAudio || _alertAudio.state !== 'running';
}

function alertRenderSoundUnlock() {
  let el = document.getElementById('alertSoundUnlock');
  const show = alertSoundOn() && alertSoundBlocked() && (_currentView === 'alert-live' || _alertNewList.length > 0);
  if (!show) { if (el) el.hidden = true; return; }
  if (!el) {
    el = document.createElement('button');
    el.id = 'alertSoundUnlock';
    el.type = 'button';
    el.className = 'alert-sound-unlock';
    el.innerHTML = '<i data-lucide="volume-x"></i><span>O navegador bloqueou o som dos alertas. <b>Clique aqui para ativar.</b></span>';
    el.addEventListener('click', () => {
      alertAudioCtx();
      alertSpeak('Som dos alertas ativado.');
      setTimeout(alertRenderSoundUnlock, 400);
    });
    document.body.appendChild(el);
    if (window.lucide) lucide.createIcons();
  }
  el.hidden = false;
}

function alertTestSound(kind = 'panic', cfg = alertSoundCfg()) {
  alertAudioCtx();
  const fake = { kind, member: { full_name: _currentUser?.full_name || 'Maria da Silva', unit_name: 'Escola Presidente Dutra' } };
  const saved = alertSoundCfg();
  alertSaveSoundCfg(cfg); // testa exatamente o que esta na tela, mesmo antes de salvar
  alertBeep(cfg.siren);
  setTimeout(() => {
    if ('speechSynthesis' in window) speechSynthesis.cancel();
    const ok = cfg.voice === 'off' || alertSpeak(alertSpeechText(fake, cfg), true);
    alertSaveSoundCfg(saved);
    if (!ok) showToast('Este navegador nao tem voz. Use Chrome ou Edge atualizados.', true);
  }, cfg.siren === 'none' ? 0 : 900);
  setTimeout(alertRenderSoundUnlock, 500);
}

// ---- janela "Som do alerta"

function alertFillVoiceSelect() {
  const sel = document.getElementById('alertSoundVoice');
  if (!sel) return;
  const cur = sel.dataset.value ?? alertSoundCfg().voice;
  const opts = ['<option value="">Automatica (portugues)</option>', '<option value="off">Sem voz (so sirene)</option>']
    .concat(alertVoices().map((v) => `<option value="${esc(v.voiceURI)}">${esc(v.name)} (${esc(v.lang)})</option>`));
  sel.innerHTML = opts.join('');
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : '';
}

function alertSoundFormCfg() {
  const g = (id) => document.getElementById(id);
  return {
    enabled: g('alertSoundEnabled').checked,
    siren: g('alertSoundSiren').value,
    voice: g('alertSoundVoice').value,
    rate: Number(g('alertSoundRate').value),
    volume: Number(g('alertSoundVolume').value) / 100,
    repeat: Number(g('alertSoundRepeat').value),
    textPanic: g('alertSoundTextPanic').value.trim() || ALERT_SOUND_DEFAULTS.textPanic,
    textDuress: g('alertSoundTextDuress').value.trim() || ALERT_SOUND_DEFAULTS.textDuress,
  };
}

function alertOpenSoundModal() {
  const cfg = alertSoundCfg();
  const g = (id) => document.getElementById(id);
  g('alertSoundEnabled').checked = cfg.enabled !== false;
  g('alertSoundSiren').innerHTML = Object.entries(ALERT_SIRENS)
    .map(([k, s]) => `<option value="${k}">${esc(s.label)}</option>`).join('');
  g('alertSoundSiren').value = cfg.siren;
  g('alertSoundVoice').dataset.value = cfg.voice;
  alertFillVoiceSelect();
  delete g('alertSoundVoice').dataset.value;
  g('alertSoundRate').value = String(cfg.rate);
  g('alertSoundVolume').value = String(Math.round((Number(cfg.volume) || 0.8) * 100));
  g('alertSoundRepeat').value = String(cfg.repeat);
  g('alertSoundTextPanic').value = cfg.textPanic;
  g('alertSoundTextDuress').value = cfg.textDuress;
  setText('alertNotifyStatus', alertNotifyStatusText());
  g('modalAlertSound').classList.remove('hidden');
  alertAudioCtx();
}

function alertBindSoundModal() {
  const g = (id) => document.getElementById(id);
  g('btnAlertNotifyEnable')?.addEventListener('click', alertAskNotifyPermission);
  g('btnAlertSoundTestPanic')?.addEventListener('click', () => alertTestSound('panic', alertSoundFormCfg()));
  g('btnAlertSoundTestDuress')?.addEventListener('click', () => alertTestSound('duress', alertSoundFormCfg()));
  g('btnAlertSoundDefaults')?.addEventListener('click', () => {
    alertSaveSoundCfg({ ...ALERT_SOUND_DEFAULTS, enabled: g('alertSoundEnabled').checked });
    alertOpenSoundModal();
  });
  g('btnAlertSoundSave')?.addEventListener('click', () => {
    alertSaveSoundCfg(alertSoundFormCfg());
    if (!alertSoundOn() && 'speechSynthesis' in window) speechSynthesis.cancel();
    alertCloseModal('modalAlertSound');
    alertRenderSoundButton();
    alertSetSiren(false);
    alertSetIncidentsForSound(_alertIncidents);
    showToast('Som do alerta salvo neste computador.');
  });
}

function alertRenderSoundButton() {
  const btn = document.getElementById('btnAlertSound');
  if (!btn) return;
  const on = alertSoundOn();
  btn.querySelector('span').textContent = on ? 'Som do alerta' : 'Som desligado';
  btn.classList.toggle('alert-sound-off', !on);
}

// ---------------------------------------------------------------- badge global

async function alertPollSummary() {
  const btn = document.querySelector('.nav-item[data-view="alert-live"]');
  // Depois de F5 a sessao segue pelo cookie e _token fica null; por isso o
  // criterio e ter usuario carregado, nao ter token em memoria.
  if (!_currentUser || !btn || btn.classList.contains('nav-item-hidden')) return;
  try {
    const res = await api('/api/alert/summary', { skipLogout: true });
    if (!res || !res.ok) return;
    const s = await res.json();
    const badge = document.getElementById('alertNavBadge');
    if (badge) {
      badge.hidden = !s.open;
      badge.textContent = String(s.open || 0);
      badge.classList.toggle('alert-nav-badge-hot', (s.new || 0) > 0);
    }
    // Fora da tela ao vivo o som depende deste poll; dentro dela, do refresh.
    if (_currentView !== 'alert-live') {
      if ((s.new || 0) > 0 || (s.duress || 0) > 0) {
        const live = await api('/api/alert/incidents?scope=live&limit=50', { skipLogout: true });
        const data = live && live.ok ? await live.json() : { incidents: [] };
        alertSetIncidentsForSound(data.incidents || []);
      } else {
        alertSetIncidentsForSound([]);
      }
    }
    if (s.latest_new_id && !_alertKnownIds.has(s.latest_new_id) && _currentView !== 'alert-live') {
      _alertKnownIds.add(s.latest_new_id);
      showToast('ALERTA DE PANICO recebido. Abra Alerta ao Vivo.', true);
    }
  } catch { /* silencioso: e so o badge */ }
}

// Relogio unico do alerta: sirene a cada 1,5s e consulta ao servidor a cada
// ~10s. Roda num Web Worker porque o Chrome segura setInterval de aba em
// segundo plano (ate 1x por minuto) -- e a central costuma deixar o SightOps
// minimizado ou atras de outra aba.
function alertStartClock() {
  if (_alertSummaryTimer) return;
  let n = 0;
  const everyPoll = Math.max(1, Math.round(ALERT_SUMMARY_MS / 1500));
  const onTick = () => {
    n += 1;
    if (_alertSirenTimer) alertSirenTick();
    if (n % everyPoll === 0) alertPollSummary();
  };
  try {
    const src = 'setInterval(function(){postMessage(1)},1500)';
    const w = new Worker(URL.createObjectURL(new Blob([src], { type: 'text/javascript' })));
    w.onmessage = onTick;
    // Worker bloqueado (CSP) falha por evento, nao por excecao: cai pro setInterval.
    w.onerror = () => { try { w.terminate(); } catch {} _alertSummaryTimer = setInterval(onTick, 1500); };
    _alertSummaryTimer = w;
  } catch {
    _alertSummaryTimer = setInterval(onTick, 1500);
  }
  setTimeout(alertPollSummary, 3000);
}
alertStartClock();

// ---------------------------------------------------------------- tela ao vivo

function alertLiveVisible() {
  return !document.getElementById('viewAlertLive')?.classList.contains('hidden');
}

function alertInitMap() {
  if (_alertMap || typeof L === 'undefined') return;
  _alertMap = L.map('alertLiveMap', { zoomControl: true }).setView([-10.186, -36.825], 14);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap', subdomains: 'abc', maxZoom: 19,
  }).addTo(_alertMap);
  _alertMarkers = L.layerGroup().addTo(_alertMap);
  _alertTrail = L.layerGroup().addTo(_alertMap);
}

function alertBind() {
  if (_alertBound) return;
  _alertBound = true;
  document.getElementById('btnAlertRefresh')?.addEventListener('click', () => alertRefresh(true));
  document.getElementById('btnAlertSound')?.addEventListener('click', alertOpenSoundModal);
  document.getElementById('btnAlertSoundTest')?.addEventListener('click', () => alertTestSound('panic'));
  alertBindSoundModal();
  document.getElementById('btnAlertHistory')?.addEventListener('click', () => {
    _alertScope = _alertScope === 'live' ? 'history' : 'live';
    const btn = document.getElementById('btnAlertHistory');
    btn.querySelector('span').textContent = _alertScope === 'live' ? 'Historico' : 'Voltar ao vivo';
    setText('alertListTitle', _alertScope === 'live' ? 'Alertas' : 'Historico');
    setText('alertListSub', _alertScope === 'live' ? 'Abertos e encerrados recentes.' : 'Todos os alertas, mais recentes primeiro.');
    _alertFittedOnce = false;
    alertRefresh(true);
  });
  document.getElementById('alertLiveList')?.addEventListener('click', (ev) => {
    const card = ev.target.closest('[data-alert-id]');
    if (card) alertSelect(card.dataset.alertId, true);
  });
  document.getElementById('alertLiveDetail')?.addEventListener('click', alertDetailClick);
  document.getElementById('btnAlertCloseConfirm')?.addEventListener('click', alertConfirmClose);
  alertBindModalClose();
}

let _alertModalCloseBound = false;
function alertBindModalClose() {
  if (_alertModalCloseBound) return;
  _alertModalCloseBound = true;
  document.querySelectorAll('[data-alert-close]').forEach((btn) => {
    btn.addEventListener('click', () => alertCloseModal(btn.dataset.alertClose));
  });
}

function alertCloseModal(id) {
  document.getElementById(id)?.classList.add('hidden');
  if (id === 'modalAlertCamera' && _alertCamHandle) { _alertCamHandle.stop(); _alertCamHandle = null; }
}

async function loadAlertLive() {
  alertBind();
  alertInitMap();
  alertAudioCtx(); // abrir a tela e um clique: aproveita pra liberar o audio
  alertRenderSoundButton();
  setTimeout(alertRenderSoundUnlock, 300);
  setTimeout(() => _alertMap?.invalidateSize(), 150);
  await alertRefresh(true);
  if (!_alertTimer) {
    _alertTimer = setInterval(() => {
      if (!alertLiveVisible()) { clearInterval(_alertTimer); _alertTimer = null; return; }
      alertRefresh(false);
    }, ALERT_REFRESH_MS);
  }
}

async function alertRefresh(force) {
  if (_alertLoading && !force) return;
  _alertLoading = true;
  try {
    const data = await alertApiJson(`/api/alert/incidents?scope=${_alertScope}&limit=200`);
    const incidents = data.incidents || [];
    const fresh = incidents.filter((i) => i.status === 'new' && !_alertKnownIds.has(i.id));
    incidents.forEach((i) => _alertKnownIds.add(i.id));
    _alertIncidents = incidents;
    setText('alertLiveState', `atualizado ${alertClock(new Date().toISOString())}`);
    if (fresh.length) {
      // Alerta novo chegou: seleciona e centraliza nele.
      _alertSelectedId = fresh[0].id;
      _alertFittedOnce = false;
    } else if (!_alertSelectedId || !incidents.some((i) => i.id === _alertSelectedId)) {
      _alertSelectedId = (incidents.find(alertOpen) || {}).id || '';
    }
    alertRenderKpis();
    alertRenderList();
    alertRenderMarkers();
    alertSetIncidentsForSound(incidents);
    await alertLoadDetail();
  } catch (err) {
    setText('alertLiveState', 'falha ao atualizar');
    if (force) showToast(err.message || 'Falha ao carregar alertas.', true);
  } finally {
    _alertLoading = false;
  }
}

function alertRenderKpis() {
  const open = _alertIncidents.filter(alertOpen);
  setText('alertKpiNew', open.filter((i) => i.status === 'new').length);
  setText('alertKpiOpen', open.filter((i) => i.status !== 'new').length);
  setText('alertKpiDuress', open.filter((i) => i.kind === 'duress').length);
  setText('alertKpiClosed', _alertIncidents.filter((i) => !alertOpen(i)).length);
  setText('alertKpiClosedSub', _alertScope === 'live' ? 'ultimos 30 minutos' : 'no historico carregado');
}

function alertRenderList() {
  const box = document.getElementById('alertLiveList');
  if (!box) return;
  if (!_alertIncidents.length) {
    box.innerHTML = `<div class="alert-live-empty">${_alertScope === 'live'
      ? 'Nenhum alerta aberto. Quando alguem apertar o botao de panico, aparece aqui com som.'
      : 'Nenhum alerta registrado ainda.'}</div>`;
    return;
  }
  box.innerHTML = _alertIncidents.map((inc) => {
    const m = inc.member || {};
    const cls = [
      'alert-card',
      inc.id === _alertSelectedId ? 'selected' : '',
      inc.status === 'new' ? 'is-new' : '',
      inc.kind === 'duress' && alertOpen(inc) ? 'is-duress' : '',
      alertOpen(inc) ? '' : 'is-final',
    ].filter(Boolean).join(' ');
    return `
      <button type="button" class="${cls}" data-alert-id="${esc(inc.id)}">
        <div class="alert-card-top">
          <strong>${esc(m.full_name)}</strong>
          <span class="alert-card-age">${esc(alertAgo(inc.opened_at))}</span>
        </div>
        <div class="alert-card-meta">${esc([m.role_title, m.unit_name].filter(Boolean).join(' · ') || 'sem lotacao')}</div>
        <div class="alert-card-pills">${alertKindPill(inc)} ${alertStatusPill(inc)}${alertPoint(inc)?.gps === false ? ' <span class="pill neutral">sem GPS</span>' : ''}</div>
      </button>`;
  }).join('');
}

function alertMarkerIcon(inc, selected) {
  const cls = ['alert-marker', `st-${inc.status}`, inc.kind === 'duress' ? 'duress' : '', selected ? 'selected' : ''].join(' ');
  return L.divIcon({ className: '', html: `<span class="${cls}"></span>`, iconSize: [22, 22], iconAnchor: [11, 11] });
}

function alertRenderMarkers() {
  if (!_alertMap) return;
  _alertMarkers.clearLayers();
  const pts = [];
  _alertIncidents.forEach((inc) => {
    const p = alertPoint(inc);
    if (!p) return;
    // No ao vivo, encerrado so aparece se estiver selecionado (nao polui o mapa).
    if (!alertOpen(inc) && inc.id !== _alertSelectedId) return;
    const mk = L.marker([p.lat, p.lon], { icon: alertMarkerIcon(inc, inc.id === _alertSelectedId), zIndexOffset: inc.status === 'new' ? 1000 : 0 });
    mk.bindTooltip(`${esc(inc.member?.full_name || '')}${p.gps ? '' : ' (lotacao, sem GPS)'}`);
    mk.on('click', () => alertSelect(inc.id, true));
    mk.addTo(_alertMarkers);
    pts.push([p.lat, p.lon]);
    if (p.gps && inc.last_accuracy && inc.id === _alertSelectedId) {
      L.circle([p.lat, p.lon], { radius: Math.min(inc.last_accuracy, 500), color: '#c92a2a', weight: 1, fillOpacity: 0.08 }).addTo(_alertMarkers);
    }
  });
  if (!_alertFittedOnce && pts.length) {
    const sel = alertPoint(_alertIncidents.find((i) => i.id === _alertSelectedId));
    if (sel) _alertMap.setView([sel.lat, sel.lon], 17);
    else _alertMap.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
    _alertFittedOnce = true;
  }
}

function alertSelect(id, center) {
  _alertSelectedId = id;
  alertRenderList();
  alertRenderMarkers();
  const p = alertPoint(_alertIncidents.find((i) => i.id === id));
  if (center && p && _alertMap) _alertMap.setView([p.lat, p.lon], Math.max(_alertMap.getZoom(), 16));
  alertLoadDetail();
}

async function alertLoadDetail() {
  const panel = document.getElementById('alertLiveDetail');
  if (!panel) return;
  if (!_alertSelectedId) {
    panel.hidden = true;
    _alertTrail?.clearLayers();
    _alertDetail = null;
    return;
  }
  try {
    const data = await alertApiJson(`/api/alert/incidents/${encodeURIComponent(_alertSelectedId)}`);
    _alertDetail = data.incident;
  } catch {
    return;
  }
  alertRenderTrail();
  alertRenderDetail();
  alertLoadNearby();
}

function alertRenderTrail() {
  if (!_alertTrail) return;
  _alertTrail.clearLayers();
  const pts = (_alertDetail?.positions || []).map((p) => [p.lat, p.lon]);
  if (pts.length > 1) L.polyline(pts, { color: '#c92a2a', weight: 3, opacity: 0.7, dashArray: '6 6' }).addTo(_alertTrail);
  if (pts.length) L.circleMarker(pts[0], { radius: 5, color: '#172026', weight: 2, fillColor: '#fff', fillOpacity: 1 })
    .bindTooltip('ponto do disparo').addTo(_alertTrail);
}

const ALERT_LOG_LABEL = {
  opened: 'Botao de panico acionado',
  retriggered: 'Botao acionado de novo',
  duress: 'SENHA DE COACAO digitada',
  cancelled_by_user: 'Cancelado pela pessoa (senha normal)',
  acknowledge: 'Assumido',
  dispatch: 'Equipe enviada',
  note: 'Observacao',
  close: 'Encerrado',
  escalated: 'Aviso enviado ao Telegram',
};

function alertRenderDetail() {
  const panel = document.getElementById('alertLiveDetail');
  const inc = _alertDetail;
  if (!panel || !inc) return;
  const m = inc.member || {};
  const p = alertPoint(inc);
  const open = alertOpen(inc);
  const posAge = inc.last_position_at ? `${alertClock(inc.last_position_at)} (ha ${alertAgo(inc.last_position_at)})` : 'nenhuma';
  const mapsUrl = p ? `https://www.google.com/maps?q=${p.lat},${p.lon}` : '';
  const actions = open ? `
      ${inc.status === 'new' ? '<button class="primary-action alert-btn-big" data-alert-act="acknowledge" type="button"><i data-lucide="hand"></i> Assumir alerta</button>' : ''}
      ${inc.status !== 'dispatched' ? '<button class="secondary-action" data-alert-act="dispatch" type="button"><i data-lucide="car"></i> Equipe enviada</button>' : ''}
      <button class="secondary-action" data-alert-act="note" type="button"><i data-lucide="message-square-plus"></i> Observacao</button>
      <button class="secondary-action" data-alert-act="close" type="button"><i data-lucide="check-circle-2"></i> Encerrar</button>` : '';
  const duressBanner = inc.kind === 'duress'
    ? `<div class="alert-duress-banner"><i data-lucide="user-x"></i><div><b>COACAO</b> — a pessoa digitou a senha de coacao as ${esc(alertClock(inc.duress_at))}.
       No celular dela aparece "cancelado", mas o rastreamento continua. <b>Nao ligue para ela:</b> alguem pode estar junto.</div></div>` : '';
  const log = (inc.log || []).slice().reverse().map((l) => `
      <li><span>${esc(alertClock(l.created_at))}</span><div><b>${esc(ALERT_LOG_LABEL[l.action] || l.action)}</b>
      ${l.actor ? `<small>${esc(l.actor)}</small>` : ''}${l.note && l.action !== 'opened' ? `<p>${esc(l.note)}</p>` : ''}</div></li>`).join('');

  panel.hidden = false;
  panel.innerHTML = `
    <div class="alert-detail-head">
      <div>
        <div class="alert-detail-pills">${alertKindPill(inc)} ${alertStatusPill(inc)}</div>
        <h2>${esc(m.full_name)}</h2>
        <p>${esc([m.role_title, m.unit_name].filter(Boolean).join(' · ') || 'sem lotacao cadastrada')}</p>
      </div>
      <div class="alert-detail-actions">${actions}</div>
    </div>
    ${duressBanner}
    <div class="alert-detail-grid">
      <div class="alert-facts">
        <span><small>Disparo</small><b>${esc(alertClock(inc.opened_at))} (ha ${esc(alertAgo(inc.opened_at))})</b></span>
        <span><small>Telefone</small><b>${m.phone ? `<a href="tel:${esc(m.phone)}">${esc(alertPhone(m.phone))}</a>` : '-'}</b></span>
        <span><small>CPF</small><b>${esc(alertCpf(m.document_id)) || '-'}</b></span>
        <span><small>Ultima posicao</small><b>${esc(posAge)}</b></span>
        <span><small>Precisao GPS</small><b>${inc.last_accuracy != null ? `~${Math.round(inc.last_accuracy)} m` : (p && !p.gps ? 'sem GPS (lotacao)' : '-')}</b></span>
        <span><small>Bateria</small><b>${inc.last_battery != null ? `${inc.last_battery}%` : '-'}</b></span>
        ${inc.acknowledged_by ? `<span><small>Assumido por</small><b>${esc(inc.acknowledged_by)} as ${esc(alertClock(inc.acknowledged_at))}</b></span>` : ''}
        ${inc.close_note ? `<span class="wide"><small>Encerramento</small><b>${esc(inc.close_note)}</b></span>` : ''}
        ${mapsUrl ? `<span class="wide"><a class="link-action" href="${esc(mapsUrl)}" target="_blank" rel="noopener"><i data-lucide="map-pin"></i> Abrir no Google Maps</a></span>` : ''}
      </div>
      <div class="alert-timeline"><h3>Linha do tempo</h3><ul>${log || '<li><div>Sem registros.</div></li>'}</ul></div>
    </div>
    <div class="alert-nearby" id="alertNearbyGrid"><div class="alert-live-empty">Buscando cameras...</div></div>`;
  if (window.lucide) lucide.createIcons();
}

// Distancia legivel: metro ate 1 km, depois km com uma casa.
function alertDistLabel(m) {
  if (m == null) return '';
  const n = Number(m);
  if (!isFinite(n)) return '';
  return n >= 1000 ? `${(n / 1000).toFixed(1)} km` : `${Math.round(n)} m`;
}

function alertCameraCard(c) {
  // A distancia e o que decide qual camera abrir primeiro numa ocorrencia --
  // entao sai do meio do texto miudo e ganha destaque proprio.
  const d = alertDistLabel(c.distance_m);
  const dist = d
    ? `<b style="color:var(--primary);font-weight:600">${esc(d)}</b> · `
    : '';
  return `
      <div class="alert-cam">
        <div class="alert-cam-thumb">${c.snapshot_url ? `<img src="${esc(c.snapshot_url)}" alt="" loading="lazy">` : '<i data-lucide="cctv"></i>'}</div>
        <div class="alert-cam-copy">
          <strong title="${esc(c.titulo || c.ip)}">${esc(c.titulo || c.ip)}</strong>
          <small>${dist}${esc(c.local || c.ip)}</small>
        </div>
        <button class="secondary-action" type="button" data-alert-cam="${esc(c.ip)}" data-alert-cam-title="${esc(c.titulo || c.ip)}"
          data-alert-cam-vendor="${esc(c.fabricante)}" data-alert-cam-model="${esc(c.modelo)}"
          data-alert-cam-connector="${esc(c.remote_connector_id || '')}"><i data-lucide="play"></i> Ao vivo</button>
      </div>`;
}

// Agrupa por site. Numa ocorrencia a central precisa saber de ONDE e cada
// camera; uma grade solta mistura sites vizinhos sem dizer qual e qual.
// Com um site so, nao poe cabecalho -- nao ajuda em nada e rouba espaco.
function alertCamerasPorSite(cams) {
  const porSite = new Map();
  cams.forEach((c) => {
    const site = (c.site || c.local || '').trim() || 'Sem site';
    if (!porSite.has(site)) porSite.set(site, []);
    porSite.get(site).push(c);
  });
  if (porSite.size <= 1) {
    return `<div class="alert-nearby-grid">${cams.map(alertCameraCard).join('')}</div>`;
  }
  return [...porSite.entries()]
    .sort((a, b) => a[0].localeCompare(b[0], 'pt-BR'))
    .map(([site, lista]) => `<div class="alert-site-group" style="margin-bottom:10px">
        <h4 class="alert-site-title" style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:6px 0 4px">${esc(site)} <span class="alert-count">${lista.length}</span></h4>
        <div class="alert-nearby-grid">${lista.map(alertCameraCard).join('')}</div>
      </div>`)
    .join('');
}

// Duas fontes: TODAS as cameras do local monitorado da pessoa (a escola, mesmo
// sem coordenada) e as proximas pelo GPS (rua). Sem repetir camera.
async function alertLoadNearby() {
  const box = document.getElementById('alertNearbyGrid');
  if (!box || !_alertDetail) return;
  const p = alertPoint(_alertDetail);
  const cid = _alertDetail.member?.unit_connector_id || '';
  // So refaz a busca quando a pessoa andou ~50 m (3 casas decimais de lat/lon).
  const key = `${_alertDetail.id}:${cid}:${p ? `${p.lat.toFixed(3)}:${p.lon.toFixed(3)}` : '-'}`;
  if (key === _alertNearbyKey && _alertNearbyHtml) { box.innerHTML = _alertNearbyHtml; if (window.lucide) lucide.createIcons(); return; }
  const qs = p ? `lat=${p.lat}&lon=${p.lon}${p.acc != null ? `&accuracy_m=${encodeURIComponent(p.acc)}` : ''}` : '';
  try {
    await alertLoadConnectors();
    const [unit, near] = await Promise.all([
      cid ? alertApiJson(`/api/alert/unit-cameras?connector_id=${encodeURIComponent(cid)}${qs ? `&${qs}` : ''}`) : Promise.resolve({ cameras: [] }),
      p ? alertApiJson(`/api/alert/nearby-cameras?${qs}&limit=6`) : Promise.resolve({ cameras: [] }),
    ]);
    const unitCams = unit.cameras || [];
    const seen = new Set(unitCams.map((c) => `${c.remote_connector_id}|${c.ip}`));
    const nearCams = (near.cameras || []).filter((c) => !seen.has(`${c.remote_connector_id}|${c.ip}`));
    let html = '';
    if (cid) {
      html += `<h3>Cameras da lotacao — ${esc(alertConnectorName(cid))} <span class="alert-count">${unitCams.length}</span></h3>
        ${unitCams.length ? alertCamerasPorSite(unitCams) : '<div class="alert-nearby-grid"><div class="alert-live-empty">Nenhuma camera cadastrada neste local.</div></div>'}`;
    }
    // Sem camera no raio, diz isso com todas as letras -- e informa a
    // distancia da mais proxima, que muda a decisao de quem atende.
    const raio = Math.round(near.radius_m || 20);
    let vazio;
    if (!p) {
      vazio = 'Sem localizacao para buscar cameras.';
    } else if (near.nearest && near.nearest.distance_m != null) {
      const d = Math.round(near.nearest.distance_m);
      const dist = d >= 1000 ? `${(d / 1000).toFixed(1)} km` : `${d} m`;
      vazio = `Nenhuma camera num raio de ${raio} m do ponto do alerta. A mais proxima (${esc(near.nearest.titulo || near.nearest.ip || '')}) esta a ${dist}.`;
    } else {
      vazio = `Nenhuma camera num raio de ${raio} m do ponto do alerta.`;
    }
    // Quando o raio foi ampliado, diz por que -- senao parece que o filtro
    // simplesmente nao funcionou.
    const nota = near.widened
      ? `<div class="alert-nearby-note" style="font-size:12px;color:var(--muted);margin:2px 0 8px">Raio ampliado para ${raio} m: o aparelho informou precisao de ~${Math.round(near.accuracy_m || 0)} m.</div>`
      : '';
    html += `<h3 class="${cid ? 'alert-nearby-second' : ''}">No local do alerta <span class="alert-count">${nearCams.length}</span></h3>${nota}
      ${nearCams.length ? alertCamerasPorSite(nearCams)
        : `<div class="alert-nearby-grid"><div class="alert-live-empty">${vazio}</div></div>`}`;
    _alertNearbyKey = key;
    _alertNearbyHtml = html;
    const target = document.getElementById('alertNearbyGrid');
    if (!target) return;
    target.innerHTML = html;
    if (window.lucide) lucide.createIcons();
  } catch {
    box.innerHTML = '<div class="alert-live-empty">Falha ao buscar cameras.</div>';
  }
}

async function alertDetailClick(ev) {
  const cam = ev.target.closest('[data-alert-cam]');
  if (cam) { alertOpenCamera(cam.dataset); return; }
  const act = ev.target.closest('[data-alert-act]')?.dataset.alertAct;
  if (!act || !_alertDetail) return;
  if (act === 'close') {
    document.getElementById('alertCloseNote').value = '';
    document.getElementById('modalAlertClose').classList.remove('hidden');
    setTimeout(() => document.getElementById('alertCloseNote')?.focus(), 50);
    return;
  }
  let note = '';
  if (act === 'note') {
    note = prompt('Observacao para o historico deste alerta:') || '';
    if (!note.trim()) return;
  }
  await alertAction(act, note);
}

async function alertAction(action, note) {
  try {
    await alertApiJson(`/api/alert/incidents/${encodeURIComponent(_alertDetail.id)}/${action}`, {
      method: 'POST', body: JSON.stringify({ note }),
    });
    await alertRefresh(true);
    return true;
  } catch (err) {
    showToast(err.message || 'Falha na acao.', true);
    return false;
  }
}

async function alertConfirmClose() {
  const note = document.getElementById('alertCloseNote').value.trim();
  if (note.length < 3) { showToast('Descreva o que foi feito antes de encerrar.', true); return; }
  if (await alertAction('close', note)) alertCloseModal('modalAlertClose');
}

function alertOpenCamera(ds) {
  const modal = document.getElementById('modalAlertCamera');
  const video = document.getElementById('alertCameraVideo');
  const status = document.getElementById('alertCameraStatus');
  if (!modal || !video || typeof mountLiveStream !== 'function') return;
  setText('alertCameraTitle', ds.alertCamTitle || ds.alertCam);
  setText('alertCameraSub', ds.alertCam);
  status.textContent = 'Conectando...';
  status.hidden = false;
  modal.classList.remove('hidden');
  if (_alertCamHandle) _alertCamHandle.stop();
  // Senha vazia: o servidor usa a credencial ja salva desta camera/site.
  _alertCamHandle = mountLiveStream(video, {
    ip: ds.alertCam, user: 'admin', pass: '', subtype: 1,
    vendor: ds.alertCamVendor || '', model: ds.alertCamModel || '', connectorId: ds.alertCamConnector || '',
    onStatus: (texto) => {
      if (texto === 'credential_required') {
        status.textContent = 'Camera sem senha salva no SightOps. Abra pelo inventario de cameras e informe a senha.';
        status.hidden = false;
        return;
      }
      status.textContent = texto || '';
      status.hidden = !texto;
    },
  });
}

// ---------------------------------------------------------------- pessoas

async function loadAlertMembers() {
  if (!_alertMembersBound) {
    _alertMembersBound = true;
    let t = null;
    document.getElementById('alertMembersSearch')?.addEventListener('input', () => {
      clearTimeout(t); t = setTimeout(alertLoadMembers, 250);
    });
    document.getElementById('btnAlertMemberNew')?.addEventListener('click', () => alertOpenMember(null));
    document.getElementById('btnAlertMemberSave')?.addEventListener('click', alertSaveMember);
    document.getElementById('btnAlertAppOpen')?.addEventListener('click', () => {
      window.open(new URL('alerta/', window.location.href).href, '_blank', 'noopener');
    });
    document.getElementById('btnAlertCodeCopy')?.addEventListener('click', () => {
      const code = document.getElementById('alertCodeValue')?.textContent || '';
      navigator.clipboard?.writeText(code).then(() => showToast('Codigo copiado.'), () => {});
    });
    document.getElementById('alertMemberLat')?.addEventListener('paste', (ev) => {
      // "-10.18, -36.82" colado do Google Maps: separa nos dois campos.
      const txt = (ev.clipboardData || window.clipboardData)?.getData('text') || '';
      const m = txt.match(/(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)/);
      if (!m) return;
      ev.preventDefault();
      document.getElementById('alertMemberLat').value = m[1];
      document.getElementById('alertMemberLon').value = m[2];
    });
    document.getElementById('alertMembersBody')?.addEventListener('click', alertMembersClick);
    alertBindModalClose();
  }
  await alertLoadMembers();
}

async function alertLoadMembers() {
  const body = document.getElementById('alertMembersBody');
  await alertLoadConnectors();
  const q = document.getElementById('alertMembersSearch')?.value.trim() || '';
  try {
    const data = await alertApiJson(`/api/alert/members?search=${encodeURIComponent(q)}`);
    _alertMembers = data.members || [];
  } catch (err) {
    body.innerHTML = `<tr class="empty-row"><td colspan="8">${esc(err.message || 'Falha ao carregar.')}</td></tr>`;
    return;
  }
  if (!_alertMembers.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="8">${q ? 'Ninguem encontrado.' : 'Nenhuma pessoa cadastrada. Clique em "Nova pessoa".'}</td></tr>`;
    return;
  }
  body.innerHTML = _alertMembers.map((m) => {
    const app = m.app_devices
      ? `<span class="pill success">${m.app_devices} aparelho${m.app_devices > 1 ? 's' : ''}</span>${m.pins_configured ? '' : ' <span class="pill amber">sem senha</span>'}`
      : (m.activation_pending ? '<span class="pill amber">codigo gerado</span>' : '<span class="pill neutral">nao ativado</span>');
    return `
      <tr>
        <td><strong>${esc(m.full_name)}</strong></td>
        <td class="monospace">${esc(alertCpf(m.document_id))}</td>
        <td>${esc(m.role_title || '-')}</td>
        <td title="${esc(alertConnectorName(m.unit_connector_id))}">${esc(m.unit_name || '-')}${m.unit_lat == null ? '' : ' <i data-lucide="map-pin" class="alert-inline-icon" title="lotacao com coordenada"></i>'}${m.unit_connector_id ? ' <i data-lucide="cctv" class="alert-inline-icon" title="cameras do local monitorado"></i>' : ''}</td>
        <td>${esc(alertPhone(m.phone) || '-')}</td>
        <td>${app}</td>
        <td>${m.active ? '<span class="pill success">ativo</span>' : '<span class="pill neutral">inativo</span>'}</td>
        <td><div class="alert-row-actions">
          <button class="secondary-action" data-member-act="code" data-id="${esc(m.id)}" type="button" ${m.active ? '' : 'disabled'}><i data-lucide="key-round"></i> Codigo</button>
          <button class="icon-button" data-member-act="edit" data-id="${esc(m.id)}" type="button" title="Editar"><i data-lucide="pencil"></i></button>
          <button class="icon-button" data-member-act="revoke" data-id="${esc(m.id)}" type="button" title="Desconectar aparelhos" ${m.app_devices ? '' : 'disabled'}><i data-lucide="unplug"></i></button>
          <button class="icon-button" data-member-act="delete" data-id="${esc(m.id)}" type="button" title="Excluir"><i data-lucide="trash-2"></i></button>
        </div></td>
      </tr>`;
  }).join('');
  if (window.lucide) lucide.createIcons();
}

async function alertFillConnectorSelect(selected) {
  const sel = document.getElementById('alertMemberConnector');
  if (!sel) return;
  const list = await alertLoadConnectors();
  sel.innerHTML = '<option value="">Nenhum</option>' + list.map((c) =>
    `<option value="${esc(c.id)}"${c.id === selected ? ' selected' : ''}>${esc(c.name)}</option>`).join('');
  if (selected && !list.some((c) => c.id === selected)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${esc(selected)}" selected>(conector removido)</option>`);
  }
}

function alertOpenMember(m) {
  const v = (id, val) => { document.getElementById(id).value = val ?? ''; };
  setText('alertMemberModalTitle', m ? 'Editar pessoa' : 'Nova pessoa');
  v('alertMemberId', m?.id);
  v('alertMemberName', m?.full_name);
  v('alertMemberCpf', m ? alertCpf(m.document_id) : '');
  v('alertMemberRole', m?.role_title);
  v('alertMemberPhone', m ? alertPhone(m.phone) : '');
  v('alertMemberUnit', m?.unit_name);
  v('alertMemberLat', m?.unit_lat);
  v('alertMemberLon', m?.unit_lon);
  v('alertMemberNotes', m?.notes);
  document.getElementById('alertMemberActive').checked = m ? !!m.active : true;
  alertFillConnectorSelect(m?.unit_connector_id || '');
  document.getElementById('modalAlertMember').classList.remove('hidden');
  setTimeout(() => document.getElementById('alertMemberName')?.focus(), 50);
}

async function alertSaveMember() {
  const g = (id) => document.getElementById(id).value.trim();
  const payload = {
    id: g('alertMemberId'),
    full_name: g('alertMemberName'),
    document_id: g('alertMemberCpf'),
    role_title: g('alertMemberRole'),
    phone: g('alertMemberPhone'),
    unit_name: g('alertMemberUnit'),
    unit_connector_id: document.getElementById('alertMemberConnector').value,
    unit_lat: g('alertMemberLat') || null,
    unit_lon: g('alertMemberLon') || null,
    notes: g('alertMemberNotes'),
    active: document.getElementById('alertMemberActive').checked,
  };
  try {
    await alertApiJson('/api/alert/members', { method: 'POST', body: JSON.stringify(payload) });
    alertCloseModal('modalAlertMember');
    showToast('Pessoa salva.');
    await alertLoadMembers();
  } catch (err) {
    showToast(err.message || 'Falha ao salvar.', true);
  }
}

async function alertMembersClick(ev) {
  const btn = ev.target.closest('[data-member-act]');
  if (!btn || btn.disabled) return;
  const m = _alertMembers.find((x) => x.id === btn.dataset.id);
  if (!m) return;
  const act = btn.dataset.memberAct;
  try {
    if (act === 'edit') { alertOpenMember(m); return; }
    if (act === 'code') {
      if (m.app_devices && !(await showConfirm({
        eyebrow: 'Codigo de ativacao', title: 'Pessoa ja tem aparelho', label: 'Gerar codigo', danger: false,
        msg: `${m.full_name} ja tem aparelho ativado. Gerar codigo para ativar mais um (ou um celular novo)? O aparelho atual continua funcionando.`,
      }))) return;
      const out = await alertApiJson(`/api/alert/members/${encodeURIComponent(m.id)}/activation-code`, { method: 'POST' });
      setText('alertCodeWho', m.full_name);
      setText('alertCodeValue', `${out.code.slice(0, 4)}-${out.code.slice(4)}`);
      setText('alertCodeExpires', alertDate(out.expires_at)?.toLocaleDateString('pt-BR') || '');
      document.getElementById('modalAlertCode').classList.remove('hidden');
    } else if (act === 'revoke') {
      if (!(await showConfirm({
        eyebrow: 'Aparelhos', title: 'Desconectar aparelhos?', label: 'Desconectar',
        msg: `Todos os aparelhos de ${m.full_name} param de funcionar ate ativar de novo com um codigo novo.`,
      }))) return;
      await alertApiJson(`/api/alert/members/${encodeURIComponent(m.id)}/revoke-devices`, { method: 'POST' });
      showToast('Aparelhos desconectados.');
    } else if (act === 'delete') {
      if (!(await showConfirm({
        eyebrow: 'Pessoas do Alerta', title: 'Excluir pessoa?', label: 'Excluir',
        msg: `${m.full_name} sai do Alerta e o app dela para de funcionar. O historico de alertas continua guardado.`,
      }))) return;
      await alertApiJson(`/api/alert/members/${encodeURIComponent(m.id)}`, { method: 'DELETE' });
      showToast('Pessoa excluida.');
    }
    await alertLoadMembers();
  } catch (err) {
    showToast(err.message || 'Falha na acao.', true);
  }
}
