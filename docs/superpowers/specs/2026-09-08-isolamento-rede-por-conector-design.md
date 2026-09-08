# Isolamento de rede por conector — design

## Problema

Todos os conectores (túneis WireGuard de cada cliente) hoje compartilham a
mesma interface `wg-sightops` no servidor de produção
(`app/services/connector_service.py::ensure_wireguard_tunnel`). Cada
conector vira um peer nessa única interface, com a rede real do cliente
(`client_lans`) virando `AllowedIPs` daquele peer. WireGuard exige que
`AllowedIPs` de peers diferentes na mesma interface não se sobreponham.
Quando dois clientes têm a mesma faixa privada — o que é comum
(`192.168.1.0/24`, `192.168.10.0/24` etc.) — só um dos dois fica
corretamente roteável; o outro fica inacessível ou tem tráfego desviado
para a rede errada.

Confirmado ao vivo em produção: buscar o IP `192.168.10.201` no conector
"Mata Grande" devolveu o dispositivo do conector "Porto Real do Colégio",
porque os dois têm `192.168.10.0/24` na sua `client_lans` e compartilham a
mesma tabela de roteamento do kernel. Auditoria nos 9 conectores atuais
achou 17 IPs colidentes entre conectores diferentes — não é caso isolado.

Isso é um vazamento de dado entre clientes (cross-tenant), classificado
pelo usuário como risco de responsabilidade legal. Requisito explícito e
não negociável: **em hipótese alguma um cliente pode se misturar no outro,
nunca, em nenhuma forma.**

Os dois pontos de código confirmados que fazem requisição direta por IP
sem escolher explicitamente o túnel do cliente:
- `app/services/device_web_proxy.py::fetch_device()` — usado pelo proxy de
  câmera já em produção.
- `app/api/endpoints/dvr.py::api_dvr_network_get`/`api_dvr_network_apply`
  — a aba "Rede", publicada nesta mesma semana.

Pode haver outros pontos com o mesmo padrão; a Fase 4 (abaixo) inclui uma
varredura completa do código antes de considerar o trabalho fechado.

## Decisão de arquitetura

Isolamento real via **network namespace por conector** (`ip netns`), não
apenas separação lógica em software. Cada conector recebe:

1. Seu próprio network namespace (`ns-<connector_id>`).
2. Sua própria interface WireGuard dentro desse namespace
   (`wg-<connector_id>`), reaproveitando a MESMA identidade de servidor
   (mesma `PrivateKey` do servidor, já existente em
   `/etc/wireguard/wg-sightops.conf`) — só a porta de escuta muda, uma
   porta dedicada por conector. Reaproveitar a chave existente significa
   que nenhum roteador de cliente precisa ser reconfigurado.
3. Nesse namespace, só o peer daquele cliente é configurado. Como cada
   namespace tem sua PRÓPRIA tabela de roteamento do kernel, duas redes de
   clientes idênticas (`192.168.10.0/24` em dois lugares) nunca colidem —
   fisicamente não há como um pacote destinado à rede do Mata Grande
   atravessar para a rede do Porto Real, porque são tabelas de roteamento
   diferentes, não apenas regras de aplicação diferentes.
4. Um par `veth` ligando o namespace raiz (onde a aplicação roda) a esse
   namespace isolado, com endereçamento link-local dedicado por conector
   (bloco `169.254.<octeto_alocado>.0/30`).
5. Um proxy SOCKS5 minimalista (Python stdlib puro, só `CONNECT`, sem
   autenticação — já escrito e testado nesta sessão) rodando DENTRO do
   namespace isolado, alcançável só pela `veth` daquele conector.

Isso já foi construído manualmente e validado ao vivo para o conector Mata
Grande nesta sessão: uma requisição ao IP `192.168.10.201` pelo proxy
isolado do Mata Grande dá "conexão recusada" (chegou na rede REAL do Mata
Grande, que não tem nada respondendo naquele endereço), enquanto o caminho
antigo compartilhado devolve HTTP 200 do dispositivo do Porto Real — prova
de que os dois caminhos levam a redes fisicamente diferentes.

O que falta é tornar esse mecanismo automático, persistente e integrado ao
código da aplicação, em vez de scripts rodados à mão como root.

### Por que não outras abordagens

- **Regra de firewall/iptables por conector na interface única**: ainda
  depende de acertar 100% das regras sempre, para sempre, incluindo em
  todo código futuro que fizer uma requisição — um único ponto esquecido
  reabre o vazamento. Namespace torna o erro estruturalmente impossível:
  mesmo que o código erre, fisicamente só alcança a rede daquele cliente.
- **VPN completa por conector com IP público/porta própria dedicada,
  sem namespace**: resolve a colisão de rota, mas não isola de verdade —
  ainda é uma única tabela de rotas do SO se as interfaces estão todas no
  namespace raiz. Namespace é o mecanismo padrão do Linux exatamente para
  este problema (múltiplas tabelas de roteamento independentes).

## Componentes

### 1. Daemon provisionador (privilegiado, roda no host como root)

Nome: `sightops-netns-provisioner`. Um processo Python simples, sem
dependências externas, escutando em um socket Unix
`/run/sightops/netns-provisioner.sock` (modo `0660`, dono `root`, grupo
`sightops-api` — o usuário que roda o processo dentro do container precisa
pertencer a esse grupo, ou o socket é montado com bind mount e permissão
ajustada no `docker-compose`).

Protocolo: linhas JSON, uma requisição por linha, uma resposta por linha
(request/response síncrono, sem streaming). Motivo de escolher isso em vez
de HTTP sobre socket Unix: zero dependência nova, fácil de auditar em uma
tela, compatível com `ctypes`-free Python puro (mesmo padrão de simplicidade
usado no proxy SOCKS5 já escrito).

Operações suportadas:

```jsonc
// request
{"op": "provision", "connector_id": "3315d77dfdedb2ee",
 "server_private_key": "<vem do wg-sightops.conf, nunca logado>",
 "peer_public_key": "1iDptEuSPwlUHOBVoB5h2lnWFW+QBkB4OOPAbUUrJUI=",
 "allowed_ips": ["10.250.0.10/32", "172.16.20.0/24", "192.168.10.0/24"]}
// response (sucesso)
{"ok": true, "listen_port": 52001, "veth_subnet": "169.254.100.0/30",
 "proxy_host": "169.254.100.2", "proxy_port": 8080}
// response (erro)
{"ok": false, "error": "namespace ja existe com config diferente", "code": "conflict"}

// request
{"op": "deprovision", "connector_id": "3315d77dfdedb2ee"}
// response
{"ok": true}

// request
{"op": "status", "connector_id": "3315d77dfdedb2ee"}
// response
{"ok": true, "exists": true, "listen_port": 52001, "proxy_host": "169.254.100.2",
 "proxy_port": 8080, "systemd_active": true}
```

`provision` é **idempotente**: se o namespace já existe com a config
pedida, devolve sucesso sem recriar nada; se existe com config diferente
(ex.: peer trocou), reconfigura o peer WireGuard existente (`wg set`) sem
recriar namespace/veth/porta — evita realocar recursos à toa quando o
cliente só troca de LAN.

Alocação determinística de recursos (porta WireGuard, sub-rede veth) é
derivada do `connector_id` via hash estável, com checagem de colisão contra
um arquivo de estado local do próprio daemon
(`/var/lib/sightops-netns-provisioner/allocations.json`) — não depende do
`connectors.json` da aplicação, o daemon é autossuficiente. Faixas
reservadas: portas WireGuard `52000-52999` (1000 conectores possíveis,
folga generosa sobre os 9 atuais), sub-redes veth
`169.254.100.0/24` em blocos `/30` (64 conectores por octeto, expande para
`169.254.101.0/24` etc. se esgotar).

O daemon roda como `systemd` service próprio
(`sightops-netns-provisioner.service`), `Restart=on-failure`, e ao subir
faz uma varredura (`op: status` interno) recriando o namespace de qualquer
conector cujo estado gravado não bate com o namespace real do kernel
(recuperação após reboot do host, ver Fase de persistência abaixo).

**Superfície de ataque**: o socket só aceita as 3 operações acima, todas
recebendo `connector_id` + dados já validados pela aplicação (que já roda
como usuário sem privilégio dentro do Docker); o daemon nunca executa
comando arbitrário vindo do cliente do socket — os únicos comandos de
sistema que ele roda (`ip netns add`, `ip link`, `wg set`) usam parâmetros
formatados internamente a partir de campos já validados (regex de
`connector_id`, CIDR parseado com `ipaddress` antes de virar string de
comando), nunca interpolação direta de string vinda da rede.

### 2. Persistência via systemd

Para cada conector provisionado, o daemon garante que existe e está ativo
um `sightops-netns-proxy@<connector_id>.service` (template unit), que só
faz `ip netns exec ns-<connector_id> python3 <script_proxy> <porta>` —
`Restart=always`, `After=network.target`. Isso substitui o processo solto
em background usado no protótipo manual desta sessão: sobrevive a reboot
do host e a crash do proxy individual sem afetar os demais conectores
(cada `.service@instância` é independente).

A interface WireGuard dentro do namespace **não** precisa de unit própria
— é criada pelo próprio daemon no `provision` e persiste enquanto o
namespace existir; o daemon recria o namespace inteiro (interface + peer)
no boot, antes de subir os serviços de proxy, via `ExecStartPre` do
próprio `sightops-netns-provisioner.service` rodando sua varredura de
recuperação.

### 3. Integração em `ensure_wireguard_tunnel`

Em `app/services/connector_service.py`, depois de gerar/confirmar o
keypair do peer e o `client_address` (lógica que já existe e não muda), a
função chama o provisionador pelo socket Unix (`op: provision`) passando
`connector_id`, a chave privada do servidor (lida de onde já é lida hoje),
a chave pública do peer e o `client_lans` calculado. Grava no registro do
conector (`connectors.json`, dentro do dict `tunnel`):

```jsonc
"tunnel": {
  ...campos existentes sem mudança...,
  "netns_proxy_host": "169.254.100.2",
  "netns_proxy_port": 8080,
  "netns_listen_port": 52001
}
```

Se a chamada ao socket falhar (daemon fora do ar, socket não montado):
**a criação/atualização do conector falha** com erro claro — não existe
fallback silencioso para o caminho antigo compartilhado. Dado o requisito
de "nunca misturar", um conector sem isolamento provisionado não deve
poder operar; é preferível um erro visível na hora do cadastro a um
vazamento silencioso depois. Isso vale tanto para conector novo quanto
para reconfiguração de um existente (rotina já idempotente do lado do
daemon, então reexecutar `ensure_wireguard_tunnel` num conector já
provisionado é seguro e barato).

### 4. Mudança nos pontos de acesso por IP

`app/services/device_web_proxy.py::fetch_device()` e
`app/api/endpoints/dvr.py` (`api_dvr_network_get`/`api_dvr_network_apply`,
e qualquer outro achado na varredura da Fase 4) passam a exigir
`connector_id` como parâmetro. Com o `connector_id`, a função busca
`tunnel.netns_proxy_host`/`netns_proxy_port` no registro do conector e
monta a requisição usando esse proxy SOCKS5
(`requests` com `proxies={"http": f"socks5h://{host}:{port}"}`) em vez de
bater direto no IP. Se o conector não tiver `netns_proxy_host` gravado
(caso de transição, antes da migração completa — ver Fase 5), a função
recusa a requisição com erro explícito em vez de cair para o caminho
antigo compartilhado — de novo, por causa do requisito de isolamento
absoluto.

Toda rota HTTP que hoje chama essas funções sem passar `connector_id`
precisa ser atualizada para descobrir e passar esse `connector_id` (a
maioria já tem essa informação disponível — vem da tela, que já sabe qual
conector/site está sendo consultado).

### 5. Auditoria

Cada `provision`/`deprovision` bem-sucedido grava uma linha em
`/var/log/sightops-netns-provisioner/audit.log` (append-only, rotação via
`logrotate` padrão do sistema): timestamp UTC, `connector_id`, operação,
resultado. Cada requisição roteada via `fetch_device`/`dvr.py` já cai nos
logs de aplicação existentes (que já registram `connector_id`); não é
necessário log adicional ali, só garantir que o `connector_id` usado para
selecionar o proxy fique nessa linha de log existente, para rastreamento
posterior.

## Fases de execução (migração seguro)

**Fase 1 — build aditivo, sem tocar nos conectores existentes.** Escrever
e publicar o daemon + systemd units, sem chamar de `ensure_wireguard_tunnel`
ainda. Formalizar (via script, não mais manual) o que já foi feito à mão
para o Mata Grande nesta sessão, e usar esse mesmo conector como teste do
daemon novo — comparando que o resultado bate com o protótipo manual.

**Fase 2 — migrar os dois conectores que já colidiram** (Mata Grande,
Porto Real do Colégio) via o daemon novo, mantendo por enquanto o peer
deles TAMBÉM na interface `wg-sightops` compartilhada (não remover ainda) —
só o código da aplicação (`fetch_device`/`dvr.py`) passa a rotear esses
dois pelo proxy isolado. Validar ao vivo que os dois voltam a responder
certo, cada um para o seu lado, sem cruzamento algum.

**Fase 3 — remover os peers do Mata Grande e Porto Real do Colégio da
interface compartilhada `wg-sightops`** (fecha de vez a rota antiga
colidente) e confirmar que nada mais depende dela para esses dois.

**Fase 4 — varredura completa do código** por qualquer outro lugar que
acesse dispositivo por IP sem `connector_id` (não só os dois já
confirmados) — obrigatória antes de considerar a correção completa, dado
que o requisito do usuário é "nunca, em nenhuma forma".

**Fase 5 — migrar os 7 conectores restantes**, um de cada vez, mesmo
padrão da Fase 2+3 (adicionar isolado → validar → remover do
compartilhado), até `ensure_wireguard_tunnel` nunca mais colocar peer
nenhum na interface `wg-sightops` compartilhada. Depois disso a interface
compartilhada pode ser aposentada (ou mantida vazia como watchdog: qualquer
peer que aparecer nela de novo, no futuro, por erro de código, é sinal de
regressão).

**Todo conector cadastrado a partir do fim da Fase 1 já nasce isolado
automaticamente** — não depende de esperar as fases seguintes.

## Rollback

Se o `provision` falhar no meio (ex.: container do daemon reinicia entre
criar o namespace e configurar o peer), a próxima chamada de `provision`
para o mesmo `connector_id` detecta o estado parcial (namespace existe mas
sem peer configurado, ou sem veth) e completa/corrige em vez de falhar —
o daemon sempre reconcilia para o estado desejado a partir do que
encontra, nunca assume que "não existe" ou "existe completo" são os únicos
dois estados possíveis.

Se a aplicação chamar `provision` e não conseguir gravar
`netns_proxy_host` no `connectors.json` depois (crash entre as duas
etapas), o conector fica com o namespace provisionado mas sem o registro
apontando pra ele — inofensivo (o namespace só é usado quando referenciado
pelo `connector_id`), e a próxima tentativa de `ensure_wireguard_tunnel`
resolve sozinha (idempotência do `provision`).

## E se o servidor mudar de máquina (VPS nova, troca de hardware)?

Toda a arquitetura acima é software/configuração padrão do Linux — nada
fica preso a esta máquina específica. Numa migração:

- **O que precisa ser levado**: a chave privada WireGuard do servidor
  (arquivo único, hoje em `/etc/wireguard/wg-sightops.conf`), o
  `connectors.json` (já portátil, é dado da aplicação), e os artefatos do
  provisionador (`sightops-netns-provisioner.service`, o script do daemon,
  o script do proxy SOCKS5) — tudo isso é reproduzido rodando o mesmo
  processo de instalação na máquina nova.
- **Levando a mesma chave privada do servidor**, nenhum roteador de
  cliente precisa ser reconfigurado — eles continuam confiando na mesma
  identidade de servidor, só muda o IP público de destino.
- **O que muda de verdade**: o IP público que cada roteador de cliente
  disca hoje (`201.182.184.84`). Recomendação, independente desta
  migração de isolamento: registrar um nome de domínio (ex.
  `vpn.easytecnologias.com.br`) e usar esse nome nos roteadores dos
  clientes daqui pra frente, em vez do IP cru — assim uma troca futura de
  servidor vira só apontar o DNS pro IP novo, sem tocar em roteador de
  cliente nenhum.
- O daemon provisionador e os namespaces por conector são recriados do
  zero na máquina nova pelo processo de provisionamento normal
  (`ensure_wireguard_tunnel` roda de novo para cada conector, ou um script
  de re-provisionamento em lote lendo o `connectors.json` existente) — não
  há estado manual "só naquela máquina" que precise ser copiado à mão além
  dos dois arquivos citados acima.

## Testes/validação

- Teste de isolamento automatizado: para cada par de conectores com
  `client_lans` colidentes (a varredura já escrita nesta sessão pode virar
  um script de teste permanente), confirmar por dois `curl` via os dois
  proxies isolados que cada um alcança sua própria rede e nenhum alcança a
  do outro.
- Teste de idempotência: chamar `provision` duas vezes seguidas para o
  mesmo conector, confirmar que não recria namespace nem derruba conexões
  em andamento.
- Teste de recuperação: reiniciar o host (ou simular removendo o
  namespace manualmente) e confirmar que o daemon o recria sozinho ao
  subir.
- Teste de regressão: depois da Fase 5, confirmar que `wg show
  wg-sightops` não lista peer nenhum (interface compartilhada vazia).
