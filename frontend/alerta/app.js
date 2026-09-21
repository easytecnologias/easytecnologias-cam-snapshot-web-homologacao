// SightOps Alerta -- app do botao de panico.
//
// Roda igual no navegador (simulacao no computador) e dentro do Capacitor
// (APK Android). Nada aqui depende do login do SightOps: o aparelho e ativado
// com um codigo de uso unico e passa a usar um token proprio.
//
// Regra de coacao: depois de cancelar, a tela e SEMPRE a mesma ("Alerta
// cancelado"), com senha normal ou de coacao. Se o servidor responder
// tracking=true (coacao), o app continua mandando localizacao em silencio,
// sem nenhum sinal visual diferente.

(function () {
  'use strict';

  // No APK o app nao esta no mesmo dominio do servidor: o build do Capacitor
  // define window.ALERTA_API_BASE antes deste script.
  const API_BASE = window.ALERTA_API_BASE
    || (window.location.pathname.startsWith('/v3/') ? window.location.origin + '/v3-api' : window.location.origin);

  const HOLD_MS = 2000;
  const FLUSH_MS = 5000;
  const STATUS_POLL_MS = 5000;
  const RETRY_MS = 3000;

  const store = {
    get(k) { try { return localStorage.getItem('alerta_' + k) || ''; } catch { return ''; } },
    set(k, v) { try { v ? localStorage.setItem('alerta_' + k, v) : localStorage.removeItem('alerta_' + k); } catch {} },
  };

  const $ = (id) => document.getElementById(id);

  const state = {
    token: store.get('token'),
    member: null,
    incidentId: store.get('incident'),
    silent: store.get('silent') === '1',
    watchId: null,
    lastFix: null,          // ultima posicao do GPS
    lastSentAt: 0,          // timestamp (ms) da ultima posicao enviada
    pending: [],            // posicoes que ainda nao chegaram no servidor
    flushTimer: null,
    pollTimer: null,
    wakeLock: null,
    battery: null,
  };

  try { JSON.parse(store.get('member') || 'null'); } catch { store.set('member', ''); }
  state.member = JSON.parse(store.get('member') || 'null');

  // ------------------------------------------------------------------ API

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function api(path, { method = 'GET', body } = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (state.token) headers.Authorization = 'Bearer ' + state.token;
    let res;
    try {
      res = await fetch(API_BASE + '/api/alert/app' + path, {
        method, headers, body: body ? JSON.stringify(body) : undefined, cache: 'no-store',
      });
    } catch (e) {
      throw new ApiError(0, 'Sem conexão com a internet.');
    }
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : 'Erro ' + res.status;
      if (res.status === 401) forgetDevice();
      throw new ApiError(res.status, detail);
    }
    return data;
  }

  // ------------------------------------------------------------------ telas

  function show(id) {
    document.querySelectorAll('.screen').forEach((s) => { s.hidden = s.id !== id; });
  }

  function renderHome() {
    const m = state.member || {};
    $('homeName').textContent = m.full_name || 'SightOps Alerta';
    $('homeUnit').textContent = [m.role_title, m.unit_name].filter(Boolean).join(' · ');
    show('scrHome');
  }

  function renderAlarm() {
    show('scrAlarm');
  }

  function setAlarmStatus(text, kind) {
    const el = $('alarmStatus');
    el.textContent = text;
    el.className = 'alarm-status' + (kind ? ' ' + kind : '');
  }

  function timeLabel(ms) {
    if (!ms) return '-';
    return new Date(ms).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function updateGpsFacts() {
    const fix = state.lastFix;
    $('alarmGps').textContent = fix ? `precisão ~${Math.round(fix.accuracy || 0)} m` : 'aguardando GPS';
    $('alarmLast').textContent = timeLabel(state.lastSentAt);
  }

  // ------------------------------------------------------------------ boot

  function forgetDevice() {
    stopTracking();
    state.token = ''; state.member = null; state.incidentId = ''; state.silent = false;
    ['token', 'member', 'incident', 'silent'].forEach((k) => store.set(k, ''));
    show('scrActivate');
  }

  async function boot() {
    if (!state.token) { show('scrActivate'); return; }
    // Mostra o inicio na hora com o que esta salvo: o botao tem que funcionar
    // mesmo sem internet no momento em que o app abre.
    if (state.member) renderHome();
    try {
      const me = await api('/me');
      applyMember(me.member);
      if (!me.member.pins_configured) { openPins(false); return; }
      if (me.incident) {
        resumeIncident(me.incident);
      } else {
        clearIncident();
        renderHome();
      }
    } catch (e) {
      if (e.status === 401) return;
      if (!state.member) show('scrActivate');
      else if (state.incidentId) resumeIncident({ id: state.incidentId });
    }
  }

  function applyMember(member) {
    state.member = member;
    store.set('member', JSON.stringify(member));
  }

  function resumeIncident(inc) {
    state.incidentId = inc.id;
    store.set('incident', inc.id);
    startTracking();
    if (state.silent) { renderHome(); return; }
    renderAlarm();
    setAlarmStatus(inc.attended ? 'A central está atendendo' : 'Aguardando a central assumir', inc.attended ? 'ok' : '');
  }

  function clearIncident() {
    stopTracking();
    state.incidentId = ''; state.silent = false; state.pending = [];
    store.set('incident', ''); store.set('silent', '');
  }

  // ------------------------------------------------------------------ ativacao

  $('inCode').addEventListener('input', (ev) => {
    const raw = ev.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8);
    ev.target.value = raw.length > 4 ? raw.slice(0, 4) + '-' + raw.slice(4) : raw;
  });

  $('formActivate').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = ev.submitter || ev.target.querySelector('button');
    $('errActivate').textContent = '';
    btn.disabled = true;
    try {
      const out = await api('/activate', {
        method: 'POST',
        body: { code: $('inCode').value, device_label: navigator.userAgent.slice(0, 120), platform: detectPlatform() },
      });
      state.token = out.token;
      store.set('token', out.token);
      applyMember(out.member);
      $('inCode').value = '';
      out.member.pins_configured ? renderHome() : openPins(false);
    } catch (e) {
      $('errActivate').textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });

  function detectPlatform() {
    if (window.Capacitor?.getPlatform) return window.Capacitor.getPlatform();
    return /android/i.test(navigator.userAgent) ? 'android-web' : 'web';
  }

  // ------------------------------------------------------------------ senhas

  function openPins(changing) {
    $('pinsTitle').textContent = changing ? 'Trocar senhas' : 'Crie suas senhas';
    $('lblCurrentPin').hidden = !changing;
    $('btnPinsBack').hidden = !changing;
    ['inCurrentPin', 'inPin', 'inPin2', 'inDuress', 'inDuress2'].forEach((id) => { $(id).value = ''; });
    $('errPins').textContent = '';
    show('scrPins');
  }

  $('formPins').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = $('errPins');
    const pin = $('inPin').value, duress = $('inDuress').value;
    if (!/^\d{4,8}$/.test(pin) || !/^\d{4,8}$/.test(duress)) { err.textContent = 'Use de 4 a 8 números.'; return; }
    if (pin !== $('inPin2').value) { err.textContent = 'A senha normal não confere.'; return; }
    if (duress !== $('inDuress2').value) { err.textContent = 'A senha de coação não confere.'; return; }
    if (pin === duress) { err.textContent = 'A senha de coação precisa ser diferente da normal.'; return; }
    try {
      await api('/pins', { method: 'POST', body: { pin, duress_pin: duress, current_pin: $('inCurrentPin').value } });
      applyMember({ ...(state.member || {}), pins_configured: true });
      renderHome();
    } catch (e) {
      err.textContent = e.message;
    }
  });
  $('btnPinsBack').addEventListener('click', renderHome);

  // ------------------------------------------------------------------ botao de panico

  (function bindPanic() {
    const btn = $('btnPanic');
    let timer = null;
    const start = (ev) => {
      ev.preventDefault();
      if (timer) return;
      btn.classList.add('holding');
      $('panicHint').textContent = 'Continue segurando...';
      if (navigator.vibrate) navigator.vibrate(30);
      timer = setTimeout(() => { timer = null; btn.classList.remove('holding'); triggerAlarm(); }, HOLD_MS);
    };
    const stop = () => {
      if (!timer) return;
      clearTimeout(timer); timer = null;
      btn.classList.remove('holding');
      $('panicHint').textContent = 'Segure o botão por 2 segundos';
    };
    btn.addEventListener('pointerdown', start);
    ['pointerup', 'pointerleave', 'pointercancel'].forEach((t) => btn.addEventListener(t, stop));
    btn.addEventListener('contextmenu', (e) => e.preventDefault());
  })();

  async function triggerAlarm() {
    $('panicHint').textContent = 'Segure o botão por 2 segundos';
    state.silent = false; store.set('silent', '');
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
    renderAlarm();
    setAlarmStatus('Enviando alerta...');
    updateGpsFacts();
    startTracking();
    // Nao espera o GPS: o alerta sai na hora, a posicao chega logo depois.
    for (;;) {
      try {
        const out = await api('/trigger', { method: 'POST', body: { position: fixPayload(state.lastFix) } });
        state.incidentId = out.incident.id;
        store.set('incident', out.incident.id);
        if (state.lastFix) state.lastSentAt = Date.now();
        setAlarmStatus(out.incident.attended ? 'A central está atendendo' : 'Alerta recebido. Aguardando a central assumir',
          out.incident.attended ? 'ok' : '');
        updateGpsFacts();
        return;
      } catch (e) {
        if (e.status === 401) return;
        setAlarmStatus('Sem conexão. Tentando de novo...', 'warn');
        await new Promise((r) => setTimeout(r, RETRY_MS));
      }
    }
  }

  // ------------------------------------------------------------------ rastreamento

  function fixPayload(fix) {
    if (!fix) return null;
    return {
      lat: fix.lat, lon: fix.lon, accuracy: fix.accuracy,
      battery: state.battery, recorded_at: new Date(fix.ts).toISOString().replace(/\.\d+Z$/, 'Z'),
    };
  }

  // APK (Capacitor): GPS por servico nativo em primeiro plano, que continua com
  // a tela bloqueada -- o GPS do navegador para quando a tela apaga. Titulo e
  // texto da notificacao fixa sao os MESMOS no panico e na coacao, e neutros:
  // nada na tela do celular pode denunciar que a central sabe da coacao.
  const NativeGeo = (() => {
    const cap = window.Capacitor;
    if (!cap || typeof cap.isNativePlatform !== 'function' || !cap.isNativePlatform()) return null;
    try { return cap.registerPlugin ? cap.registerPlugin('BackgroundGeolocation') : cap.Plugins.BackgroundGeolocation; } catch { return null; }
  })();

  function startNativeWatch() {
    state.watchId = 'native-pending';
    NativeGeo.addWatcher({
      backgroundTitle: 'SightOps Alerta',
      backgroundMessage: 'Protecao ativa',
      requestPermissions: true,
      stale: false,
      distanceFilter: 0,
    }, (loc, err) => {
      if (err) { onFixError({ code: err.code === 'NOT_AUTHORIZED' ? 1 : 2 }); return; }
      if (!loc) return;
      onFix({ coords: { latitude: loc.latitude, longitude: loc.longitude, accuracy: loc.accuracy }, timestamp: loc.time || Date.now() });
    }).then((id) => {
      // Parou antes do plugin responder: desliga o que acabou de ligar.
      if (state.watchId === 'native-pending') state.watchId = id;
      else NativeGeo.removeWatcher({ id }).catch(() => {});
    }).catch(() => { if (state.watchId === 'native-pending') state.watchId = null; });
  }

  function startTracking() {
    if (state.watchId === null) {
      if (NativeGeo) {
        startNativeWatch();
      } else if (navigator.geolocation) {
        state.watchId = navigator.geolocation.watchPosition(onFix, onFixError, {
          enableHighAccuracy: true, maximumAge: 5000, timeout: 20000,
        });
      }
    }
    if (!state.flushTimer) state.flushTimer = setInterval(flushPositions, FLUSH_MS);
    if (!state.pollTimer) state.pollTimer = setInterval(pollStatus, STATUS_POLL_MS);
    requestWakeLock();
    readBattery();
  }

  function stopTracking() {
    if (NativeGeo) {
      if (typeof state.watchId === 'string' && state.watchId !== 'native-pending') {
        NativeGeo.removeWatcher({ id: state.watchId }).catch(() => {});
      }
    } else if (state.watchId !== null && navigator.geolocation) {
      navigator.geolocation.clearWatch(state.watchId);
    }
    state.watchId = null;
    clearInterval(state.flushTimer); state.flushTimer = null;
    clearInterval(state.pollTimer); state.pollTimer = null;
    state.lastFix = null; state.lastSentAt = 0;
    if (state.wakeLock) { state.wakeLock.release().catch(() => {}); state.wakeLock = null; }
  }

  function onFix(pos) {
    state.lastFix = { lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: pos.coords.accuracy, ts: pos.timestamp || Date.now() };
    state.pending.push(fixPayload(state.lastFix));
    if (state.pending.length > 50) state.pending.splice(0, state.pending.length - 50);
    updateGpsFacts();
    // Com a tela bloqueada o setInterval do WebView quase para; a chegada da
    // posicao (servico nativo) vira o proprio gatilho de envio.
    if (Date.now() - (state.lastFlushTry || 0) >= FLUSH_MS) flushPositions();
  }

  function onFixError(err) {
    if (!state.silent) $('alarmGps').textContent = err.code === 1 ? 'GPS sem permissão' : 'sem sinal de GPS';
  }

  async function flushPositions() {
    if (!state.incidentId || !state.pending.length) return;
    state.lastFlushTry = Date.now();
    const batch = state.pending.slice();
    try {
      const out = await api(`/incidents/${encodeURIComponent(state.incidentId)}/positions`, { method: 'POST', body: { positions: batch } });
      state.pending.splice(0, batch.length);
      state.lastSentAt = Date.now();
      updateGpsFacts();
      if (out.tracking === false) finishedByCentral();
    } catch (e) {
      if (e.status === 404) finishedByCentral();
      // sem rede: as posicoes ficam na fila e vao na proxima rodada
    }
  }

  async function pollStatus() {
    if (!state.incidentId) return;
    try {
      const me = await api('/me');
      if (!me.incident) { finishedByCentral(); return; }
      if (!state.silent) {
        setAlarmStatus(me.incident.attended ? 'A central está atendendo' : 'Alerta recebido. Aguardando a central assumir',
          me.incident.attended ? 'ok' : '');
      }
    } catch { /* sem rede: segue tentando */ }
  }

  function finishedByCentral() {
    const wasSilent = state.silent;
    clearIncident();
    if (wasSilent) return; // em coacao a tela ja esta no inicio; nao muda nada
    setAlarmStatus('A central encerrou o alerta', 'ok');
    setTimeout(renderHome, 3000);
  }

  async function requestWakeLock() {
    try {
      if (!state.wakeLock && navigator.wakeLock) state.wakeLock = await navigator.wakeLock.request('screen');
    } catch {}
  }

  async function readBattery() {
    try {
      if (!navigator.getBattery) return;
      const b = await navigator.getBattery();
      state.battery = Math.round(b.level * 100);
    } catch {}
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.incidentId) { requestWakeLock(); flushPositions(); }
  });

  // ------------------------------------------------------------------ cancelar (teclado)

  let pinBuffer = '';

  (function buildKeypad() {
    const keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'apagar', '0', 'OK'];
    $('keypad').innerHTML = keys.map((k) => {
      const cls = k === 'OK' ? 'key-ok' : (k === 'apagar' ? 'key-muted' : '');
      return `<button type="button" class="${cls}" data-key="${k}">${k}</button>`;
    }).join('');
    $('keypad').addEventListener('click', (ev) => {
      const k = ev.target.closest('button')?.dataset.key;
      if (!k) return;
      if (k === 'apagar') pinBuffer = pinBuffer.slice(0, -1);
      else if (k === 'OK') { submitCancel(); return; }
      else if (pinBuffer.length < 8) pinBuffer += k;
      renderPinDots();
    });
  })();

  function renderPinDots() {
    $('pinDots').innerHTML = '<i></i>'.repeat(pinBuffer.length);
  }

  function openPinSheet() {
    pinBuffer = ''; renderPinDots();
    $('errPinSheet').textContent = '';
    $('pinSheet').hidden = false;
  }

  function closePinSheet() { $('pinSheet').hidden = true; pinBuffer = ''; }

  $('btnCancelAlarm').addEventListener('click', openPinSheet);
  $('btnPinCancel').addEventListener('click', closePinSheet);

  async function submitCancel() {
    if (pinBuffer.length < 4) { $('errPinSheet').textContent = 'Digite sua senha.'; return; }
    if (!state.incidentId) { $('errPinSheet').textContent = 'O alerta ainda está sendo enviado. Aguarde.'; return; }
    const pin = pinBuffer;
    try {
      const out = await api(`/incidents/${encodeURIComponent(state.incidentId)}/cancel`, { method: 'POST', body: { pin } });
      closePinSheet();
      if (out.tracking) {
        // Coacao: continua rastreando, mas a tela e identica a um cancelamento.
        state.silent = true; store.set('silent', '1');
      } else {
        clearIncident();
      }
      show('scrCancelled');
      setTimeout(renderHome, 3000);
    } catch (e) {
      pinBuffer = ''; renderPinDots();
      $('errPinSheet').textContent = e.status === 0 ? 'Sem conexão. Tente de novo.' : e.message;
    }
  }

  // ------------------------------------------------------------------ menu

  $('btnMenu').addEventListener('click', () => { $('testGpsResult').textContent = ''; $('menuSheet').hidden = false; });
  $('btnMenuClose').addEventListener('click', () => { $('menuSheet').hidden = true; });
  $('btnChangePins').addEventListener('click', () => { $('menuSheet').hidden = true; openPins(true); });
  $('btnTestGps').addEventListener('click', () => {
    const out = $('testGpsResult');
    if (!navigator.geolocation) { out.textContent = 'Este aparelho não tem GPS disponível.'; return; }
    out.textContent = 'Buscando...';
    navigator.geolocation.getCurrentPosition(
      (p) => { out.textContent = `GPS funcionando (precisão ~${Math.round(p.coords.accuracy)} m). Nada foi enviado.`; },
      (e) => { out.textContent = e.code === 1 ? 'Sem permissão de localização. Libere nas configurações.' : 'Não consegui pegar o GPS agora.'; },
      { enableHighAccuracy: true, timeout: 15000 },
    );
  });
  $('btnLogout').addEventListener('click', () => {
    if (!confirm('Desativar este aparelho? Você vai precisar de um novo código da central.')) return;
    $('menuSheet').hidden = true;
    forgetDevice();
  });

  boot();
})();
