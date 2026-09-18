# SightOps Agent

Acesso web **direto** a cameras/NVRs atras do tunel isolado, sem reconstrucao de
HTML e sem expor porta publica. Roda no PC do usuario.

## Como funciona
```
Navegador -> 127.0.0.1:47600 (agente) -> WSS autenticado -> App -> IP virtual (vnat) -> dispositivo
```
O frontend chama `http://127.0.0.1:47600/open?wsbase=wss://SEU_DOMINIO&ip=<ip>&port=<porta>&token=<sessao>`.
O agente sobe uma porta local e canaliza cada conexao TCP pela ponte
`/api/ws/web-tunnel/<ip>` (endpoint em app/api/endpoints/ws.py). Retorna a URL
local; o navegador abre e a UI do dispositivo carrega DIRETA.

## Rodar (dev)
```
node --experimental-websocket sightops-agent.js
```
(Node 21+ tem WebSocket nativo sem a flag; ou `npm i ws` que o agente usa se achar.)

## Empacotar pra distribuir (.exe unico, Windows)
- Node SEA (Single Executable Application) OU `pkg`. O runtime precisa do
  WebSocket: Node 21+ nativo, ou bundlar o pacote `ws`, ou passar
  `--experimental-websocket`.
- Instalar como autostart (bandeja) pra o usuario leigo so clicar no botao Web.

## Endpoints do controle (127.0.0.1:47600)
- `GET /health` -> {ok, agent, version}
- `GET /open?wsbase=&ip=&port=&token=` -> {ok, url, port}

Provado ponta a ponta (mock device+bridge) em 2026-09-18: browser -> agente ->
WS -> ponte -> device, caminho preservado.
