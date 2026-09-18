# Proxy web confiável de câmera/DVR/NVR — Design

## Contexto e problema

O botão "Web" (abrir a interface administrativa nativa do equipamento) existe em
três lugares do frontend e se comporta de três jeitos diferentes hoje:

- `frontend/js/cameras.js:2489` — já usa um proxy HTTP no backend
  (`/api/maintenance/web/{ip}/`, `app/api/endpoints/maintenance.py:1181`), com
  reescrita de HTML/JS pra caminho absoluto e um shim de JS que intercepta
  `fetch`/`XMLHttpRequest`/`setAttribute`. É a base certa, mas tem bugs reais.
- `frontend/js/bootstrap.js:241` — `window.open('http://' + item.camera_ip)`,
  direto do navegador do operador, sem passar pelo servidor.
- `frontend/js/recorders.js:755` — mesma coisa, pra DVR/NVR
  (`window.open('http://' + _recActive.host)`).

Os dois últimos só funcionam se o computador do operador tiver, na hora, um
túnel WireGuard ativo pra rede daquele cliente específico — o que normalmente
não é o caso, já que o servidor (não o navegador do operador) é quem tem a
rota WireGuard pra cada rede de cliente. Resultado: comportamento "capenga"
relatado pelo usuário — às vezes funciona, na maioria das vezes não.

Mesmo o caminho que já usa o proxy (`cameras.js`) tem três bugs concretos,
achados lendo o código de `maintenance.py`:

1. **`_camera_web_target_url` fixa o esquema em `"http"` sempre** — câmera/DVR
   que só aceita HTTPS no admin (comum em firmware mais novo) nunca abre.
2. **O cabeçalho `WWW-Authenticate` é descartado** no loop que copia headers da
   resposta (`maintenance_camera_web_proxy`, linhas ~1211-1226) — câmera com
   login HTTP padrão (Basic/Digest) devolve 401 pedindo credencial, mas o
   proxy apaga esse cabeçalho antes de repassar pro navegador. Sem ele o
   navegador não sabe que precisa pedir senha: o operador só vê uma tela em
   branco/erro, sem chance nenhuma de logar.
3. **`_is_proxy_allowed_host` só confere se o IP é privado/CGNAT**, não se
   pertence a um equipamento cadastrado no tenant atual. Isso é uma lacuna de
   segurança (o próprio código já reconhece esse risco em outro lugar — ver
   `_ip_in_inventory` em `app/api/endpoints/cameras.py:149`, cujo docstring diz
   literalmente "faixas privadas se repetem entre tenants neste sistema") e
   também é o motivo de o proxy nunca ter podido logar sozinho: sem saber
   *qual* equipamento é aquele IP, não tem como buscar a senha salva dele.

## Objetivo

Um clique em "Web" deve, sempre:
- Abrir a interface administrativa real do equipamento numa aba nova.
- Funcionar sem o operador precisar de VPN nenhuma na própria máquina.
- Logar sozinho quando o equipamento usa autenticação HTTP padrão (Basic ou
  Digest) e já existe senha salva no inventário.
- Quando o equipamento usa uma tela de login própria em JavaScript (não HTTP
  auth), abrir a tela de login de verdade, funcionando (hoje nem isso
  acontece de forma confiável) — o operador digita a senha como sempre fez,
  sem promessa de login automático que não pode ser cumprida com segurança.

## Escopo

Câmeras e DVR/NVR. Os três pontos de entrada (`cameras.js`, `bootstrap.js`,
`recorders.js`) passam a apontar para o mesmo proxy corrigido.

Fora de escopo: qualquer outro tipo de equipamento (OLT, switch, Mikrotik) —
esses já são geridos por CLI/SSH, não têm "botão Web" hoje, e não foram
pedidos.

## Arquitetura

Continua sendo um proxy HTTP no backend (a base que já existe em
`maintenance.py` está certa) — não um túnel de rede por sessão. Um túnel
dedicado por clique (portas efêmeras, roteamento dinâmico no nginx) resolveria
o mesmo problema de forma mais "fiel", mas exige gerir alocação/limpeza de
porta e um roteamento que hoje não existe — infraestrutura nova pra um
ganho marginal, já que a reescrita de conteúdo do proxy atual já cobre bem
os casos reais (tela de configuração, não vídeo ao vivo via navegador).

### Correção 1 — HTTPS com fallback, não HTTP fixo

`_camera_web_target_url` deixa de fixar o esquema. `maintenance_camera_web_proxy`
tenta HTTPS primeiro (equipamento de CFTV moderno tende a vir com HTTPS
habilitado, às vezes com certificado autoassinado — o proxy já usa
`verify=False`, então certificado autoassinado não é problema) e cai para
HTTP se a conexão HTTPS falhar (erro de conexão/handshake, não erro HTTP do
próprio equipamento). O resultado da tentativa que funcionou fica em cache
em memória por IP durante a vida do processo, pra não pagar o custo da
tentativa dupla em toda requisição subsequente da mesma sessão de navegação
(a página faz dezenas de sub-requisições pra JS/CSS/imagem).

### Correção 2 — repassar `WWW-Authenticate`

Adicionar `www-authenticate` ao conjunto de headers repassados tal como
vêm do equipamento, no loop que já existe em `maintenance_camera_web_proxy`.
Sozinho isso já faz o navegador mostrar o diálogo nativo de login pra
equipamento com Basic/Digest — o que hoje simplesmente não acontece.

### Correção 3 — confirmar posse do equipamento antes de proxiar

Antes de montar a URL de destino, `maintenance_camera_web_proxy` passa a
chamar o equivalente de `_ip_in_inventory(ip)` (câmera) OU uma função nova
análoga para DVR/NVR (`_recorder_row_for_host`, lendo do mesmo inventário que
já serve `frontend/js/recorders.js` — `legacy_rows_from_db('dvr'/'nvr', ...)`
com fallback pro JSON por tenant, mesmo padrão de `_camera_row_for_ip`).  Se
o IP não pertence a nenhum equipamento do tenant atual, `403` — nunca proxia
pra IP arbitrário, mesmo que seja um IP privado válido. Fecha a lacuna de
segurança e também é o que permite a Correção 4 (login automático), porque
agora o proxy sabe exatamente qual linha do inventário — e portanto qual
credencial salva — corresponde àquele IP.

**Achado durante a implementação (2026-09-05): isso não é lacuna nova, é
regressão.** Essa exata checagem (`_ip_belongs_to_current_tenant`) já tinha
sido corrigida e publicada em produção antes, num hotfix de auditoria de
segurança anterior (`docs/HANDOFF_AGENTES.md`, seção "Não reverter"), e já
tinha até um teste pronto (`scripts/sightops_camera_web_proxy_test.py`).
Confirmado ao vivo que o hotfix se perdeu num deploy posterior que partiu
de uma imagem anterior a ele — o mesmo padrão de drift já documentado
várias vezes neste projeto. A implementação reusa o nome de função já
estabelecido (`_ip_belongs_to_current_tenant`) e o teste já existente, em
vez de recriar algo novo.

### Correção 4 — login automático via credencial salva (só câmera)

Reaproveita `resolve_camera_password(ip, "", "")` (já existe, já resolve
senha salva por MAC/site pra câmera). Se existe senha salva:
- Primeira tentativa: `requests.request(..., auth=(user, password))` (Basic,
  padrão da biblioteca).
- Se o equipamento responder 401 com `WWW-Authenticate: Digest ...`, repete
  a mesma requisição com `requests.auth.HTTPDigestAuth(user, password)`.
- Se ainda assim vier 401 (senha errada/desatualizada), repassa o 401 (com
  `WWW-Authenticate`, Correção 2) pro navegador — o operador vê o diálogo
  nativo de login e pode digitar a senha certa manualmente, sem travar.

Equipamento com tela de login própria em JavaScript (não HTTP auth) não tem
como ser preenchido de forma confiável sem depender de comportamento
específico de cada marca/firmware — nesses casos o proxy só entrega a página
de login real (corrigida pelas correções 1-3), e o operador loga como
sempre logou. Documentado como limite conhecido, não como bug.

**DVR/NVR não tem login automático nesta primeira versão.** Investigação
confirmou que, diferente de câmera (`resolve_camera_password`, já existe),
não existe hoje nenhum armazenamento de usuário/senha por gravador no
sistema — a tabela `recorders` só guarda host/mac/modelo/fabricante, e um
comentário em `app/core/crypto.py` já registra cifrar senha de DVR como
"trabalho futuro". Criar esse armazenamento do zero é escopo à parte
(decisão do usuário, 2026-09-05): por ora, DVR/NVR ganha as Correções 1-3
(HTTPS com fallback, `WWW-Authenticate` repassado, checagem de posse no
inventário) — a tela de login real passa a abrir e funcionar de verdade,
só que a senha continua sendo digitada na hora, como sempre foi.

### Correção 5 — apontar os 3 lugares pro mesmo proxy

- `cameras.js:2489`: já usa o proxy, sem mudança de URL.
- `bootstrap.js:241`: troca `window.open('http://' + item.camera_ip, ...)`
  por `window.open(`${API_BASE}/api/maintenance/web/${encodeURIComponent(item.camera_ip)}/`, '_blank', 'noopener')`.
- `recorders.js:755`: troca `window.open('http://' + _recActive.host, ...)`
  pelo mesmo padrão, usando `_recActive.host`. A rota do proxy já é genérica
  por IP — não precisa saber se é câmera ou gravador, só precisa achar o IP
  no inventário certo (Correção 3 cobre os dois inventários).

## Fluxo de dados

1. Operador clica "Web" numa câmera/DVR/NVR — `window.open` navega a aba nova
   pra `/api/maintenance/web/{ip}/`.
2. O middleware de auth já resolve tenant pelo cookie `sightops_session`
   (mesmo mecanismo usado em toda navegação direta de aba hoje — nenhuma
   mudança de autenticação necessária).
3. `maintenance_camera_web_proxy` confere posse do IP no inventário do tenant
   atual (Correção 3); 403 se não achar.
4. Se for câmera, resolve credencial salva (Correção 4); monta a requisição
   HTTPS→HTTP (Correção 1) com Basic/Digest se houver senha. Se for DVR/NVR,
   monta a mesma requisição HTTPS→HTTP sem credencial nenhuma (sem
   armazenamento de senha pra gravador nesta versão).
5. Repassa a resposta, reescrevendo conteúdo (já existe) e headers
   (Correção 2), incluindo `WWW-Authenticate` quando presente.
6. Sub-requisições da própria página (JS/CSS/imagem/AJAX) batem na mesma rota
   com `path` preenchido — já funciona hoje pra esse parte, sem mudança.

## Segurança

- Nunca proxia pra IP que não esteja no inventário do tenant autenticado
  (Correção 3) — fecha o SSRF entre tenants que o código já documentava como
  risco conhecido em outro endpoint.
- Credencial nunca aparece no frontend nem em URL — só é usada no header
  `Authorization` da requisição servidor→equipamento, do mesmo jeito que
  outras rotas deste arquivo já fazem (ex.: `resolve_camera_password`).
- Cookie de sessão do SightOps (`sightops_session`) continua explicitamente
  removido antes de repassar cookies pro equipamento (`_camera_cookie_header`
  já faz isso, sem mudança).

## Tratamento de erro

- IP fora do inventário do tenant → `403`.
- HTTPS e HTTP falham os dois (equipamento off-line/rede indisponível) →
  `502`, mesma mensagem já usada hoje (`falha ao acessar web da camera {ip}`),
  ajustada pra mencionar qual dos dois esquemas foi tentado por último.
- 401 do equipamento com credencial salva errada → repassa o 401 real
  (com `WWW-Authenticate`) — navegador pede senha, sem mensagem de erro do
  SightOps no meio.

## Testes

Sem servidor HTTP real de câmera disponível em teste automatizado — cobertura
via um servidor HTTP local de mentira (biblioteca padrão `http.server`, sem
framework, seguindo o padrão de scripts deste repo) simulando:
- Um "equipamento" que exige Basic auth e aceita a credencial salva
  (confirma que o proxy loga sozinho, sem 401 chegando no navegador).
- Um "equipamento" que exige Digest.
- Um "equipamento" que só responde em HTTPS (confirma o fallback da
  Correção 1).
- IP fora do inventário do tenant atual (confirma o `403` da Correção 3).

Mais os três pontos de entrada do frontend: confirmar por leitura de string
(`grep`) que `bootstrap.js`/`recorders.js` não têm mais nenhum
`window.open('http://'` restante, igual ao padrão de verificação usado antes
nesta sessão pro fix do `Content-Type`/FormData.

## Fora de escopo

- Túnel de rede dedicado por sessão (Abordagem 2 do brainstorming) — motivo
  documentado acima, em Arquitetura.
- TR-069/CWMP — não se aplica: equipamento de CFTV não fala esse protocolo,
  e mesmo quando um dispositivo fala, TR-069 é pra automação/config em lote
  pelo lado do servidor, não pra abrir a interface web nativa pro operador.
- Login automático para equipamento com tela de login própria em JavaScript
  (não HTTP auth) — ver Correção 4.
- Armazenamento de senha por DVR/NVR (e, por consequência, login automático
  neles) — não existe hoje, é escopo à parte; decisão do usuário em
  2026-09-05 de deixar pra depois.
