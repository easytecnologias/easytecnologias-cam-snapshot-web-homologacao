// Player de "ver ao vivo": fala MSE (Media Source Extensions) direto com o
// go2rtc pelo WebSocket que ele ja expoe (/go2rtc/api/ws), sem WebRTC, sem
// STUN/TURN -- so essa conexao WebSocket precisa passar, o que ja e
// garantido sempre que o proprio site carrega.
//
// Protocolo confirmado lendo o client oficial do go2rtc (video-rtc.js,
// versao 1.6.0, servido pelo proprio container em producao): depois que o
// WebSocket abre, cria-se um MediaSource; quando ele fica pronto
// (evento sourceopen), manda-se {type:'mse', value:<codecs suportados>};
// o go2rtc responde com o mesmo formato indicando o codec escolhido, e a
// partir dai manda fragmentos MP4 BINARIOS direto pelo WebSocket, que vao
// para dentro de um SourceBuffer.
//
// Usado tanto pelo painel de detalhe de camera (cameras.js) quanto pelo
// modal de manutencao (maintenance.js) -- antes cada um tinha sua propria
// copia da sinalizacao WebRTC.
//
// Streams NUNCA sao desregistrados explicitamente por este arquivo (nem em
// stop() nem ao trocar de qualidade) -- so a tarefa periodica do backend
// (reap_idle_streams, a cada 5min) remove streams sem espectador. E
// proposital: desregistrar ao fechar UM espectador derrubava outros que
// estivessem vendo a MESMA camera/qualidade ao mesmo tempo.

const LIVE_STREAM_CODECS = [
  'avc1.640029', // H.264 high 4.1
  'avc1.64002A', // H.264 high 4.2
  'avc1.640033', // H.264 high 5.1
];

function _liveStreamName(ip, subtype) {
  const st = Number(subtype) === 0 ? 0 : 1;
  return `cam_${ip.replace(/\./g, '_')}_${st}`;
}

function _liveStreamCodecs(MediaSourceClass) {
  return LIVE_STREAM_CODECS.filter(c => {
    try { return MediaSourceClass.isTypeSupported(`video/mp4; codecs="${c}"`); }
    catch (e) { return false; }
  }).join();
}

// Safari no iPhone/iPad NAO tem o MediaSource padrao -- so a partir do
// iOS 17 ele expoe uma versao restrita chamada ManagedMediaSource, com uma
// API praticamente igual mas anexada via video.srcObject em vez de
// video.src. Sem essa checagem, todo iPhone (qualquer navegador -- Chrome
// e Firefox no iOS sao Safari por baixo, exigencia da Apple) ficava preso
// em "Aguardando video..." com um erro silencioso no console. Confirmado
// lendo o client oficial do go2rtc (video-rtc.js), que documenta e trata
// exatamente esse caso. iOS anterior ao 17 nao tem suporte nenhum -- isso
// e limite da propria Apple, nao tem contorno.
function _liveStreamMediaSourceClass() {
  if (typeof ManagedMediaSource !== 'undefined') return ManagedMediaSource;
  if (typeof MediaSource !== 'undefined') return MediaSource;
  return null;
}

function _liveStreamConcat(buffers) {
  const total = buffers.reduce((n, b) => n + b.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const b of buffers) {
    out.set(new Uint8Array(b), offset);
    offset += b.byteLength;
  }
  return out;
}

// Espera crescente entre tentativas de reconexao, com teto de 30s -- evita
// bater no go2rtc/backend sem parar quando a camera fica offline por muito
// tempo ou a sessao expira.
function _liveStreamBackoffMs(attempt) {
  return Math.min(4000 * Math.pow(1.6, attempt), 30000);
}

// Sinaliza que o servidor nao tem (e o operador nao mandou) senha nenhuma
// pra essa camera/site -- diferente de qualquer outra falha, isso NAO deve
// entrar no ciclo de reconexao automatica (tentar de novo sem senha nunca
// vai funcionar sozinho).
class CredentialRequiredError extends Error {}

async function _liveStreamRegister(ip, user, pass, subtype, hint) {
  const body = {
    user: user || '',
    password: pass || '',
    subtype: Number(subtype),
    vendor: hint?.vendor || '',
    model: hint?.model || '',
  };
  const resp = await api(`/api/maintenance/stream_register/${ip}`, { method: 'POST', body: JSON.stringify(body) });
  if (!resp) throw new Error('Falha ao registrar stream');
  if (resp.status === 401) {
    const errBody = await resp.json().catch(() => null);
    if (errBody?.error === 'credential_required') throw new CredentialRequiredError('credential_required');
  }
  if (!resp.ok) throw new Error('Falha ao registrar stream');
  const respBody = await resp.json();
  return respBody?.stream_name || _liveStreamName(ip, subtype);
}

/**
 * Monta o player de "ver ao vivo" num <video> ja existente na tela.
 *
 * @param {HTMLVideoElement} videoEl
 * @param {{ip: string, user: string, pass: string, subtype?: number, vendor?: string, model?: string, onStatus?: (texto: string) => void}} opts
 * @returns {{setSubtype: (novoSubtype: number) => void, stop: () => void}}
 */
function mountLiveStream(videoEl, opts) {
  const user = opts.user || 'admin';
  const pass = opts.pass || '';
  const hint = { vendor: opts.vendor || '', model: opts.model || '' };
  const onStatus = typeof opts.onStatus === 'function' ? opts.onStatus : () => {};

  const MediaSourceClass = _liveStreamMediaSourceClass();
  const isManaged = typeof ManagedMediaSource !== 'undefined' && MediaSourceClass === ManagedMediaSource;

  let ip = opts.ip;
  let subtype = Number(opts.subtype) === 0 ? 0 : 1;
  let generation = 0; // incrementa a cada connect(); descarta eventos de tentativas antigas
  let ws = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let stopped = false;

  function teardown() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    try { videoEl.pause(); } catch (e) {}
    videoEl.removeAttribute('src');
    videoEl.srcObject = null;
    videoEl.load();
  }

  function scheduleReconnect() {
    reconnectTimer = setTimeout(connect, _liveStreamBackoffMs(reconnectAttempts++));
  }

  function onVisibilityChange() {
    if (stopped) return;
    if (document.hidden) {
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    } else if (!ws || ws.readyState !== WebSocket.OPEN) {
      connect();
    }
  }
  document.addEventListener('visibilitychange', onVisibilityChange);

  async function connect() {
    const myGen = ++generation;
    teardown();
    if (stopped) return;
    if (!MediaSourceClass) {
      onStatus('Navegador sem suporte a video ao vivo');
      return;
    }
    onStatus('Conectando...');

    let streamName;
    try {
      streamName = await _liveStreamRegister(ip, user, pass, subtype, hint);
    } catch (e) {
      if (myGen !== generation || stopped) return;
      if (e instanceof CredentialRequiredError) {
        // Nao adianta tentar de novo sem senha -- devolve o sinal pro
        // chamador (o painel da camera) mostrar o formulario de login,
        // em vez de ficar reconectando pra sempre.
        onStatus('credential_required');
        return;
      }
      onStatus('Erro ao registrar stream');
      scheduleReconnect();
      return;
    }
    if (myGen !== generation || stopped) return;

    // Deriva o WS do go2rtc do MESMO API_BASE: no v3 (/v3-api) o prod-nginx
    // roteia /v3-api/go2rtc/api/ws -> go2rtc do V3; sem isso ia pra /go2rtc/ na
    // raiz (go2rtc do PROD, que nao tem o stream) -> "mse: stream not found".
    // Prod: API_BASE == origin -> comportamento inalterado.
    const _wsBase = ((typeof API_BASE === 'string' && API_BASE) ? API_BASE : location.origin).replace(/^http/, 'ws');
    const socket = new WebSocket(`${_wsBase}/go2rtc/api/ws?src=${streamName}`);
    socket.binaryType = 'arraybuffer';
    ws = socket;

    let ms = null;
    let sourceBuffer = null;
    let pending = [];

    function onSourceBufferUpdateEnd() {
      if (!sourceBuffer.updating && pending.length) {
        const merged = _liveStreamConcat(pending);
        pending = [];
        try { sourceBuffer.appendBuffer(merged); } catch (e) { /* ignora, o proximo frame corrige */ }
      }
      if (!sourceBuffer.updating && sourceBuffer.buffered && sourceBuffer.buffered.length) {
        const end = sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1);
        const start0 = sourceBuffer.buffered.start(0);
        // Guarda mais buffer e NAO cola na borda ao vivo: toca ~2s atras dela.
        // Antes o player perseguia a borda (acelerava ate 2x, zero folga) e,
        // com stream de fps irregular/H.265 pelo tunel, alcancava a borda e
        // CONGELAVA esperando o proximo frame. Agora mantem ~2-4s de folga
        // (buffer que absorve o jitter) e so acelera de leve se atrasar demais.
        const RETAIN = 15;
        const start = end - RETAIN;
        if (start > start0) {
          sourceBuffer.remove(start0, start);
          ms.setLiveSeekableRange(start, end);
        }
        if (videoEl.currentTime < start || videoEl.currentTime > end) {
          videoEl.currentTime = end - 2;
        }
        const behind = end - videoEl.currentTime;
        videoEl.playbackRate = behind > 4 ? 1.3 : 1;
      }
    }

    socket.onopen = () => {
      if (myGen !== generation) return;
      onStatus('Aguardando video...');

      ms = new MediaSourceClass();
      if (isManaged) {
        videoEl.disableRemotePlayback = true;
        videoEl.srcObject = ms;
      } else {
        videoEl.src = URL.createObjectURL(ms);
      }
      ms.addEventListener('sourceopen', () => {
        if (myGen !== generation) return;
        if (!isManaged) URL.revokeObjectURL(videoEl.src);
        socket.send(JSON.stringify({ type: 'mse', value: _liveStreamCodecs(MediaSourceClass) }));
      }, { once: true });
    };

    socket.onmessage = (ev) => {
      if (myGen !== generation) return;

      if (typeof ev.data === 'string') {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'mse') {
          reconnectAttempts = 0;
          sourceBuffer = ms.addSourceBuffer(msg.value);
          sourceBuffer.mode = 'segments';
          sourceBuffer.addEventListener('updateend', onSourceBufferUpdateEnd);
          onStatus('');
          videoEl.play().catch(() => {});
        } else if (msg.type === 'error') {
          // go2rtc so descobre que a senha esta errada DEPOIS de registrar o
          // stream e tentar puxar o RTSP de verdade -- isso acontece quando a
          // camera tem uma senha diferente da salva pro site (a maioria tem a
          // mesma, mas nem sempre). Trata igual credential_required: limpa o
          // que tava guardado neste navegador e reabre o formulario, em vez
          // de deixar a mensagem crua do ffmpeg na tela sem dizer o que fazer.
          if (/401|unauthorized|authorization failed/i.test(String(msg.value || ''))) {
            onStatus('credential_required');
          } else {
            onStatus('Erro go2rtc: ' + msg.value);
          }
        }
        return;
      }

      // fragmento MP4 binario
      if (!sourceBuffer) return; // chegou antes do handshake 'mse' terminar, descarta
      if (sourceBuffer.updating || pending.length) {
        pending.push(ev.data);
      } else {
        try { sourceBuffer.appendBuffer(ev.data); } catch (e) { /* ignora, o proximo frame corrige */ }
      }
    };

    socket.onclose = () => {
      if (myGen !== generation || stopped) return;
      onStatus('Reconectando...');
      scheduleReconnect();
    };

    socket.onerror = () => {
      if (myGen !== generation) return;
      onStatus('Erro de conexao');
    };
  }

  connect();

  return {
    setSubtype(novoSubtype) {
      const st = Number(novoSubtype) === 0 ? 0 : 1;
      if (st === subtype) return;
      subtype = st;
      reconnectAttempts = 0;
      connect();
    },
    stop() {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      stopped = true;
      generation++;
      teardown();
    },
  };
}
