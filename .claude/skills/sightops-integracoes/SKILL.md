---
name: sightops-integracoes
description: Mapeia as integrações e serviços do SightOps (cam-snapshot-web-v2) que dependem de infraestrutura real — rede, WireGuard/namespace isolado, proxy SOCKS5, go2rtc, Zabbix, banco, Docker — e varre o sistema atrás de bugs de acoplamento escondido, onde uma mudança estrutural num lugar quebra silenciosamente uma integração em outro arquivo que ninguém revisou. Use sempre que o usuário pedir para "levantar as integrações", "mapear o que depende do quê", "ver o que uma mudança pode quebrar", investigar por que algo que "sempre funcionou" parou depois de uma mudança de infra/rede/deploy, ou pedir uma varredura geral de bugs focada em acoplamento entre serviços (não segurança/SaaS — isso é a `sightops-audit`). Gatilho forte: qualquer menção a "achar tudo que usa X", "o que mais depende disso", ou um sintoma que já se repetiu mais de uma vez em partes diferentes do sistema pela mesma causa.
---

> **Diferença da `sightops-audit`**: a `sightops-audit` varre o sistema inteiro
> atrás de bugs/segurança/prontidão SaaS em geral. Esta skill é mais estreita
> e mais funda: foca especificamente em **acoplamento entre um serviço de
> infraestrutura e tudo que assume o comportamento dele** — o tipo de bug que
> só aparece quando uma coisa estrutural muda (rede, isolamento, porta, deploy)
> e quebra silenciosamente 3-4 integrações diferentes, uma de cada vez, ao
> longo de horas, porque nenhuma delas foi checada de propósito no momento da
> mudança. Se o usuário já tem um sintoma concreto isolado, use
> `sightops-bug-producao`. Se quer um raio-x de segurança/arquitetura geral,
> use `sightops-audit`. Se quer saber **o que mais quebra quando eu mexo
> nisto**, é esta aqui.

# SightOps — Mapa de Integrações e Caça a Bugs de Acoplamento

Repositório: `C:\PROJETOS\cam-snapshot-web-v2`.

## Por que esta skill existe (caso real, 09/09/2026)

Numa única sessão, isolar UM conector por namespace de rede (trabalho
legítimo, corrigindo um vazamento cross-tenant real) quebrou, um de cada vez,
sem que ninguém tivesse mapeado isso de antemão:

1. `sightops_wireguard_sync.py` (timer a cada 60s) recriava o peer do
   conector isolado na interface WireGuard **compartilhada**, porque não
   sabia que aquele conector tinha migrado — o roteador do cliente nunca
   conseguia ficar de fato no túnel isolado.
2. `ops/netns_provisioner/system_ops.py::ensure_namespace` configurava o
   WireGuard (`wg set ... allowed-ips`) mas nunca criava a rota de kernel
   nem a regra de MASQUERADE — o túnel "funcionava" (handshake) mas nenhum
   pacote de dados voltava pro cliente.
3. `app/services/camsnapshot/device_info.py::probe_device` (sondagem de
   MAC/fabricante/modelo, usada tanto pela varredura local quanto remota)
   nunca tinha suporte a `socks_proxy` — só `get_snapshot` tinha, de uma
   correção anterior (Task 6b). A varredura de um site isolado sempre
   devolvia só "online"/IP, nunca os dados reais.
4. O daemon `ops/netns_provisioner/daemon.py` nunca dava
   `systemctl enable --now` no `sightops-netns-proxy@<connector_id>.service`
   — o namespace/rota/masquerade ficavam perfeitos e **nada escutava do
   outro lado**. Toda chamada proxied (snapshot, PTZ, reboot, renomear,
   varredura) caía em "connection refused" engolido silenciosamente por um
   `except Exception: return None`, parecendo "a câmera não respondeu".
5. `go2rtc` (binário externo, live view) nunca teve nenhum suporte a proxy
   isolado — RTSP direto sempre vai falhar pra um site isolado, ponto ainda
   em aberto.
6. `app/services/zabbix_monitoring_service.py` faz seu próprio ping/TCP
   check **direto**, sem qualquer noção de conector/proxy, e grava
   `status_source: "zabbix"` no inventário — um valor que o resto do
   sistema trata como autoritativo e nunca sobrescreve. Zabbix sempre vai
   marcar um site isolado como offline, e essa marca gruda.

Nenhum desses 6 pontos apareceu na primeira tentativa de "consertar a rede".
Cada um só foi achado depois que o usuário reportou o MESMO sintoma de novo,
num lugar diferente. É exatamente esse padrão — uma mudança estrutural com
raio de explosão maior do que o mapeado — que esta skill existe pra pegar
ANTES, numa varredura deliberada, em vez de depois, um incidente de cada vez.

## O que "integração/infraestrutura" significa aqui

Qualquer serviço que faça UMA destas coisas conta como integração pra efeito
desta skill:

- Fala com algo fora do processo Python da API (rede, outro processo,
  binário externo, banco, fila, systemd, Docker, servidor remoto).
- Assume uma característica do AMBIENTE de rede (rota direta, IP/porta
  fixos, DNS, multicast/broadcast, mesma sub-rede).
- Roda em background/periodicamente, fora do ciclo request/response
  (timer systemd, thread, WebSocket de longa duração, cron).
- É consumido por MAIS de um chamador (se só um lugar usa, o raio de
  explosão de mudar é pequeno; se vários usam, é onde o bug se esconde).

Exemplos já conhecidos neste projeto (confirme se ainda procede — pode ter
mudado): WireGuard compartilhado (`wg-sightops`) e por-namespace (daemon
`ops/netns_provisioner/`), proxy SOCKS5 isolado (`connector_netns_proxy` em
`app/services/connector_service.py`), go2rtc/ffmpeg (live view), Zabbix
(`app/services/zabbix_monitoring_service.py`, `zabbix_access_service.py`),
OLT (Fiberhome/Huawei, drivers só leem), scan/inventário
(`ws_scan_service.py`, `scan_service.py`, `camsnapshot/`), Telegram
(alertas), ImgBB (upload de snapshot), Postgres vs SQLite (`DATABASE_BACKEND`),
os dois ambientes de produção paralelos (`sightops-prod-*` /
`sightops-v3-*`, ver `docs/HANDOFF_AGENTES.md` e memória do projeto).

## Processo

### 1. Escolha o ponto de partida

Duas formas de entrar nesta skill:

- **"O que mais depende de X?"** — o usuário aponta uma peça de infra
  específica (ex: "o proxy isolado", "o go2rtc", "essa mudança no Zabbix").
  Vá direto pro passo 2 com X fixado.
- **Varredura geral** — o usuário quer o mapa completo, ou pediu pra "achar
  os bugs" sem apontar uma peça. Enumere TODAS as integrações (seção acima)
  antes de aprofundar em qualquer uma.

### 2. Para cada integração: ache a fonte da verdade E todo mundo que assume o comportamento dela

Não basta achar ONDE a integração está implementada — o bug mora na
DIFERENÇA entre o que ela realmente garante hoje e o que cada chamador acha
que ela garante. Para cada uma:

1. **Leia a implementação real** (não confie em nome de função nem
   comentário antigo). Pergunte: o que isso garante de fato, hoje, olhando
   o código? (ex.: "`ensure_namespace` configura o WireGuard" não é o
   mesmo que "o tráfego chega no cliente" — só descobre lendo linha a
   linha se a rota/masquerade também são criados.)
2. **Grep por todo chamador**: `grep -rn "<nome_da_funcao_ou_simbolo>"
   app/ frontend/js/ ops/ scripts/` — literalmente todo lugar que importa
   ou invoca aquilo.
3. **Para cada chamador, pergunte**: ele está tratando o retorno/efeito
   colateral certo, ou assumindo uma garantia que a implementação real não
   dá (ex.: assumindo rota direta quando só existe proxy; assumindo que um
   valor vazio "" e `None` significam a mesma coisa; assumindo que uma
   exceção engolida por `except Exception: return None` significa "não
   respondeu" quando na verdade significa "erro de configuração/infra")?
4. **Ache o "silêncio"**: o padrão mais perigoso deste projeto é
   `try: ... except Exception: return None` (ou similar) em volta de uma
   chamada de rede — transforma QUALQUER falha (proxy não configurado,
   porta errada, serviço nunca subiu, credencial errada) na MESMA saída
   vazia, indistinguível de "dispositivo genuinamente offline". Todo
   lugar assim é candidato a esconder exatamente a classe de bug que essa
   skill procura — abra e confirme se dá pra diferenciar causa infra vs.
   causa real antes de reportar como "ok, só engoliu erro esperado".
5. **Cheque as DUAS pontas de um par**: quando a integração tem duas
   metades que precisam concordar (porta alocada vs porta que o serviço
   real escuta; endereço gravado no cadastro vs endereço realmente
   roteável; nome de campo que quem grava usa vs nome que quem lê espera
   — ex.: `connector_id` vs `remote_connector_id`, achado ao vivo hoje),
   confirme que as duas metades usam literalmente o mesmo valor. Um script
   pequeno que printa os dois lados lado a lado é mais confiável que ler
   os dois arquivos de cabeça.

### 3. Teste, não deduza, quando puder

Sempre que a integração envolve rede/processo real e o usuário já deu
acesso ao servidor de produção nesta conversa, prefira reproduzir o
problema com uma chamada mínima e real em vez de só ler o código e
concluir por inspeção — foi assim que os pontos 3 e 4 do incidente de hoje
foram confirmados (testar `probe_device` direto dentro do container, testar
o handshake SOCKS5 cru byte a byte, checar `systemctl status` do serviço).
Regras de segurança de produção da `sightops-audit`/memória do projeto
continuam valendo aqui: nunca rode comando destrutivo, nunca troque
container/faça deploy sem confirmação — leitura e teste read-only (ping,
`ss -tlnp`, `systemctl status`, chamar uma função Python pura dentro de um
`docker exec` sem side-effect) são seguros; qualquer coisa que mude estado
de produção pede confirmação primeiro.

### 4. Priorize por raio de explosão, não por severidade isolada

Um bug com 1 chamador é um bug pequeno. O mesmo bug de acoplamento com 6
chamadores diferentes (o padrão do incidente de hoje) é uma categoria de
risco maior, mesmo que cada chamador individual pareça "só um detalhe" —
porque cada novo caso de uso futuro (novo conector isolado, nova integração
que usa o mesmo proxy) herda o mesmo bug automaticamente. Ao reportar,
deixe claro quantos lugares dependem da mesma garantia quebrada.

## Saída obrigatória

# Mapa de Integrações — SightOps

## Integrações mapeadas
Para cada uma: nome, arquivo(s) que implementam, o que ela REALMENTE
garante hoje (confirmado lendo o código, não suposto), lista de todo
chamador encontrado via grep.

## Garantias quebradas / acoplamento arriscado
| Integração | Garantia assumida pelo(s) chamador(es) | Garantia real | Chamadores afetados | Como confirmar |
|---|---|---|---|---|

## Bugs confirmados (testados, não só deduzidos)
Para cada um: evidência (comando rodado + saída real), causa raiz, todo
lugar que precisa da mesma correção, correção mínima.

## Bugs suspeitos (não testados ainda)
Mesma coisa, mas sinalizado como hipótese — não afirme "é bug" sem ter
rodado algo real quando havia como rodar.

## Se o usuário apontou uma mudança específica
- O que mudou:
- Toda integração que depende do comportamento antigo:
- Qual delas já foi verificada/corrigida, qual ainda não:

## Próximos passos sugeridos
Ordenado por raio de explosão (quantos chamadores dependem da mesma
garantia), não por facilidade de correção.

## Regras
- Primeira passada é leitura/mapeamento — só altere código se o usuário
  pedir explicitamente ou se a correção for mecânica e de baixo risco
  (mesmo padrão da `sightops-resolver`).
- Nunca rode comando destrutivo nem troque algo em produção sem
  confirmação explícita do usuário nesta conversa.
- Não afirme "achei o bug" com base só em leitura de código quando dava
  pra testar de verdade (produção acessível, script isolado, container) —
  teste primeiro, reporte o resultado real.
- Se o mapa ficar grande, prefira grep/comandos reproduzíveis no relatório
  em vez de colar o conteúdo inteiro dos arquivos — quem for reler depois
  consegue rodar de novo pra confirmar que ainda procede.
