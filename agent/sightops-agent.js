#!/usr/bin/env node
/*
 * SightOps Agent -- acesso web direto a cameras/NVRs atras do tunel isolado.
 *
 * Roda no PC do usuario. O navegador (frontend do SightOps) fala com ele em
 * http://127.0.0.1:47600 e pede pra abrir um dispositivo; o agente sobe uma
 * porta local (127.0.0.1:porta_aleatoria) e canaliza cada conexao TCP por um
 * WebSocket seguro ate a ponte do app (/ws/web-tunnel/<ip>), que alcanca o
 * dispositivo pelo IP virtual do conector. Assim a UI abre DIRETA, sem
 * reconstrucao de HTML e sem porta publica exposta.
 *
 * Zero dependencias: usa http/net nativos e o WebSocket embutido do Node 18+.
 */
'use strict';

const http = require('http');
const net = require('net');

// WebSocket: global (Node com --experimental-websocket / Node 21+) ou pacote 'ws'.
let WS = (typeof WebSocket !== 'undefined') ? WebSocket : null;
if (!WS) { try { WS = require('ws'); } catch (_) { /* sem ws */ } }
if (!WS) {
  console.error('WebSocket indisponivel. Rode com: node --experimental-websocket sightops-agent.js  (ou instale o pacote ws)');
  process.exit(1);
}

const CONTROL_HOST = '127.0.0.1';
const CONTROL_PORT = 47600;           // porta fixa que o frontend procura
const IDLE_MS = 10 * 60 * 1000;       // fecha o forward apos 10 min ocioso
const VERSION = '1.0.0';

// forwards ativos: chave "wsbase|ip|port" -> { server, localPort, lastUsed, key }
const forwards = new Map();

function log(...a) { console.log(new Date().toISOString(), ...a); }

function fwdKey(wsbase, ip, port) { return `${wsbase}|${ip}|${port}`; }

// Sobe (ou reaproveita) um forward local -> WS bridge -> dispositivo.
function ensureForward(wsbase, ip, port, token) {
  return new Promise((resolve, reject) => {
    const key = fwdKey(wsbase, ip, port);
    const existing = forwards.get(key);
    if (existing) { existing.lastUsed = Date.now(); return resolve(existing.localPort); }

    const server = net.createServer((sock) => {
      const entry = forwards.get(key);
      if (entry) entry.lastUsed = Date.now();
      const url = `${wsbase}/ws/web-tunnel/${encodeURIComponent(ip)}`
        + `?token=${encodeURIComponent(token)}&port=${encodeURIComponent(port)}`;
      let ws;
      try { ws = new WS(url); } catch (e) { sock.destroy(); return; }
      ws.binaryType = 'arraybuffer';
      const pending = [];
      let open = false;
      ws.onopen = () => {
        open = true;
        for (const chunk of pending) ws.send(chunk);
        pending.length = 0;
      };
      ws.onmessage = (ev) => {
        try {
          const buf = Buffer.from(ev.data instanceof ArrayBuffer ? ev.data : ev.data);
          sock.write(buf);
        } catch (_) {}
      };
      ws.onclose = () => { try { sock.end(); } catch (_) {} };
      ws.onerror = () => { try { sock.destroy(); } catch (_) {} };
      sock.on('data', (data) => {
        if (entry) entry.lastUsed = Date.now();
        if (open) { try { ws.send(data); } catch (_) {} }
        else pending.push(data);
      });
      sock.on('close', () => { try { ws.close(); } catch (_) {} });
      sock.on('error', () => { try { ws.close(); } catch (_) {} });
    });

    server.on('error', (e) => reject(e));
    server.listen(0, CONTROL_HOST, () => {
      const localPort = server.address().port;
      forwards.set(key, { server, localPort, lastUsed: Date.now(), key });
      log(`forward aberto: 127.0.0.1:${localPort} -> ${ip}:${port} (via ${wsbase})`);
      resolve(localPort);
    });
  });
}

// Faxina periodica dos forwards ociosos.
setInterval(() => {
  const now = Date.now();
  for (const [key, e] of forwards) {
    if (now - e.lastUsed > IDLE_MS) {
      try { e.server.close(); } catch (_) {}
      forwards.delete(key);
      log(`forward fechado (ocioso): ${key}`);
    }
  }
}, 60 * 1000).unref();

function sendJson(res, code, obj, origin) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': origin || '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    // PNA: navegador HTTPS -> 127.0.0.1 exige este header (Chrome/Edge).
    'Access-Control-Allow-Private-Network': 'true',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

const control = http.createServer(async (req, res) => {
  const origin = req.headers.origin || '*';
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      // PNA: responde ao preflight Access-Control-Request-Private-Network.
      'Access-Control-Allow-Private-Network': 'true',
    });
    return res.end();
  }
  let u;
  try { u = new URL(req.url, `http://${CONTROL_HOST}:${CONTROL_PORT}`); }
  catch (_) { return sendJson(res, 400, { ok: false, error: 'url invalida' }, origin); }

  if (u.pathname === '/health') {
    return sendJson(res, 200, { ok: true, agent: 'sightops', version: VERSION }, origin);
  }

  if (u.pathname === '/open') {
    const wsbase = (u.searchParams.get('wsbase') || '').replace(/\/+$/, '');
    const ip = u.searchParams.get('ip') || '';
    const port = u.searchParams.get('port') || '80';
    const token = u.searchParams.get('token') || '';
    if (!/^wss?:\/\//i.test(wsbase) || !ip || !token) {
      return sendJson(res, 400, { ok: false, error: 'faltam wsbase/ip/token' }, origin);
    }
    try {
      const localPort = await ensureForward(wsbase, ip, port, token);
      return sendJson(res, 200, { ok: true, url: `http://127.0.0.1:${localPort}/`, port: localPort }, origin);
    } catch (e) {
      return sendJson(res, 502, { ok: false, error: String(e && e.message || e) }, origin);
    }
  }

  return sendJson(res, 404, { ok: false, error: 'nao encontrado' }, origin);
});

control.on('error', (e) => {
  if (e && e.code === 'EADDRINUSE') {
    log(`porta ${CONTROL_PORT} ja em uso -- o agente ja esta rodando? saindo.`);
    process.exit(0);
  }
  log('erro no controle:', e && e.message || e);
  process.exit(1);
});

control.listen(CONTROL_PORT, CONTROL_HOST, () => {
  log(`SightOps Agent v${VERSION} ouvindo em http://${CONTROL_HOST}:${CONTROL_PORT}`);
});
