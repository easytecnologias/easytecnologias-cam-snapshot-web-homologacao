# Handoff entre agentes (Claude / Codex)

Registro de tarefas médias/importantes (produção, banco, Zabbix,
conectores, KMZ, ONU/OLT, inventário, ou qualquer coisa que outro agente
possa sobrescrever sem saber). Tarefa pequena não entra aqui — fica só na
resposta final do agente pro usuário. Entrada mais recente no topo.

---

## 2026-08-26 — WhatsApp migrado para a Cloud API oficial; Evolution e W-API removidos

**Agente:** Claude.

**Codex: isto conflita com o que voce fez hoje.** O commit `41e9a65` trouxe
suporte a W-API, e havia 160 linhas de teste do provedor Evolution na working
tree. Removi os dois, com autorizacao explicita do usuario ("pode tirar qualquer
coisa de apis gratuitas so estamos com a oficial"). Os testes daqueles provedores
foram substituidos por um da Cloud API em
`scripts/sightops_access_control_notifications_test.py`.

**Por que:** a Evolution passou uma noite inteira aceitando mensagens (HTTP 201,
ack do servidor) sem entregar nenhuma. O WhatsApp devolvia ERRO em todas, e a
Evolution engolia o aviso -- o banco ficava em PENDING para sempre e a tela dizia
"Conectado" com a sessao morta. Diagnostico so fechou olhando o sufixo das chaves
no Redis (`_0` = ERROR). Causa raiz: Baileys `rc.9` desatualizado para o protocolo
atual; a correcao existe em `rc13`, que so esta numa imagem `homolog` que nao
sobe (bug de Prisma). Ou seja: nao havia caminho por ali.

**O que mudou no codigo:**
- `access_control_notifications.py`: provedor unico `cloud_api`. Removidas
  `_send_whatsapp_evolution`, `_send_whatsapp_wapi`, `_evolution_cfg`,
  `_evolution_state_label`, `disconnect_access_whatsapp`, `_whatsapp_base_url` e o
  watchdog de sessao. Envio agora e por **template aprovado** com 4 variaveis
  (evento, escola, aluno, horario) -- a Cloud API nao aceita texto livre em
  mensagem iniciada pela empresa.
- `access_control.py`: endpoints `/whatsapp/qr` e `/whatsapp/disconnect` removidos
  (nao existe QR nem sessao no canal oficial). Adicionados `GET` e `POST`
  `/whatsapp/meta/{tenant}` -- webhook da Meta.
- `security.py`: `/api/access-control/whatsapp/meta/` liberado da autenticacao
  (a Meta chama sem credencial; a protecao e o token de verificacao conferido
  dentro do endpoint).
- `access_control_whatsapp_inbound.py`: `extract_meta_inbound` e
  `extract_meta_statuses`. O `process_access_whatsapp_inbound` aceita os dois
  formatos, entao a triagem sobreviveu.
- Frontend: painel de QR substituido por bloco de status do canal (numero, nome
  exibido, template e sua situacao, qualidade da conta).

**Multi-cliente, atencao:** o webhook da Meta e **um so para o app inteiro**. Quem
separa cliente e o `phone_number_id` que recebeu -- `resolver_cliente_por_numero`
percorre os tenants e casa pela configuracao. O slug da URL e so reserva. Se o
numero nao pertencer a ninguem configurado, o webhook **ignora e responde 200**
(erro faria a Meta desativar o webhook). Esse resolvedor e O(tenants) por
mensagem: serve para os 4 clientes de hoje, precisa de indice antes de escalar.

**Estado na Meta (conta da Easy Tecnologia):** empresa verificada, app publicado,
numero +55 82 9369-0487 "Escola Segura" inscrito, pagamento configurado, webhook
validado e app inscrito na WABA. Falta so o template `aviso_acesso_aluno` sair de
"em analise" -- quando aprovar, o envio funciona sem mexer em nada.

**Armadilha que custou tempo, para nao repetir:** criar template mandando JSON com
acentos pela linha de comando no Windows grava `?` no lugar do acento (o shell nao
entrega UTF-8). O primeiro template foi criado com "Hor?rio" e precisou ser
refeito. Monte o payload em Python, grave em arquivo e mande com `--data-binary`.

**Tambem entrou neste commit:** as correcoes de tunel do controle de acesso da
noite de 25/08, que ate agora existiam **so na imagem de producao**
(`access_control_device.py`, +96 linhas): leitura e escrita falam direto com a
controladora pelo WireGuard quando o IP consta no inventario **daquele** conector,
porque o agente RouterOS nao faz Digest. `_TunelIndisponivel` separa "o tunel nao
entregou" de "o dispositivo recusou" -- antes os dois viravam 502 e a recusa
legitima da controladora ("Batch Process Error", que so pede updateMulti) era
tratada como falha de rede.

---

## 2026-08-21 (madrugada) — fechamento da noite: producao alinhada e mapas de volta

**Agente:** Claude. Tres pendencias que ficaram abertas na noite anterior foram
fechadas, todas verificadas:

**1. `.env.production` alinhado.** Apontava `20260819-podasegura` enquanto o
container rodava `20260820-mapaseletor`: um `docker compose up` qualquer
reverteria driver VSOL, correcao do mapa e tela nova de uma vez. Agora
`docker compose config` resolve para a mesma imagem que esta no ar. Backup:
`.env.production.bak-20260821`.

**2. Coordenadas devolvidas aos tres sites** (as 147 que o bug havia carimbado
com o mapa de Japaratinga e que tinham sido limpas):

```
SANTANA              -> 224 atualizadas | 0 sem ponto | 306 de outros sites intocadas
BARRA DE SAO MIGUEL  -> 180 atualizadas | 0 sem ponto | 350 intocadas
ESCOLA MEDEA         ->  24 atualizadas | 1 sem ponto | 505 intocadas
```

530 linhas antes, 530 depois. Conferencia final: nenhuma camera de outro site
dentro da area de Japaratinga. Backup em
`/app/output/backup-inventario-rads-20260821-011343.json`. PRAIA BONITA (40)
segue sem coordenada -- nao existe KMZ dela importado.

**3. Regra TEMP de NAT removida do Mikrotik.** So depois de provar que a camera
nao dependia mais dela: ping com origem `179.97.32.6` (fora do /23) deu 0% de
perda antes de remover, e `100.66.11.31` responde pelo tunel sem NAT nenhum.

**Git:** dois commits, um por autoria -- `ed5c5ed` (VSOL + mapa + tela, meu) e
`10210ea` (controle de acesso, Codex). O repo voltou a importar: faltava
restaurar `camera_allowlist.py` e corrigir quatro nomes do 4840e que
`olt_service` importava de `olt_vsol_epon`. 53 de 54 testes passam; a unica
falha e `sightops_access_control_shell_test.py` (espera 4 KPIs, a tela ja tem
8) -- do bloco do Codex.

**O push nao pode ser feito por mim** (o classificador de permissoes bloqueia
publicacao externa). Fica para o usuario: `git push origin main`.

**Pendencia de seguranca, anterior a esta noite:** a senha do servidor esta em
texto puro em `scripts/sightops_access_control_*_test.py` e no proprio HANDOFF,
e **ja esta no historico do git**. Apagar dos arquivos nao resolve -- precisa
trocar a senha e passar a le-la do ambiente.

---

## 2026-08-20 (noite) — OLT VSOL homologada, faltando o MAC do CPE

**Agente:** Claude — **PASSAGEM DE BASTAO, tarefa inacabada**

### Atualizacao (20/08, madrugada) — driver ja coleta o CPE, falta provar na OLT

**Agente:** Claude. **Estado: escrito e testado no repo, NAO rodou na OLT e NAO
foi para producao.** Nao ha nada em producao diferente do que ja estava.

O que foi feito em `app/cli/tools/olt_vsol_epon.py`:

- `parse_onu_mac_table()` — le `show onu <id> mac-address-table`.
- `_le_macs_da_onu()` — roda o comando dentro do contexto da PON, como os
  outros `show onu ...` deste driver.
- `_linhas_por_cpe()` — explode cada ONU em **uma linha por CPE** (era uma linha
  por ONU com `cpe_mac: ""`). ONU offline nao e consultada; ONU sem CPE
  aprendido sai com o proprio MAC como chave (`cpe_source: onu-sem-trafego`),
  para nao sumir do relatorio — mesmo criterio do driver 4840e.

Teste sem OLT: `python scripts/sightops_olt_vsol_cpe_mac_test.py` (canal falso,
6 verificacoes, passando). Cobre dois formatos plausiveis de tabela, tabela
vazia, exclusao do MAC da propria ONU e ONU offline continuando visivel.

**Formato real ja capturado na OLT** (probe read-only, PON 0/1, ONUs 1 e 2):

```
epon-olt(config-pon-0/1)# show onu 1 mac-address-table
 Mac Address Table
----------------------------------------------------------
Index   VLAN   MAC  Address         PON       ONU    Aging(s)
1       1000   54:6c:ac:25:e6:cf    EPON0/1   1      255
2       1000   54:6c:ac:25:e8:1a    EPON0/1   1      255
3       1000   98:2a:0a:4b:a5:71    EPON0/1   1      255
4       1000   54:6c:ac:25:e8:1c    EPON0/1   1      255

 Total Addresses Found in System :4
```

O comando funciona, roda dentro do contexto da PON e a PON 0/1 sozinha tem 8
ONUs (6 online), 4 CPEs cada. A saida real virou fixture do teste. Detalhe que
justifica nao contar colunas: a linha tem TRES inteiros (Index, VLAN, Aging) --
`_vlan_da_linha` pega o token a esquerda do MAC, senao viria 1 ou 255.

**O cruzamento foi PROVADO antes do deploy (nada gravado):** li as 4 PONs,
juntei os CPEs de todas as ONUs online e cruzei com
`_known_mac_ip_index(connector_id="bdd51284f07594d9", site="JAPARATINGA")`:

```
CPEs distintos lidos na OLT      : 57
CPEs que casam com o indice      : 57   (100%)
IPs resolvidos                   : 100.66.11.x
```

Ou seja a trava vai passar (`matched_connector_ips = 57`, nao 0). E como o
inventario de OLT do tenant `rads` **nao tem nenhuma camera do site
JAPARATINGA** hoje (so SANTANA 224, BARRA 180, PRAIA BONITA 40, ESCOLA MEDEA
25), essas 57 linhas entram por `_sync_camera_inventory_from_olt_rows` como
cameras **criadas** ja com PON/ONU preenchidos.

Nao precisa rodar scan no site: os MACs ja sao conhecidos por outro caminho --
`inventario-switch` (61 entradas) e `arp_sample`/`dhcp_sample` do proprio
conector (36).

**Alinhamento de campos, feito junto:** o cruzamento procura nomes fixos
(`onu_model`, `oper_status`, `omci_status`) e este driver produzia `modelo` e
`up`/`down`. A camera casaria pelo MAC e ficaria sem modelo e sem estado, em
silencio. `_campos_de_topologia` traduz para o vocabulario do 4840e
(`Active`/`Offline`, `OK`/`LOS`) e o teste trava esses nomes.

**Base de producao conferida:** o `olt_vsol_epon.py` dentro de
`sightops-prod-api:20260820-vsol` tem as mesmas 545 linhas e as mesmas funcoes
do arquivo local (md5 `70cdeacd...`), entao o arquivo local e a base + as
mudancas desta sessao. Copiar o arquivo inteiro na imagem nova e seguro; nao ha
import novo.

**DEPLOY FEITO** — imagem `sightops-prod-api:20260820-vsol-cpemac`, publicada
com `deploy_api.py` (nao com `compose up`). Container `sightops-prod-api` no ar,
healthy, `api=401` / `front=200`, driver novo confirmado dentro da imagem em uso.
Validacao antes da troca: o teste do repo e um script de import da aplicacao
inteira rodaram DENTRO da imagem nova, com producao intocada.

**PENDENCIA que nao e desta mudanca, mas morde:** o `.env.production` aponta
`CAM_SNAPSHOT_IMAGE=sightops-prod-api:20260819-podasegura`, enquanto o container
roda a `-cpemac`. O `deploy_api.py` nao mexe nesse arquivo, entao eles vivem
dessincronizados. Qualquer `docker compose up` devolve producao para a imagem de
19/08 e o driver VSOL some junto. Deixado como esta por ser escrita em arquivo
de producao -- decidir com o usuario.

Resto: sobrou um container `sightops-prod-api-20260820-fonterede` em estado
`Created` (deploy anterior, nao deste). Fora da rede, inofensivo, mas e lixo.

### "Testar conexao" da VSOL falhava — corrigido no mesmo dia

Sintoma na tela de OLT: `Falhou`. Detalhe salvo no registry:
`500: Erro ao descobrir ONUs na OLT: Socket is closed`.

Causa: o botao chama `discover_onus`, e o `olt_service.py` **de producao** so
conhecia VSOL no `collect_macs` — os outros caminhos caiam no driver Intelbras,
que fala comando que esta OLT nao entende e derruba a sessao. Pior: o
`olt_capabilities.py` de producao **ja anunciava** `discover_onus`, `telemetry`,
`find_onu` e `onu_signal` para `vsol_epon`. A capability prometia e o servico
nao entregava.

Corrigido na imagem `sightops-prod-api:20260820-vsol-rotas` (patch aplicado
sobre os arquivos extraidos da imagem no ar, nao sobre o repo):

- `_is_vsol()` no olt_service (o criterio era inline, agora quatro operacoes
  usam o mesmo);
- desvio VSOL em `discover_onus`, `collect_onu_telemetry`, `find_onu` e
  `onu_signal`;
- `_discover_vsol_por_pon()` — o driver devolve lista unica de ONUs, a tela
  espera mapa por PON com a chave `discovered`;
- `api/endpoints/olt.py`: o teste de conexao da VSOL passa a usar `collect_macs`
  como a 4840e. Motivo: `show onu discover` lista so ONU NAO autorizada, entao
  numa OLT ja provisionada volta vazio e o teste diria "0 PON(s)" com tudo
  funcionando.

**Consequencia que precisa estar clara:** "Testar conexao" na VSOL agora faz a
coleta completa — demora minutos e ESCREVE inventario (deve criar as 57
cameras). E o mesmo comportamento que a 4840e ja tinha (na tela ela mostra
"Conexao OK - 258 M").

Validado antes do deploy dentro da imagem: app carrega, os 4 caminhos existem,
`_is_vsol` nao confunde 4840E com VSOL, e o formato da descoberta e o que o
endpoint conta. Nao consegui exercitar o discover contra a OLT real por fora --
`docker cp`/`exec` para dentro do container de producao esbarra no classificador
de permissoes. O teste de verdade e clicar o botao.

### Resultado final medido em producao

A coleta rodou pela tela e o registry gravou `ok - 60 MAC(s) coletado(s)`:

```
linhas da VSOL no inventario : 60   (57 CPEs + 3 ONUs offline pelo fallback)
linhas com IP resolvido      : 57
cameras de JAPARATINGA       : 61
cameras com PON + ONU        : 56
```

Exemplo de linha completa: `cpe_mac 54:6c:ac:25:e6:cf | ip 100.66.11.28 |
pon 0/1 | onu_id 1 | onu_model R1v2 | oper_status Active | vlan 1000 |
cpe_source mac-address-table`.

**Correcao de uma previsao errada minha:** eu disse que nasceriam 57 cameras
novas. Nao nasceram — as 61 cameras de JAPARATINGA **ja existiam** no inventario
(vieram do inventario de switch, com nome de verdade: "38 - GCM 1", "40 -
ENTRADA BASE"). O que a coleta fez foi **enriquecer** as existentes com a
topologia. `created_cameras` foi zero.

As 5 sem vinculo nao sao falha: 4 delas (GCM 1, GCM 2, ENTRADA BASE, INTERNA CCO
BASE) tem MAC que **nenhuma ONU aprendeu** — sao da base/CCO, ligadas em switch,
nao atras de ONU. A quinta (100.66.11.31) esta sem MAC no cadastro, e sem MAC
nao ha o que cruzar.

As 3 ONUs offline (0/1:7, 0/1:8, 0/2:2) entraram com o MAC da propria ONU, que e
o comportamento desenhado -- ficar de fora do relatorio seria pior.

### Mapa/KMZ carimbava coordenada de um site em camera de outro

Relato do usuario: "cadastro camera nova, mando carregar o mapa e ele apaga o
inventario". **Nao apaga** -- medido: 530 linhas antes, 530 depois, e o
`apply_locations_to_inventory` devolve toda linha, inclusive sem match.

O que acontecia de verdade e pior: o apply cruzava o KMZ contra o inventario
**inteiro**, sem filtro de site, e um dos criterios de match e o numero no inicio
do titulo. `"1 - ESCOLA NOSSA SENHORA SANTANA"` casava com `"1 - SPEED ORLA"` so
porque ambos comecam com 1. Com "sobrescrever" marcado, o KMZ de um site
espalhava coordenadas pelo resto.

**Estrago ja gravado, medido em 20/08:** 147 cameras de outros sites estavam
posicionadas dentro da area de Japaratinga:

```
JAPARATINGA          ->  60  (correto)
BARRA DE SAO MIGUEL  ->  61  (errado)
SANTANA              ->  61  (errado)
ESCOLA MEDEA         ->  25  (errado -- fica em Barra de Sao Miguel)
```

**Correcao (imagem `sightops-prod-api:20260820-kmzsite`)**, em `kmz_ops.py` +
`api/endpoints/tools.py`:

- `detect_kmz_site()` descobre o site do mapa pelos nomes dos pontos. So o match
  FORTE (nome inteiro) vota, porque e o unico que nao vaza: o KMZ de Japaratinga
  deu 60 votos nela e **zero** em qualquer outro site.
- linha de outro site passa intacta (contada em `fora_do_site`).
- `site` no payload e conferido contra o detectado; divergencia vira **400 com
  texto**, nao carimbo silencioso.
- sem site reconhecido, o criterio fraco (numero) fica **proibido** -- nome
  inteiro e IP continuam valendo.

Antes/depois com o mesmo KMZ e os mesmos dados: Japaratinga seguia com 60, e
Barra/Santana/Medea foram de 61/61/25 para **0/0/0**.

Teste: `python scripts/sightops_kmz_site_isolado_test.py` (6 verificacoes, com os
nomes reais que colidiram). A mesma correcao esta no repo e em producao.

**Dados ainda por reparar:** as 147 coordenadas erradas continuam gravadas.
Backup em `/app/output/backup-inventario-rads-20260820-212236.json` (530 linhas,
489 com coordenada). O reparo pela tela e reimportar o KMZ de cada site e
aplicar COM "sobrescrever" -- agora que o filtro existe, cada mapa so encosta no
proprio site e as coordenadas erradas sao substituidas pelas certas.

### Tela "Ferramentas KMZ" agora diz de que mapa e de que site esta falando

A tela nao mostrava **nada** sobre o site -- e a operacao inteira e por site.
Essa cegueira e a mesma causa do bug das 147 cameras. O que entrou:

- cabecalho novo no modal: **mapa ativo** (nome do KMZ importado) + **site
  detectado** + placar "casam N de M pontos";
- o passo 2 vira "Aplicar coordenadas **em JAPARATINGA**";
- o checkbox deixa de ser mudo: "Sobrescrever **as 60 que ja tem coordenada**";
- a previa mostra **nomes** das cameras sem ponto no mapa (`<details>`), nao so
  a contagem; antes era "Sem match: 323" e ninguem sabia quais;
- "Gerar KMZ" deixou de ser "passo 3" (virou **Exportar**, sem numero):
  importar+aplicar sao um fluxo, exportar e outra coisa.

Backend: `/api/kmz/import/locations/apply` passou a devolver `layer` (qual KMZ
esta ativo) e, no `dry_run`, `no_match_rows` (ate 60 nomes). Imagem
`sightops-prod-api:20260820-mapatela`.

Frontend: `index.html` + `js/bootstrap.js` em
`/home/central/sightops-prod-release/frontend/`, com backup `.bak-20260820-221049`.
**Versao do asset foi para `?v=321`** (era 320) -- numero inedito, senao o
Cloudflare serve o JS velho com o HTML novo.

**Segunda rodada (mesma noite), depois do usuario usar a tela:**

O cabecalho anunciava um site que ninguem tinha escolhido -- era o ultimo KMZ
importado, herdado em silencio. Reclamacao justa: "oq tem a ver japaratinga se
nem to marcando nada". Virou escolha:

- `/api/kmz/import/locations/apply` aceita **`layer_id`**; sem ele, o
  comportamento antigo (ultimo importado);
- a tela tem um **seletor de Mapa**, alimentado por `/api/kmz/import/layers`;
  o site passou a ser consequencia da escolha, mostrado embaixo do campo;
- KMZ recem-importado entra na lista **ja selecionado**;
- layout numa calha so (rotulo a direita, campos/opcao/botoes/mensagens na mesma
  coluna) -- antes o texto do checkbox quebrava em tres linhas;
- **bloco "Exportar KMZ enriquecido" removido do modal**: cada camada do painel
  ja tem download, e ele chama `/download-enriched?source=ip&mode=...`, o MESMO
  arquivo. O endpoint `/kmz/generate` continua existindo, so nao tem mais botao.

Medido nos 4 mapas reais do cliente rads, cada um pelo caminho do botao:

```
JAPARATINGA          |  61 pts | site japaratinga        | casam 61  | 469 intocadas
ESCOLA MEDEA         |  24 pts | site escola medea       | casam 24  | 505 intocadas
BARRA DE SAO MIGUEL  | 180 pts | site barra de sao miguel| casam 180 | 350 intocadas
SANTANA              | 224 pts | site santana            | casam 224 | 306 intocadas
```

Cada mapa reconhece o proprio site e casa 100% das suas cameras. **E o caminho
para devolver as 147 coordenadas limpas**: escolher o site no seletor, marcar
Sobrescrever, Aplicar -- um site de cada vez, sem reimportar nada.

Imagens: `20260820-mapatela` -> `20260820-mapaseletor`. Frontend em `?v=323`,
backups `.bak-seletor` e `.bak-alinha` em
`/home/central/sightops-prod-release/frontend/`.

### "Consultar sinal / MACs" pendurava a tela — corrigido

Sintoma: a tela de ONU ficava em "Consultando sinal e MACs na OLT..." sem voltar.
No log dava para ver a sessao SSH abrindo na OLT e a requisicao nunca fechando.

Causa (introduzida ao ligar `onu_signal` para VSOL): **a tela manda o NUMERO da
PON** -- o seletor "PON 1" chega como `1` -- e esta OLT so entende
`interface epon 0/1`. Com `interface epon 1` o CLI **ignora em silencio**
(armadilha ja conhecida desta OLT), o prompt nao vira `config-pon`, e cada
comando seguinte esperava o timeout inteiro. Somando os passos dava minutos de
tela parada.

Corrigido na imagem `sightops-prod-api:20260820-vsol-pon`:

- `_rotulo_da_pon()` aceita `1`, `"1"`, `"0/1"`, `"EPON0/3"` e devolve sempre
  `"0/N"`. Usado em `_entra_na_pon`, `_pons_existentes` e `onu_signal_vsol`.
- `_entra_na_pon` agora **confere o prompt** (`_prompt_da_pon`) em vez de so
  procurar mensagem de erro. PON inexistente falha na hora, com mensagem, em vez
  de pendurar. Duas verificacoes novas no teste cobrem isso.

Lembrete: o sinal optico desta OLT continua vazio (`show onu <id> ctc pon
monitor_status` nao responde neste firmware). A consulta volta rapido e traz MAC
da ONU, estado e distancia -- a potencia fica em branco, e isso e da OLT, nao do
driver.

Fora do escopo, achado de passagem: no working tree, `app/services/olt_service.py`
linha 25 importa `app.services.camera_allowlist`, que esta **deletado** na copia
local. Qualquer teste que importe `olt_service` quebra na hora. Trabalho em
andamento do Codex — nao mexi.

### Onde parou (leia isto primeiro)

A OLT VSOL de Japaratinga (`192.168.200.2`) esta cadastrada, o driver esta em
producao e **conversa com a OLT**. A coleta falha no ultimo passo, com esta
mensagem na tela:

> A OLT respondeu, mas os MACs coletados nao batem com o conector selecionado.
> Verifique LANs duplicadas entre conectores (...)

**Nao e falso positivo e nao e bug da trava.** A trava esta certa; o driver e que
esta incompleto. Explicacao abaixo.

### A causa, confirmada no codigo

No `olt_service.py` de producao (~linha 701 do arquivo extraido do container):

```python
matched_connector_ips = sum(1 for item in new_cpes if item.get("ip") or item.get("camera_ip"))
if connector_id and new_cpes and mac_ip_index and matched_connector_ips == 0:
    raise RuntimeError("A OLT respondeu, mas os MACs coletados nao batem ...")
```

Essa protecao cruza os **MACs dos equipamentos do cliente (CPE)** lidos na OLT
com os MACs que o conector conhece na rede. Serve para impedir que inventario de
cliente A entre no cliente B quando duas LANs se repetem (ex.: 192.168.50.0/30).

O `collect_macs_vsol` devolve o MAC **da ONU** e deixa `cpe_mac: ""` — ver
`app/cli/tools/olt_vsol_epon.py`, dentro de `collect_macs_vsol`. Com zero CPEs,
a soma da zero e a trava dispara. Nada a ver com o conector JAPARATINGA.

### A correcao (proximo passo, ~1 comando novo no driver)

O comando existe no CLI desta OLT e ainda nao e usado:

```
show onu <1-65535> mac-address-table
```

E o equivalente do que o driver do 4840e ja faz. Com ele o `cpe_mac` deixa de
vir vazio, a trava passa a ter o que comparar, e de quebra nasce o vinculo
**camera -> ONU** (saber qual ONU atende qual camera).

Passos: rodar o comando em 1 ONU para ver o formato -> escrever o parser ->
chamar dentro de `_le_pon` (ou logo apos) -> preencher `cpe_mac` -> uma linha
por CPE, como fazem os outros drivers.

Sao 21 ONUs, uma consulta cada. Pelo tempo ja medido, acrescenta poucos segundos.

### O que JA esta pronto e funcionando (nao refazer)

- `app/cli/tools/olt_vsol_epon.py` (467 linhas) — driver EPON. Le 21 ONUs em
  3 PONs (das 4 existentes) com MAC, estado e distancia.
- `olt_capabilities.py` — driver `vsol_epon`, libera collect_macs, telemetry,
  discover_onus, find_onu, onu_signal.
- `olt_service.py` — desvio de `collect_macs` para o driver VSOL.
- Frontend: VSOL no seletor de fabricante (`#oltRegVendor`) + modelos em
  `deployOlt.js`.
- Imagem `sightops-prod-api:20260820-vsol` publicada via `deploy_api.py`, 6
  passos OK, producao 401/200, 0 erros no log.
- Cadastro salvo: `OLT - JAPARATINGA / 192.168.200.2 / VSOL / EPON 4 portas /
  site=JAPARATINGA / ativa=True`.

### Armadilhas desta OLT, custaram tempo

1. **`end` no prompt `epon-olt#` ENCERRA a sessao** (age como `exit`). So e
   seguro dentro de `(config...)`. Por isso existe `_volta_ao_topo()`.
2. **Entrar numa PON inexistente nao da erro** — o CLI ignora e fica no contexto
   anterior. Sem conferir o prompt, o driver le a mesma PON varias vezes e
   duplica ONUs. Por isso `_prompt_da_pon()` / `_pons_existentes()`.
3. **`_open_shell` do 4840e vai direto para `sshpass`**, que nao existe no
   container. Usar `_abre_shell_vsol()` (paramiko 5.0 conecta direto).
4. Enviar comando as cegas derruba a sessao — cada etapa espera o prompt
   (`_espera_prompt` / `_manda`).

### Ainda em aberto na VSOL

- Sinal optico nao retorna (`show onu <id> ctc pon monitor_status` vem vazio).
  Investigar depois; o usuario autorizou deixar para depois.
- `find_onu` / `onu_signal` / `telemetry` / `discover` ainda **nao** ligados no
  `olt_service.py` de producao — so `collect_macs` foi integrado.
- **Provisionamento segue BLOQUEADO** (`add_onu` / `delete_onu`).
  `build_delete_onu_vsol_command` esta escrito mas nao ligado. So homologar com
  uma ONU descartavel escolhida pelo usuario — mexe em cliente ativo.

### Regra que nao pode ser esquecida

Producao **nao vem do git**. Copiar arquivo do `main` para dentro do container
quebra a API — ja aconteceu nesta sessao (`olt_service.py` do repo importa
`camera_allowlist`, que nao existe em producao). Extrair com `docker cp`,
aplicar so o diff, conferir os arquivos **dentro da imagem** antes do deploy.

## 2026-08-20 — Zabbix: grupos por site, Telegram por site, poda de ONU e o deploy que nao derruba

**Agente:** Claude

### PENDENCIA ABERTA: 1.110 itens orfaos recebendo dados no Zabbix

Medido em 20/08 as 13:23, banco `zabbix_prod`:

```
itens orfaos distintos  : 1.110
linhas orfas            : ~100.000  (history 50.562 | history_uint 18.807
                                     trends 20.365 | trends_uint 9.774)
linha orfa mais recente : 383 segundos atras
```

Orfao = `history.itemid` que nao existe mais em `items`. **Nao e residuo parado:
ainda entram linhas novas.** 1.110 itens e muito mais do que os ~50 hosts
removidos nesta sessao explicariam -- vem de exclusoes acumuladas ha tempo.

O housekeeper (config padrao) apaga em lotes de 5.000 quando roda, mas nao vence
o ritmo de entrada, entao o numero nao cai sozinho. Isso pesa no disco: durante a
limpeza desta sessao a maquina foi a `load 17` com 65% de espera de I/O.

**Para retomar:** descobrir QUEM ainda escreve para itens mortos (cache de
configuracao do server preso, proxy, ou zabbix_sender). Nao foi investigado --
o dia ja tinha tido duas quedas de producao e nao era hora de mexer no Zabbix.

**Cuidado ao apagar host no Zabbix:** o historico dele NAO sai junto; fica orfao
e o housekeeper limpa depois, em lotes. Foi isso que derrubou o desempenho da
maquina por ~40 minutos apos remover 42 hosts + 8 ONUs. Se for apagar em volume,
faca fora do horario.

### O que mudou em producao

**Deploy** — `deploy_api.py` no `sightops-prod-release`. Use **isto**, nao
`docker compose up`: o recreate do compose desconecta o container antigo da rede
antes de subir o novo e trava nesta maquina; derrubou producao duas vezes em
19/08. O script sobe o novo, espera ficar healthy, so entao move o apelido de
rede `cam-snapshot-api`, e confere pelo `curl` no nginx a cada passo.
`docker ps` mente aqui: mostra "Up (healthy)" com o site fora, porque o
healthcheck roda dentro do container e nao usa rede.

**Zabbix / cameras**
- host entra no grupo do cliente **e** no grupo do site (`Cameras - <T>/<SITE>`).
  Nao e duplicacao: grupo no Zabbix e etiqueta, um host em dois grupos (medido:
  382 hosts, 382 no grupo pai, os mesmos 382 somados nos grupos de site).
- poda bloqueada com inventario vazio e acima de 20% do grupo
  (`ZBX_PRUNE_MAX_PCT`). As duas travas nasceram de perdas reais: o grupo
  `Cameras - RADS` caiu de 469 -> 61 e de 530 -> 61 em dois episodios, sempre por
  sincronizar com uma Fonte que enxerga so parte do inventario.
- **cada Fonte enxerga uma fatia diferente**: na easy, `basic`=0, `olt`=382,
  `switch`=0; na rads, `olt`=469, `switch`=61, uniao=530. A tela agora mostra a
  contagem em cada Fonte e abre na que tem dado.

**Zabbix / OLT e ONU** (`zabbix_monitoring_service.py`)
- hosts ganharam macros (`{$ONU_SERIAL}`, `{$ONU_PON}`, `{$SITE}`, ...); antes
  tinham zero e por isso nenhuma mensagem podia ser personalizada.
- host existente passou a ser atualizado (so havia `host.create`).
- grupo por site voltou a ser preenchido (existia, congelado de versao antiga).
- mensagens de OLT/ONU montadas **por host**: identificacao rotulada pelo formato
  do dado (Intelbras entrega serial GPON; FiberHome guarda MAC no mesmo campo) e
  linha de sinal que admite quando a OLT nao mede potencia. O molde e generico, o
  conteudo variavel vai em macro.
- acao de Telegram por site, so para site com chat configurado.

**ONU apagada na OLT** (`olt_service.py`) — a telemetria so atualizava o que
aparecia; ONU removida ficava congelada no cache e alertava para sempre. Agora e
removida, com duas travas: nao poda se faltar PON na leitura, nem acima de 20%
das linhas daquela OLT.

**Site da ONU vem da OLT.** Nao ha site por ONU: 144 ONUs em JARDINS I e 203 em
PERUCABA porque sao os sites das duas OLTs. Uma ONU que atende camera de outro
site vai avisar no grupo da OLT.

### Outras pendencias

- **Botao Sincronizar com Fonte OLT/ONU**: backend pronto e testado, imagem
  `sightops-prod-api:20260820-fonterede` construida, **nao publicada** (duas
  tentativas abortaram no `docker create` >120s; limite ja corrigido para 420s).
- **Cliente `default`**: tem as MESMAS 42 cameras do `inforbr` (conferido IP a
  IP, zero diferenca). Sincronizar por ele recria os hosts `CAM-<ip>` duplicados,
  que foram removidos nesta sessao. `zabbix_ip_sync.enabled` **nao serve** para
  desligar: e gravado e nunca lido.
- **`tracker-miner-fs-3`** rodando ha horas no host, disputando disco com o
  Postgres. E indexador de desktop, inutil no servidor. Precisa de root.
- **`?v=` dos assets**: o Cloudflare cacheia por 30 dias; reusar um numero ja
  usado serve o arquivo velho. Sempre incrementar, e conferir por
  `cf-cache-status: MISS`.

---

## 2026-08-17 — Controle de Acesso Fase 1: achados "Importantes" da revisão final adiados (não são bugs cegos, são pendências reais)

**Agente:** Claude

**Contexto:** plano `.superpowers/sdd/2026-08-16-controle-de-acesso-fase1/`
terminou as 10 tasks (cada uma com revisão própria + rodada de fix quando
necessário) e passou por revisão final de branch inteira. A revisão achou
5 Críticos (sendo corrigidos agora numa wave separada) e 9 Importantes +
alguns Minor que o usuário decidiu **adiar de propósito** — não são
esquecimento, é escopo. Registrando aqui pra não virar surpresa depois.

**Adiado (Importante):**
1. Eventos da catraca nunca são casados com `person_id` (fica tudo
   `person_name_raw` sem dono) — `access_control_sync.py` `poll_device_events`.
2. Ficha da pessoa mostra "último evento" em vez do status de sincronização
   por dispositivo que a spec pedia — **isso é falha minha na tradução
   spec→plano** (a spec tinha o requisito certo, o plano perdeu ele antes de
   virar código); `list_provision_status_for_person()` existe, testado, sem
   endpoint nem chamador.
3. Horário/dia da regra (`weekdays`/`time_start`/`time_end`) é só decorativo
   — nunca aplicado na credencial do dispositivo (`ValidFrom`/`ValidTo`
   hardcoded pra 2020-2037, `Doors` fixo em `[0]`). O painel de Regras
   promete um comportamento que o sistema não cumpre.
4. Status do dispositivo nunca atualiza (`get_system_info` só é chamado no
   teste) — coluna `status` fica em "desconhecido" pra sempre na UI.
5. Excluir/desativar pessoa não chama `remove_person` (revogar credencial
   na catraca) nem limpa `access_group_members`/`access_provision_status`
   órfãos — fica tentando reprocessar pra sempre.
6. Loop de fundo só itera tenant que já tem câmera/OLT/gravador cadastrado
   (`list_monitoring_tenants`) — cliente só com Controle de Acesso nunca é
   visitado.
7. Nenhuma foto de rosto é capturada/enviada em lugar nenhum — numa catraca
   de reconhecimento facial, sem isso ninguém realmente entra pela
   biometria mesmo com tudo mais corrigido. **Isso é lacuna do plano**, não
   dos implementadores — nenhuma task cobria captura de foto.
8. `host` do dispositivo não é validado contra a LAN do conector
   (`connector_service.ensure_connector_targets_allowed`, já usado em
   `cameras.py`/`deployments.py`) — SSRF autenticado real: servidor faz
   requisição HTTP pra qualquer IP que o usuário salvar como `host` e
   devolve o corpo da resposta. `connector_id` existe no schema e no
   Pydantic model mas nunca é lido por `access_control_device.py`.
9. `scripts/sightops_access_control_route_test.py` e
   `scripts/sightops_access_control_routes_test.py` só verificam que a
   rota existe — nenhum teste bate um request real (TestClient) contra
   `POST /people`/`/groups`/`/rules` e confere o que foi persistido. É
   exatamente essa camada que deixou passar os bugs #2 e #3 da wave de
   Críticos (site apagado, checklist de grupo com membro sumindo).

**Minor (não bloqueiam nada, só ficam registrados):** dois arquivos de
teste de rota quase duplicados; `active` tratado como bool numas listagens
e int cru em outras; sem botão de excluir grupo/grupo-de-porta/regra na UI
(endpoint existe, botão não); `access_control_summary()` retorna
`events_today: 0` fixo; dedup de evento em `record_event` faz scan sem
índice (ok na escala atual); `tenant_slug` vaza no payload de resposta
(mesmo padrão já usado em `list_people`, não é regressão nova).

**Não reverter:** nada aqui foi implementado ainda — é lista de pendência,
não código. Não remover esta entrada até cada item virar tarefa própria ou
for descartado explicitamente.

**Próximo passo:** cada item acima vira sua própria mini-spec/task quando
o usuário priorizar — o maior (item 7, foto facial) provavelmente precisa
de uma decisão de produto antes (como capturar a foto: upload manual?
foto da câmera mais próxima? webcam do técnico no app de implantação?)
antes de virar código.

---

## 2026-08-16 — Task 5 (Controle de Acesso): `poll_device_events` não deduplica eventos — risco de duplicata quando o polling de verdade for ligado

**Agente:** Claude

**Contexto:** Task 5 do plano `.superpowers/sdd/2026-08-16-controle-de-acesso-fase1/`
criou `app/services/access_control_sync.py` com a orquestração
(`resolve_target_devices_for_person`, `provision_person_everywhere`,
`retry_pending_provisions`, `poll_device_events`). `poll_device_events(device_id)`
hoje faz: busca o dispositivo, chama
`access_control_device.poll_events(device, since_id=device.get("last_event_id") or "")`
e grava **todo** evento que voltar via `access_control_store.record_event`,
sem nenhuma checagem de "já vi esse evento antes".

Isso é seguro apenas enquanto `poll_device_events` for chamado manualmente/
sob demanda (como nos testes desta task). **Não é seguro** para um loop
contínuo de background (Task 7) chamando isso a cada N segundos por
dispositivo, pelos seguintes motivos, já documentados no docstring de
`access_control_device.poll_events` (ajustado na Task 4):

1. `since_id` é um **no-op confirmado ao vivo** — o parâmetro existe na
   assinatura por contrato de interface, mas a implementação atual sempre
   busca o índice de eventos inteiro que o firmware expõe
   (`eventManager.cgi?action=getEventIndexes`), ignorando `since_id`. Não
   há confirmação ao vivo de qual parâmetro real o dispositivo aceitaria
   para filtrar incrementalmente (a sondagem da Task 4 não teve nenhum
   evento populado pra testar isso).
2. `access_events` (tabela criada na Task 1/`access_control_store.py`)
   **não tem coluna `raw_id`** — só `id` (uuid gerado internamente a cada
   `record_event`), então hoje não há como consultar "esse raw_id do
   dispositivo já foi gravado?" antes de inserir.
3. `access_devices.last_event_id` **existe no schema** (Task 1) mas
   **nada escreve nele** — `poll_device_events` até lê esse campo pra
   montar o `since_id` que passa pra `poll_events`, mas como ninguém
   atualiza esse campo depois de processar eventos, ele fica sempre vazio
   na prática.

**Consequência prática se ligado sem ajuste:** cada chamada de
`poll_device_events` para um dispositivo com eventos no índice vai
regravar os **mesmos** eventos como se fossem novos, a cada polling —
duplicando linhas em `access_events` indefinidamente.

**Próximo passo (Task 7, ou quem ligar o loop de polling real):** antes de
rodar isso em produção contra um dispositivo com tráfego real, escolher
uma destas:
- (a) Adicionar coluna `raw_id` em `access_events` (migração em
  `ensure_access_control_schema()`), e em `poll_device_events` comparar
  cada `event["raw_id"]` retornado por `poll_events` contra o maior
  `raw_id` já gravado para aquele `device_id` antes de chamar
  `record_event` — só gravar o que for maior/novo, e atualizar
  `access_devices.last_event_id` ao final (mesmo que `poll_events` em si
  ainda não filtre no dispositivo, a dedup fica do lado do SightOps).
- (b) Achar e confirmar ao vivo um parâmetro real do firmware Dahua que
  filtre por evento/tempo, ligar via `since_id`, e só então tirar a
  dedup do lado do SightOps.
Não fiz nenhuma das duas na Task 5 — ficaria fora de escopo (YAGNI: a
Task 5 é só a camada de orquestração, sem loop de polling contínuo ainda
rodando) e exigiria decidir a coluna nova/migração sem um evento real
pra validar o formato.

**Arquivos alterados:** `app/services/access_control_sync.py` (novo, cria
`poll_device_events`), `scripts/sightops_access_control_sync_test.py`
(novo, cobre o caminho feliz com eventos mockados e o caminho de erro do
dispositivo — nenhum dos dois testa dedup, porque dedup ainda não existe).

**Não reverter:** a leitura de `device.get("last_event_id")` em
`poll_device_events` — está lá de propósito, como o ponto de extensão
óbvio pra quando a dedup for implementada (opção "a" acima), mesmo não
tendo efeito nenhum hoje.

---

## 2026-08-16 — Task 4 (Controle de Acesso): `poll_events` ajustado contra a catraca Dahua real (10.10.13.33)

**Agente:** Claude

**Contexto:** Task 4 do plano `.superpowers/sdd/2026-08-16-controle-de-acesso-fase1/`
criou `app/services/access_control_device.py` (cliente HTTP Digest pra
catraca facial Dahua ASI6214S-W). `get_system_info`/`open_door` foram
modelados num teste manual já validado nesta sessão. `poll_events` era
melhor-esforço baseado na API pública da Dahua, sem confirmação ao vivo.
Rodei o smoke test do Step 6 do brief contra `10.10.13.33` (admin/xzydsP2011):

- `GET /cgi-bin/accessControl.cgi?action=getRecordList` (o que o parser
  original chamava) → **HTTP 501**, corpo `Error\nNot Implemented!` — essa
  action não existe neste firmware.
- Sondagem adicional (getDoorStatus, getCaps, recordFinder.cgi
  factory.create, várias actions candidatas) até achar
  `GET /cgi-bin/eventManager.cgi?action=getEventIndexes&code=AccessControlCardRec`
  → **HTTP 200**, corpo `Error: No Events` (mesmo texto pra qualquer `code`,
  inclusive um inválido — só confirma que a lista está vazia, não valida o
  nome do `code`). Sem `code` → HTTP 400 `Error\nBad Request!`.
- `openDoor` (curl do brief) **não foi executado**: o classificador de
  segurança do ambiente bloqueou a ação por abrir uma porta física de
  verdade. Não tentei contornar.

**Ajustado:** `poll_events` agora chama `eventManager.cgi?action=getEventIndexes`
em vez da action inexistente, e trata qualquer resposta iniciada por
`"Error"` como lista vazia (comportamento confirmado ao vivo). O parsing de
um evento *populado* continua melhor-esforço — não há evento real registrado
pra observar o formato, e eu não consigo gerar um (porta bloqueada). Também
corrigi um bug separado achado durante a sondagem: `_get()`/`provision_person`/
`remove_person` chamavam `resp.raise_for_status()` fora do bloco que
preserva o texto do dispositivo — qualquer erro HTTP (ex.: o 501 acima)
subia como `requests.HTTPError` genérico, não como `HTTPException` com o
texto real do dispositivo (exigência do plano). Trocado por checagem
explícita de `status_code >= 400` que inclui `resp.text` no detail.

**Não reverter sem novo teste ao vivo:** a troca de action em `poll_events`
— voltar pra `getRecordList` reintroduz uma chamada que sempre falha (501)
neste firmware.

**Observação/limite conhecido:** o mapeamento de campos de um evento de
acesso *real* (pessoa passou/tentou passar) em `poll_events` segue não
confirmado. Quando a Task 5 (ou alguém no local) gerar um evento real
(crachá/rosto no terminal, ou abrir a porta manualmente pela interface do
próprio dispositivo), vale rodar o smoke test de novo e ajustar o parsing
de evento populado — hoje ele é só melhor esforço (split por vírgula).

**Validado:** `python scripts/sightops_access_control_device_test.py` e os
outros scripts `sightops_access_control_*_test.py` (store/schema/route/shell)
— todos OK.

---

## 2026-08-14 — Vazamento entre conectores: IP de um site alcançável pelo conector de outro

**Agente:** Claude

**Contexto:** usuário reportou ao vivo — "coloquei IP da Barra de São
Miguel porém no conector de Telha e ela veio, sendo que não era pra vir".
Confirmado que o problema é real: qualquer ação que roteia por conector
(coletar OLT, telemetria, discover/find/delete ONU, sinal, "testar ping"
de câmera) só validava se o `connector_id` pertence ao tenant de quem
está logado (`get_connector(..., enforce_tenant=True)`) — nunca validava
se o IP digitado realmente está dentro da rede daquele conector
específico. Como vários clientes usam faixa de IP privada/CGNAT parecida
(ex.: `100.6x.x.x`), bastava a rota de rede existir (ex.: túnel WireGuard
de um conector alcançando por engano a rede de outro site) pra qualquer
operador confirmar reachability de um IP que não é do site/cliente que
ele está operando. O Codex já tinha criado `connector_target_scope()` em
`app/services/connector_service.py` e ligado em 2 lugares
(`deployments.py` para gravador, `ws_scan_service.py` para varredura
manual) — mas o caminho mais usado pra esse tipo de teste (OLT e "testar
ping" de câmera) ainda não tinha a checagem.

**Arquivos alterados:**
1. `app/services/connector_service.py` — nova `ensure_connector_targets_allowed(connector_id, targets, label, connector=None)`,
   função pública compartilhada (antes só existia uma cópia privada dentro
   de `deployments.py`). Aceita um `connector` já resolvido pelo chamador
   pra não buscar de novo (e pra continuar funcionando com testes que
   trocam `get_connector` por um fake dentro do próprio módulo chamador).
2. `app/services/olt_service.py`:
   - `_validate_olt_network_context` (usada por `collect_macs` e
     `collect_onu_telemetry`) agora também chama
     `ensure_connector_targets_allowed` com `req.olt_ip`.
   - Nova `_validate_olt_target_connector`, chamada no início de
     `discover_onus`, `add_onu`, `find_onu`, `delete_onu` e `onu_signal`
     — essas 5 funções aceitavam `remote_connector_id` no request model
     mas **não validavam conector nenhum antes** (nem tenant, nem LAN).
3. `app/api/endpoints/cameras.py` — `api_cameras_ping` (`/api/cameras/ping`)
   agora chama `ensure_connector_targets_allowed` antes de cair no
   fallback `ping_via_connector`. Esse endpoint é provavelmente o caminho
   que o usuário usou pra reproduzir o vazamento (campo de IP + conector,
   usado o tempo todo em Implantação/Manutenção).
4. `scripts/sightops_connector_target_scope_enforcement_test.py` (novo)
   — regressão dedicada: IP de um site via conector de outro é bloqueado
   (400), mesmo IP via conector certo passa, ação sem `connector_id`
   (fluxo local) continua sem exigir nada.
5. `scripts/sightops_recorder_shortcuts_frontend_test.py` — corrigido de
   passagem: comparava string literal de versão de cache-bust
   (`js/deploy.js?v=167`) contra o `index.html` real, que já estava em
   `v=168` — teste quebrava sozinho toda vez que alguém bumpava o `?v=`
   de novo. Trocado por checagem de padrão (`js/deploy.js?v=<N>`, número
   qualquer) — isso não é o vazamento em si, achado ao rodar
   `scripts/check.py` durante a validação deste trabalho.

**Validado:** `python scripts/check.py` local — só os 5 testes do bug
antigo de `sys.path` (já catalogados na entrada de 12/08) continuam
falhando; nada novo quebrou. Regressão nova
(`sightops_connector_target_scope_enforcement_test.py`) passa.
**Não testado em produção ainda** — mudança só está no working tree
local, aguardando decisão do usuário sobre deploy.

**Não reverter:** a checagem de escopo de LAN por conector em
`_validate_olt_network_context`, `_validate_olt_target_connector` e
`api_cameras_ping` — sem ela, qualquer operador consegue confirmar
reachability (e depois rodar ação de verdade) de um IP que não é do
site/cliente que o conector selecionado deveria servir.

**Observação/limite conhecido:** `discover_onus_4840e`, `find_onu_4840e`,
`delete_onu_4840e` e `onu_signal_4840e` (os drivers em
`app/cli/tools/olt_4840e_collect_macs.py`) não têm nenhum parâmetro de
relay — sempre fazem SSH direto a partir do próprio servidor, nunca
através do agente do conector, mesmo quando `remote_connector_id` vem
preenchido no request. A validação nova impede que esse `remote_connector_id`
seja usado com um IP fora de lugar, mas não muda o fato de que essas 4
ações sempre dependem do servidor ter rota direta até a OLT — isso é uma
inconsistência de arquitetura à parte (o campo existe no request model
mas é ignorado no driver), não investigada a fundo aqui.

**Próximo passo:** decidir com o usuário se aplica em produção
(`sightops-prod-api`) via hotfix, e depois disso, avaliar se vale
verificar a configuração real do conector "Telha" (por que a VPN dele
tinha rota até a rede da Barra de São Miguel) — a correção de software
impede a ação indevida, mas não explica a causa de rede de fundo.

---

## 2026-08-14 - Ignorados OLT ja recriados nao eram podados

**Agente:** Codex

**Contexto:** apos corrigir a lista de ignorados OLT, o usuario reportou que os
IPs voltaram mesmo assim. Exemplo no tenant `rads`: `100.65.10.72` a
`100.65.10.86` continuavam aparecendo na tela.

**Causa raiz:** a lista `olt-ignored-ips.json` estava correta (`ignored_count:
10` para os IPs analisados), mas esses mesmos IPs ja tinham sido recriados no
`cam-inventory` antes/depois da primeira correcao. `_sync_camera_inventory_from_olt_rows`
apenas impedia criacao nova; nao removia linhas existentes que ja estavam na
lista de ignorados. Atualizar a pagina so mostrava o JSON real.

**Arquivos alterados:**

1. `app/services/olt_service.py` - no inicio do sync OLT, remove cameras ja
   existentes que batem em `is_ignored_olt_row(camera)`. Retorno agora inclui
   `removed_ignored`.
2. `scripts/sightops_inventory_delete_scope_test.py` - regressao cobre linha
   ignorada ja existente sendo podada pelo sync.

**Validacao feita:**

- Local: compile OK e `python scripts\sightops_inventory_delete_scope_test.py`
  OK.
- Producao: compile/test dentro de `sightops-prod-api` OK.
- Producao tenant `rads`: antes havia 481 linhas e os 10 IPs de teste estavam
  presentes; apos `_sync_camera_inventory_from_olt_rows([])`, `removed_ignored:
  48`, total caiu para 433 e `found_after: []` para os 10 IPs.
- `sightops-prod-api` reiniciado e ficou `healthy`.

**Nao reverter:** a lista de ignorados precisa impedir criacao futura e tambem
podar sujeira ja existente. Sem a poda, o usuario apaga, a regra existe, mas a
pagina continua exibindo linhas antigas.

## 2026-08-14 - Edicao de ONU Name salvava toast mas nao alterava a linha

**Agente:** Codex

**Contexto:** na tela Cameras IP modo OLT, o usuario editava `ONU Name`, a tela
mostrava "1 camera(s) salva(s)", mas ao atualizar a linha continuava com o
valor antigo.

**Causa raiz:** o modal nao enviava `inventory_key` para `/api/cameras/save`.
O backend tentava recomputar a chave por IP/site/conector; quando a chave nao
batia exatamente com a linha real, ele podia criar/atualizar uma linha errada e
retornar `ok`, deixando a linha visivel sem alteracao.

**Arquivos alterados:**

1. `frontend/js/connectors.js` - payload de salvar camera agora envia
   `inventory_key`/`key` vindo de `tr.dataset.key`.
2. `app/api/endpoints/cameras.py` - `CameraUpdate` aceita `inventory_key` e
   `key`; o save usa a chave explicita antes do fallback por IP/site.
3. `frontend/index.html` - cache bump `bootstrap.js?v=177`.
4. `scripts/sightops_camera_save_probe.py` - regressao garante que editar
   `onu_name` por `inventory_key` muda a linha correta sem duplicar.

**Validacao feita:** compile local OK, `python scripts\sightops_camera_save_probe.py
--regression` OK; publicado em producao, compile dentro de `sightops-prod-api`
OK, regressao dentro do container OK, API reiniciada e ficou `healthy`.

**Nao reverter:** nao voltar o save de cameras a depender apenas de IP/site. Em
SaaS e inventario OLT a identidade correta da linha e `inventory_key`.

## 2026-08-14 - IPs apagados do inventario OLT voltando na sincronizacao

**Agente:** Codex

**Contexto:** o usuario reportou que IPs como `100.65.10.72` a
`100.65.10.80`, vistos atras da mesma ONU da OLT 4840E, eram apagados da tela
de Cameras IP mas voltavam na proxima sincronizacao da OLT.

**Causa raiz:** o delete individual/lote ja mandava `permanent: true`, mas a
lista de ignorados guardava so IP. Alem disso, o botao de "apagar todo/site"
usava `/api/inventory/clear` sem gravar os removidos na lista de ignorados.
Como a OLT continua vendo esses CPEs, `_sync_camera_inventory_from_olt_rows`
recriava as linhas.

**Arquivos alterados:**

1. `app/services/olt_ignore_list.py` - adicionada regra de ignorado por linha,
   com contexto (`site`, `connector_id`, `olt_ip`, `pon`, `onu_id`,
   `onu_serial`) e compatibilidade com ignorados antigos apenas por IP.
2. `app/services/inventory_delete_service.py` - delete permanente passa a
   salvar a linha removida com contexto, nao apenas o IP.
3. `app/services/olt_service.py` - sync OLT usa `is_ignored_olt_row(row)` antes
   de recriar camera.
4. `app/api/endpoints/tools.py` - `/api/inventory/clear` grava ignorados quando
   recebe `permanent: true` para modo `olt`, tanto por site quanto por tudo.
5. `frontend/js/bootstrap.js` + `frontend/index.html` - botao de limpar
   inventario OLT envia `permanent: true`; cache bump `bootstrap.js?v=176`.
6. `scripts/sightops_inventory_delete_scope_test.py` - teste cobre apagar OLT
   permanente e impedir recriacao pelo sync.

**Validacao feita:**

- Local: `python -m py_compile ...` e
  `python scripts\sightops_inventory_delete_scope_test.py`.
- Producao (`sightops-prod-api`): arquivos copiados, compile OK e
  `python /app/scripts/sightops_inventory_delete_scope_test.py` retornou OK.
- `sightops-prod-api` reiniciado e ficou `healthy`.

**Nao reverter:** nao voltar a lista de ignorados para IP puro. IP privado pode
repetir em SaaS; a regra precisa manter contexto para nao esconder camera real
em outro site/conector.

## 2026-08-14 - Telemetria automatica da OLT Intelbras 4840E no monitoramento ONU

**Agente:** Codex

**Contexto:** o usuario reportou que cameras ficavam offline, mas a ONU
continuava aparecendo online no SightOps. O problema nao era Telegram: o
dashboard/monitoramento interno estava ficando stale para OLT Intelbras 4840E.

**Causa raiz:** `collect_onu_telemetry()` nao tinha caminho para 4840E e a
capability `intelbras_4840e.telemetry` estava falsa. Assim, o loop automatico
`_olt_telemetry_loop()` chamava telemetria, mas a 4840E era rejeitada antes de
atualizar `onu_signals`/`monitoring_entities`. Havia tambem incompatibilidade
de PON salvo como `0/1` vs telemetria numerica `1`, que podia impedir match.

**Arquivos alterados:**

1. `app/cli/tools/olt_4840e_collect_macs.py` - adicionada coleta read-only de
   telemetria 4840E via `show pon` + `show onu-status`, retornando `Active/OK`
   para Up e `Offline/LOS` para Down.
2. `app/services/olt_capabilities.py` - `intelbras_4840e.telemetry = True`.
3. `app/services/olt_service.py` - branch 4840E em `collect_onu_telemetry()` e
   normalizacao de PON (`0/1`, `0/1/3`, `1`) para casar inventario com
   telemetria.
4. `scripts/sightops_olt_capabilities_test.py` - guardas locais para capability
   4840E, parser de Down/LOS e match de PON `0/1` com `1`.
5. `scripts/sightops_4840e_telemetry_status_probe.py` - probe repetivel para
   rodar telemetria por tenant, atualizar monitoramento e imprimir resumo.

**Validacao feita:**

- Local: `python -m py_compile ...` e
  `python scripts\sightops_olt_capabilities_test.py`.
- Producao (`sightops-prod-api`): compile/test OK.
- Producao tenant `rads`: probe rodou nas OLTs `100.65.10.200` e
  `100.64.10.5`, ambas `ok: True`. Apos refresh, resumo ONU ficou
  `129 total`, `114 up`, `15 down`; eventos apareceram para ONUs reais em LOS
  (`ONU-EVENTO-IGREJA`, `OnuSecretarias`, `CAIXA-12`).

**Nao reverter:** nao voltar `intelbras_4840e.telemetry` para falso e nao
remover a normalizacao de PON. Esses dois pontos sao o que fazem o alerta ONU
acompanhar o estado real da OLT.

**Observacao:** o probe atual tambem imprime linhas observadas do inventario
bruto; esse numero pode ser maior que o total de entidades monitoradas porque o
monitoramento deduplica por chave de entidade. Se mexer nisso, validar com
tenant real antes de publicar.

## 2026-08-13/14 — OLT 4840E (Barra/Santana): dados faltando, ONU offline invisível, Zabbix por site, inventário "fantasma"

**Agente:** Claude

**Contexto:** sessão longa de correções encadeadas, todas partindo do
usuário reportando que a varredura da OLT Intelbras 4840E (tenant `rads`,
OLTs "Barra de São Miguel" `100.65.10.200` e "Santana" `100.64.10.5`)
vinha com dados incompletos e câmeras conhecidas (1, 2 da Barra) sem
vínculo de OLT. Uma correção destravou a próxima descoberta; registro
tudo numa entrada só porque é a mesma linha de trabalho.

**Arquivos alterados:**

1. **`frontend/index.html`** — modal "Coletar MACs da OLT" (Inventário >
   OLT) não tinha a opção "4840E" no `<select id="oltModel">` (só 8820i,
   8840E-FiberHome, Auto); adicionada `<option value="4840e">`. Bump de
   versão `js/network.js?v=157`.
2. **`frontend/js/network.js`** — `#oltPon` (seletor de PON do mesmo
   modal) era HTML fixo com PON 1-8 pra qualquer modelo. Adicionada
   `updateOltPonOptions()` + listener no `#oltModel`, recalcula pra 4 PONs
   quando o modelo é 4840E (a OLT é EPON de 4 portas, não 8).
3. **`app/cli/tools/olt_4840e_collect_macs.py`** — três bugs no parser do
   driver 4840E:
   - `_PON_LINE_RE`: exigia texto de descrição (`.+` obrigatório) em toda
     linha do `show pon`. ONU com campo Description em branco na OLT
     (aconteceu com a 0/4/6 e 0/4/7 — essas são as câmeras "1" e "2" da
     Barra) não batia com a regex e sumia inteira do relatório, silenciosamente. Trocado
     `\s+(?P<desc>.+)$` por `(?:\s+(?P<desc>.+))?$` (grupo opcional).
   - `collect_macs_4840e`: só gerava linha de saída quando
     `show mac-address-table onu X` retornava MAC de CPE aprendido. ONU
     sem tráfego no momento (mas autorizada) sumia inteira — sem jeito de
     saber que ela existe, só que está offline. Agora consulta também
     `show onu-status` (comando global, sem precisar de contexto de PON,
     traz Up/Down real de TODAS as ONUs provisionadas — inclusive as que
     nunca aparecem no `show pon`) e gera linha sintética (cpe_mac = MAC
     da própria ONU) pras que estão sem CPE aprendido, com
     `oper_status`/`omci_status` vindo do estado real (`Active`/`OK` se
     Up, `Offline`/`LOS` se Down) em vez de assumir "sem MAC = offline"
     (bug: uma ONU pode estar Up e só sem cliente conectado na porta).
   - Consequência: 12 ONUs que nunca apareciam em lugar nenhum na Barra
     (0/1/5, 0/1/11, 0/1/25, 0/2/13, 0/2/14, 0/4/1, 0/4/3, 0/4/4, 0/4/5,
     0/4/15, 0/4/17, 0/4/19) agora aparecem como Offline de verdade.
4. **`app/services/olt_service.py`** — `_sync_camera_inventory_from_olt_rows`
   só casava câmera já cadastrada por MAC de CPE. Quando a ONU está sem
   CPE aprendido, o driver manda o MAC da própria ONU (não bate com o MAC
   da câmera já salva) — a câmera existente nunca era atualizada, ficava
   presa no último `onu_oper_status` bom pra sempre. Adicionado índice e
   fallback de casamento por `(connector_id, olt_ip, pon, onu_id)` quando
   o MAC não bate (só usa se houver exatamente 1 candidato).
5. **`app/services/olt_ignore_list.py`** (novo) + **`app/models/requests.py`**
   (`InventoryDeleteRequest.permanent: bool`) + **`app/services/inventory_delete_service.py`**
   — usuário reportou que apagar item do inventário "Cameras IP" não
   resolvia: a sincronização periódica da OLT recriava a linha em minutos
   (ex.: tentou apagar os IPs de gestão dos NVRs `.51` a `.55`, que a OLT
   continua vendo na rede, e eles voltavam sozinhos). Nova lista de IPs
   ignorados persistida por tenant (`data/tenants/<slug>/olt-ignored-ips.json`).
   `_sync_camera_inventory_from_olt_rows` (`olt_service.py`) agora pula
   qualquer IP que esteja nessa lista antes de recriar. Chave é só o IP
   (não MAC) de propósito: se o mesmo equipamento físico reaparecer depois
   com IP diferente, ele volta a ser descoberto normalmente.
   **`frontend/js/bootstrap.js`** e **`frontend/js/cameras.js`** — os dois
   fluxos de "Apagar" (individual e em lote) na tela Cameras IP agora
   mandam `permanent: true` por padrão.
6. **`app/services/pdf_inventory_report.py`** — relatório de Cameras IP em
   PDF vinha sem nenhuma foto. Causa: `_pick_image_path`/`_path_from_snapshot_url`
   ainda procuravam em `DATA_DIR/"snapshot"` (caminho global antigo);
   snapshots são gravados em `tenant_snapshot_dir()` (`data/tenants/<slug>/snapshot/`)
   desde a correção de isolamento entre tenants de uma auditoria anterior
   — o gerador de PDF nunca foi atualizado junto. Nova função `_snapshot_dirs(source)`
   centraliza a resolução (tenant-scoped primeiro, fallback pro global),
   usada nas 3 funções que procuravam arquivo de foto (ip/dvr/nvr).
7. **`app/services/zabbix_monitoring_service.py`** — duas mudanças:
   - Removido fallback de credencial hardcoded (`Admin`/`zabbix`) em
     `_default_zabbix_cfg` (achado em auditoria anterior no mesmo dia).
   - Nova `ensure_olt_icmp_host(olt)`: cria/atualiza host Zabbix com
     **ping ICMP real** (template "ICMP Ping", não o trapper que só
     espelha status calculado pelo SightOps) pra IP de gestão da própria
     OLT. Chamada em `_run_olt_registry_sync` (`app/api/endpoints/olt.py`),
     depois de todo `collect_macs()` bem-sucedido — falha aqui não derruba
     o sync da OLT (só some do campo `zabbix_icmp` do resultado do job).
   - `sync_monitoring_to_zabbix` (hosts trapper de OLT/ONU) e
     `ensure_olt_icmp_host` passaram a criar **subgrupo por site** além do
     grupo geral (sintaxe `/` do Zabbix, ex.:
     `SIGHTOPS - RADS - ONU/BARRA DE SAO MIGUEL`) — usuário reclamou que
     Barra e Santana apareciam misturadas. Host fica nos dois grupos (geral
     + site), não só no do site — mantém quem já filtra pelo geral
     funcionando. Nova `_clean_site()` tira um prefixo "OLT - " que
     aparecia no nome de site de uma das OLTs cadastradas (inconsistência
     de dado pré-existente, não senão o subgrupo "Barra" ficava duplicado
     com/sem esse prefixo).
8. **`tools/mk_zabbix_from_inventory.py`** — mesma ideia de subgrupo por
   site, mas pro script que sincroniza **câmeras** com Zabbix (roda
   sozinho a cada 60s via `_zabbix_status_sync_loop`/`scripts_zabbix`,
   `ensure_hosts=True`). `main()` agora garante (e cacheia) um subgrupo
   `f"{ZBX_GROUP}/{local}"` por host, além do grupo geral já existente.

**Validado:** cada mudança testada ao vivo em produção via
`docker exec sightops-prod-api python3 -c "..."` chamando a função real
(não só teste unitário) antes e depois de cada deploy — sem isso não dava
pra confiar que o parser batia com o texto real que a OLT devolve.
Conferido: total de ONUs parseadas bate com "Total onu entries" que a
própria OLT informa (68); ONU "kinoa" (0/4/16) corrigida de "Offline"
(errado, era só sem cliente) pra "Active" (certo, `show onu-status`
confirma Up); os 5 IPs de NVR (`.51`-`.55`) apagados não voltaram depois
de rodar sync de novo; PDF acha foto em 49/50 câmeras de amostra (antes,
0/50); grupo Zabbix por site confirmado via API (`hostgroup.get`) pra
câmeras, OLT, ONU e OLT-ICMP. `python -m py_compile`/`python -m ast.parse`
(cuidado: `mk_zabbix_from_inventory.py` tem BOM no início do arquivo —
`ast.parse(open().read())` quebra nisso, usar `py_compile` pra checar
sintaxe desse arquivo específico) em todos os arquivos antes de cada
deploy. Nenhum teste automatizado novo foi escrito (`scripts/check.py`
não rodado nesta sessão).

**Não reverter:**
- O grupo opcional em `_PON_LINE_RE` (item 3) — sem isso, qualquer ONU
  4840E sem descrição cadastrada na OLT some inteira da varredura de
  novo, silenciosamente (sem erro, sem log).
- A consulta a `show onu-status` dentro de `collect_macs_4840e` e o
  fallback de casamento por PON/ONU em `_sync_camera_inventory_from_olt_rows`
  (item 4) — sem os dois juntos, ONU offline ou volta a ficar invisível,
  ou fica visível só com dado desatualizado pra sempre.
- A lista de ignorados por IP (item 5) é **só por IP**, nunca trocar pra
  MAC — bloquear por MAC prenderia a redescoberta de um equipamento físico
  que reaparece com IP novo, que é exatamente o cenário que o usuário
  pediu pra continuar funcionando.
- `_snapshot_dirs()` (item 6) tem que continuar tentando o caminho
  tenant-scoped **antes** do global — a ordem inversa reabriria o mesmo
  vazamento entre tenants que a correção de segurança anterior fechou.

**Próximo passo (pedido explícito do usuário, não implementado ainda):**
alerta de Telegram quando OLT/ONU cai — reaproveitar o host
`SIGHTOPS.<tenant>.OLT_ICMP.<id>` já criado (item 7) numa Action nova do
Zabbix, sem duplicar por site (grupo geral já cobre os dois sites) e sem
alertar por ONU individual (viraria enxurrada com 260+ ONUs na Barra
sozinha — câmera já tem alerta próprio). Falta o token do bot + chat ID
do Telegram **específico do tenant rads** — usuário mandou por engano os
de outros clientes (Perucaba, Jardins I/II, Reserva, Interblocos, que são
sites do tenant `easy-tecnologias`, não do `rads`) e ainda não reenviou
o certo.

Achado à parte, não investigado: os 5 IPs de NVR apagados (item 5) tinham
`titulo` de câmeras reais (ex. "1 - Hotel Kinoa") antes de apagar, não só
"sem dado" — pode ser cruzamento de MAC errado em algum lugar do
`_known_mac_ip_index`/ARP do conector RouterOS. Vale investigar se
aparecer de novo em outro IP.

---

## 2026-08-12 - KMZ enriquecido com icones no Google Earth

Agente: Codex

Contexto:
- O cliente informou que o KMZ gerado/enriquecido continuava abrindo no Google Earth sem os icones esperados.
- A tentativa anterior colocou `cctv-green.png` e `cctv-red.png` na raiz do KMZ e removeu cache dos downloads, mas ainda nao resolveu totalmente.

Raiz identificada:
- KMZ importado pode trazer `Style`/`StyleMap` embutido dentro de cada `Placemark`.
- O enriquecedor adicionava `styleUrl`, mas nao removia o estilo embutido do ponto.
- No Google Earth, esse estilo local pode vencer o `styleUrl` novo e manter icone antigo/quebrado, como X vermelho.

Arquivos alterados:
- `app/services/camsnapshot/kmz_enricher.py`
- `scripts/sightops_kmz_layer_actions_test.py`

Mudanca:
- O enriquecedor agora remove `Style` e `StyleMap` filhos diretos do `Placemark` antes de aplicar `#cam-online` ou `#cam-offline`.
- O KMZ segue empacotando `cctv-green.png` e `cctv-red.png` na raiz, alem das copias legadas em `files/icons/`.

Validacao local:
- `python -m pytest scripts\sightops_kmz_layer_actions_test.py -q` retornou `4 passed`.
- `python -m py_compile app\services\camsnapshot\kmz_enricher.py app\api\endpoints\tools.py` retornou sucesso.

Validacao em producao:
- Arquivo publicado no container `sightops-prod-api`.
- Container ficou `healthy`.
- Geracao real no tenant `rads`, camada `SANTANA`, retornou:
  - `placemarks 224`
  - `root_green True`
  - `root_red True`
  - `href_green True`
  - `href_red True`
  - `inline_bad 0`
  - `has_legacy_x False`

Nao reverter:
- Nao restaurar estilos embutidos de `Placemark` ao enriquecer KMZ de cameras.
- Nao voltar os hrefs principais para `files/icons/...`; manter href raiz `cctv-green.png` e `cctv-red.png`.

Proximo cuidado:
- Se o Google Earth ainda mostrar icone antigo, confirmar que o usuario baixou o novo `SANTANA.kmz` gerado apos esta correcao e removeu a camada antiga do Google Earth antes de importar novamente.

---

## 2026-08-12 — Correções de segurança da auditoria completa

**Agente:** Claude

**Contexto:** auditoria completa do sistema (código local + comparação
com produção real, `sightops-prod-api`/`sightops-prod-nginx` em
10.10.12.7) achou dois bugs críticos de isolamento entre tenants, mais
outros achados médios/baixos. Corrigidos em três commits:
`ab05124`, `30f64c4`, `a6aa52d`.

**Arquivos alterados:**
- `app/api/endpoints/maintenance.py` — proxy web de câmera
  (`/api/maintenance/web/{ip}/...`) agora exige que o IP pertença ao
  inventário do tenant atual (`_ip_belongs_to_current_tenant`), bloqueia
  loopback/link-local, encaminha header `Authorization`.
- `app/api/endpoints/cameras.py` (`api_snapshot_save`) — removido
  fallback que aceitava path absoluto arbitrário do disco.
- `app/services/photo_store.py` — fallback pros diretórios globais de
  snapshot só roda com tenant vazio ou `"default"` (mesmo guard que
  `app/main.py` já tinha).
- `app/services/auth_store.py` (`delete_tenant`) — agora também apaga
  `tenant_data_dir(slug)` do disco.
- `app/api/endpoints/auth.py` — `update_tenant`/`delete_tenant` respondem
  403 (não 400) pra quem não é admin de plataforma.
- `app/services/pdf_inventory_report.py` — pasta de relatórios PDF agora
  é tenant-scoped (`_reports_dir()`).
- `app/core/security.py` — `POST /api/network/tools/run` exige
  `operator`, `POST /api/system/bootstrap` exige `admin`.
- `app/services/windows_inventory_service.py` — token legado global do
  Windows Agent controlado por `WINDOWS_AGENT_LEGACY_TOKEN_ENABLED`
  (default ligado).
- `scripts/sightops_hikvision_switch_test.py` — senha real de switch de
  cliente trocada por valores sintéticos.
- `Dockerfile` — uvicorn com `--no-access-log` (evitava vazar
  `live_token` no log do container).

**Validado:** `scripts/check.py` local — só os 5 testes que já falhavam
antes (bug de `sys.path` em arquivos de teste recentes, sem relação com
este trabalho: `sightops_camera_recorder_fallback_test.py`,
`sightops_dashboard_snapshot_count_test.py`,
`sightops_kmz_layer_actions_test.py`,
`sightops_zabbix_access_service_test.py`,
`sightops_zabbix_status_sync_autoupsert_test.py`) continuam falhando.
`sightops_camera_web_proxy_test.py` foi ajustado pra checagem nova de
posse de IP e passa. Deploy aplicado em produção real
(`sightops-prod-api`) via hotfix — exceto o `Dockerfile`, que precisa de
rebuild de imagem (ainda não aplicado em produção).

**Não reverter:**
- A checagem de posse de IP em `_camera_web_target_url`/
  `_ip_belongs_to_current_tenant` — sem ela, um cliente volta a acessar
  câmera/serviço HTTP privado de outro.
- O guard de fallback legado em `photo_store.py`/`api_snapshot_save` —
  sem ele, dois tenants com câmera no mesmo IP privado vazam snapshot um
  do outro, ou qualquer operator lê arquivo arbitrário do servidor.

**Próximo passo:** rebuild de imagem pra aplicar o `--no-access-log` do
Dockerfile em produção. Itens ainda abertos, fora do escopo deste
trabalho: chave SSH `_tmp_sightops_deploy_ed25519` solta na raiz (decisão
do usuário sobre remover/rotacionar); Easy Backup (CORS aberto, defaults
fracos) não mexido — usuário confirmou que é serviço descontinuado.

---

## 2026-08-13 — Auditoria de acompanhamento (mudanças em andamento)

**Agente:** Claude

**Contexto:** usuário pediu nova auditoria completa após um incidente de
CPU em produção (`sightops-prod-api` travado a ~100% por tempo
prolongado, mitigado com `docker restart`, causa raiz não confirmada).
A auditoria revisou o working tree sujo (34 arquivos, nenhum commit local
à frente do `origin/main`) e encontrou, entre outras coisas, que esta
entrada de handoff (a de cima, "Correções de segurança da auditoria
completa") tinha sido **substituída inteira** pela entrada do Codex sobre
KMZ, em vez de ficar empilhada acima dela. Restaurada agora — ver arquivo
completo de novo.

**Achados da auditoria (resumo, não corrigidos ainda nesta entrada além
do que está listado abaixo):**
- `app/services/zabbix_monitoring_service.py` (`_default_zabbix_cfg`) —
  tinha fallback hardcoded pra credencial de fábrica do Zabbix
  (`Admin`/senha padrão) quando a config do tenant não está setada.
  **Corrigido nesta entrada**: agora levanta erro explícito em vez de
  tentar logar com a credencial padrão.
- `app/cli/tools/olt_4840e_collect_macs.py` (código novo, +601 linhas,
  sessão SSH legada via `subprocess`+`sshpass`/Telnet pra OLT 4840E) —
  não tem teto de tempo agregado pro `collect_macs()` inteiro, só timeout
  por comando individual. Candidato mais provável (não confirmado) pra
  explicar lentidão prolongada, ainda não corrigido — próximo agente que
  mexer nisso, adicionar timeout agregado.
- `recorder_media_service.EXPORT_DIR` e parte de `scan_service.py` ainda
  usam caminho de arquivo global (`DATA_DIR`) sem `tenant_slug` — não
  corrigido nesta entrada, precisa confirmar se é só fallback de tenant
  vazio/"default" ou vazamento real entre clientes antes de mexer.

**Arquivos alterados nesta entrada:**
- `app/services/zabbix_monitoring_service.py` — removido fallback de
  credencial hardcoded.
- `docs/HANDOFF_AGENTES.md` — restaurada a entrada anterior que tinha
  sido apagada.

**Não reverter:**
- A remoção do fallback de credencial Zabbix — sem ela, o sistema tenta
  logar sozinho com usuário/senha de fábrica quando a config real não
  está setada, o que é uma dependência oculta de credencial fraca.

**Próximo passo:** confirmar por SSH em 10.10.12.7 se o código deste
working tree já foi copiado pro container `sightops-prod-api` (deploy é
manual, não vem do git) antes ou depois do incidente de CPU de hoje —
isso decide se as mudanças em `maintenance_ping_service.py`/
`ws_scan_service.py`/`zabbix_monitoring_service.py` já ativas nesta
sessão são a causa do travamento ou uma correção feita depois.

---

## 2026-08-14 - Regra de IP ignorado no inventario OLT

**Agente:** Codex

**Contexto:** o usuario apaga linhas OLT enquanto organiza documentacao/KMZ.
Essas linhas precisam ficar bloqueadas para a sincronizacao automatica da OLT
nao recriar sujeira a cada ciclo, mas nao podem virar bloqueio permanente:
quando uma varredura manual encontrar novamente uma camera real, o IP deve
sair automaticamente da lista de ignorados.

**Arquivos alterados nesta entrada:**
- `app/services/olt_ignore_list.py` - novo `remove_ignored_rows()`.
- `app/services/scan_service.py` - varredura HTTP manual remove da lista de
  ignorados os IPs encontrados.
- `app/services/rescan_service.py` - rescan de IP unico tambem reabilita o IP.
- `app/services/ws_scan_service.py` - varredura manual via conector remoto
  tambem reabilita IPs encontrados.
- `scripts/sightops_manual_scan_restores_ignored_test.py` - regressao dedicada.

**Nao reverter:** a separacao e intencional. O sync automatico da OLT continua
respeitando `olt-ignored-ips.json`; somente caminhos manuais de scan/rescan
podem retirar IPs dessa lista quando a camera for encontrada de novo.

---

## 2026-08-18 - Camera apagada voltava e site novo roubava IP do antigo

**Agente:** Claude

**Contexto:** reclamacao do usuario em producao: "eu apago as cameras mas elas
voltam, e coloco um site novo numa varredura e ele pega o IP antigo e mistura
tudo". Eram tres defeitos distintos, todos confirmados no codigo.

**1. Varredura desfazia a exclusao.** `scan_service` e `ws_scan_service`
chamavam `remove_ignored_rows()` com TUDO que a varredura encontrasse. Bastava
varrer a faixa do site para o proprio sistema desbloquear e recadastrar todas
as cameras que o usuario tinha apagado.

**Nao revertemos a entrada de 2026-08-14** (Codex): varredura manual continua
reabilitando IP ignorado. O que mudou e o ALCANCE -- agora so reabilita quando
o alvo foi digitado IP a IP (`100.65.10.72` ou lista separada por virgula),
que e o pedido explicito "quero esse de volta". Alvo em faixa
(`100.65.10.1-100.65.10.100`) ou CIDR (`/24`) e descoberta ampla: ali o
bloqueio do usuario continua valendo e as linhas ignoradas sao FILTRADAS antes
de salvar. `scripts/sightops_manual_scan_restores_ignored_test.py` continua
passando.

**2. Bloqueio so valia no modo OLT.** `is_ignored_olt_row` so era consultado em
`olt_service`, e `tools.py` so gravava o bloqueio `if permanent and mode ==
"olt"`. Nos modos Basico e Switch, apagar nunca grudava. Alem disso o escopo
exigia que `olt_ip`/`pon`/`onu_id` batessem -- campos que varredura basica nao
traz --, entao o bloqueio nao casava e a camera voltava. Agora site/conector
continuam rigidos (protegem IP privado repetido entre clientes) e os campos de
topologia so sao comparados quando a linha nova os informa.

**3. Merge cruzava sites.** Para linha local (sem conector), `_merge_inventory_rows`
casava por `IP:` (a chave logica), depois por IP sozinho e por MAC sozinho, sem
olhar site. Como 100.65.x se repete em todo cliente, a camera do site novo
casava com a linha do site antigo -- e `site`/`local`/`site_name` estao na lista
de campos que sempre sobrescrevem, entao o registro antigo passava a apontar
para o site novo. Agora nenhuma regra de match cruza linhas de sites diferentes.
`_apply_default_local` tambem so carimba o site nas linhas desta varredura
(antes pegava o inventario inteiro e batizava qualquer linha sem `local`).

**Arquivos alterados:**
- `app/services/olt_ignore_list.py` - `filter_ignored_rows()`, alias
  `is_ignored_row`, escopo tolerante em topologia.
- `app/services/scan_service.py` - `_explicit_target_ips()`, filtro de
  ignorados, guarda de site no merge, `_apply_default_local(only_ips=...)`.
- `app/services/ws_scan_service.py` - mesmo criterio na varredura via conector.
- `app/api/endpoints/tools.py` - bloqueio gravado nos tres modos.
- `frontend/js/bootstrap.js` - "Apagar inventario" manda `permanent` nos tres modos.
- `scripts/sightops_scan_respects_deleted_test.py` - regressao dos tres casos.

**Allowlist estrita: IMPLEMENTADA na mesma sessao (ver entrada abaixo).**


---

## 2026-08-18 - Inventario declarativo: allowlist de IPs por site

**Agente:** Claude

**Contexto:** pedido direto do usuario -- "eu nao quero ele descobrindo IP, eu
que digo qual IP ele deve olhar". A varredura era autoritativa: cadastrava tudo
que respondia na faixa, e o usuario passava o dia apagando. Agora a lista dele
manda.

**Modelo escolhido (por ele): ESTRITO.** O que nao esta na lista do site nao
entra e nao vira pendencia -- e descartado. Nao ha tela de aprovacao.

**Ativacao por site, nao global.** `site_is_enforced()` so retorna True se o
site tem lista cadastrada e o modo estrito esta ligado. Site sem lista continua
com o comportamento antigo -- ligar isso num cliente nao quebra os outros. Foi
de proposito: nao existe flag global que bote todo mundo em estrito de uma vez.

**Onde e o corte** (os tres caminhos que criam camera sozinhos):
- `app/services/scan_service.py` - varredura HTTP, filtra ANTES do merge.
- `app/services/ws_scan_service.py` - varredura via conector remoto.
- `app/services/olt_service.py` - sync automatico da OLT (era o pior: recriava
  em background sem o usuario pedir nada).

**Arquivos alterados:**
- `app/services/camera_allowlist.py` (novo) - store por tenant
  (`camera-allowlist.json`), aceita IP, faixa `10.0.0.10-20` e CIDR.
- `app/api/endpoints/tools.py` - `GET/POST /api/inventory/allowlist`
  (actions: set, add, remove, enforce). Ficou em tools.py de proposito, pra nao
  disputar `endpoints/__init__.py` e `main.py` com quem mexe em outra area.
- `frontend/index.html` - botao "IPs permitidos" e modal (`modalAllowlist`);
  versoes de cameras.js/bootstrap.js incrementadas (cache-busting).
- `frontend/js/cameras.js` - `openAllowlistModal`, `allowlistSave`,
  `allowlistImportFromInventory` (puxa os IPs que ja estao no inventario do
  site pra virar a lista inicial).
- `frontend/js/bootstrap.js` - listeners do modal.
- `scripts/sightops_camera_allowlist_test.py` - regressao.

**Contadores novos** no retorno do scan, uteis pra depurar em producao:
`blocked_allowlist_count` (varredura) e `blocked_allowlist` (sync OLT).

---

## 2026-08-18 - Apagar e apagar: allowlist substitui a lista de bloqueados

**Agente:** Claude

**Contexto:** pergunta do usuario -- "pq eu preciso dessa lista de IPs
bloqueados? pq nao posso simplesmente apagar, e se eu quiser chamo de novo?".
Ele esta certo. A lista de bloqueados (`olt-ignored-ips.json`) so existia porque
varredura e sync da OLT recriavam linha por conta propria; era remendo, nao
solucao. Com allowlist estrita ela vira redundante -- sair da lista de
permitidos ja garante que nao volta.

Pior: do jeito que ficou na entrega anterior, as duas listas podiam se
contradizer. Apagar camera em site estrito gravava o IP nos bloqueados mas
**nao tirava dos permitidos** -- dois cadastros discordando sobre o mesmo IP.

**Regra agora:**
- Site COM allowlist ligada -> apagar remove o IP da lista de permitidos e
  **nao** grava bloqueio. Para trazer de volta, recolocar o IP na lista.
- Site SEM allowlist -> comportamento antigo, continua dependendo da lista de
  bloqueados (sem ela a varredura recadastra tudo).

**Arquivos alterados:**
- `app/services/camera_allowlist.py` - `forget_rows()`, que separa as linhas de
  site declarativo (tira da allowlist) das de site legado (`rows_legado`, que o
  chamador ainda manda pra lista de bloqueados).
- `app/services/inventory_delete_service.py` - remocao de cameras selecionadas.
- `app/api/endpoints/tools.py` - "Apagar inventario" por site e total.
- `frontend/js/bootstrap.js` - o toast diz quantos IPs sairam da lista.
- `scripts/sightops_apagar_e_chamar_de_volta_test.py` - ciclo completo:
  autoriza -> varre -> apaga -> varre de novo (nao volta) -> recoloca na lista
  -> varre (volta). Cobre tambem que site sem allowlist segue usando bloqueio.

**Nao "simplificar" removendo a lista de bloqueados.** Ela continua sendo a
unica protecao dos sites que ainda nao migraram pro modo declarativo.

---

## 2026-08-19 - Zabbix acumulava host fantasma (o sync nunca removia)

**Agente:** Claude

**Contexto:** o usuario apagou o inventario e as cameras continuaram no Zabbix.
Medido em producao: **1462 hosts fantasma** -- 377 em `Cameras - EASY-TECNOLOGIAS`
(116 no inventario, 493 no Zabbix) e 1085 em `Cameras - RADS` (469 x 1554). No
sentido inverso, ZERO faltando: prova de que o sync so somava.

**Causa:** `tools/mk_zabbix_from_inventory.py` so tinha `host.create`/`host.update`
-- nenhum `host.delete`. E `api_inventory_clear` (apagar inventario) nao menciona
Zabbix. Ou seja, apagar inventario NUNCA teve efeito la.

O padrao certo ja existia no proprio sistema: `monitoring_service._observe_many()`
poda o que sumiu (`prune_entity_type=...`), e por isso o monitoramento estava
limpo (116/116 e 469/469, zero orfaos). O Zabbix so nao tinha sido ligado nisso.

**Correcao:** `prune_hosts()` no script do Zabbix, com tres travas:
1. age SO dentro do grupo do tenant (`Cameras - <TENANT>`);
2. so mexe em host com o padrao criado pelo sistema (`<TENANT>-CAM-...`), entao
   host cadastrado a mao nunca e tocado (medido: 0 fora do padrao hoje);
3. so roda com `ZBX_PRUNE=1`, que `maintenance.py` liga **apenas no sync do
   inventario completo** -- com filtro de site a lista e parcial e podar
   apagaria os hosts dos outros sites.

**Validacao:** rodado contra o Zabbix real com `host.delete` interceptado --
identificou exatamente os 1085 orfaos de rads e nao apagou nada.

**Passivo:** os 1462 fantasmas saem sozinhos no primeiro sync completo (sem
filtro de site). Existe tambem `zbx_limpar.py` (simulacao por padrao,
`--executar` para valer, backup em `/app/data/zabbix-backup/`).

**Arquivos:** `tools/mk_zabbix_from_inventory.py`, `app/api/endpoints/maintenance.py`.
Imagem publicada: `sightops-prod-api:20260819-zbx-prune`.

---

## 2026-08-19 - O bloqueio de camera expirava sozinho

**Agente:** Claude

**Contexto:** varredura de consistencia em producao achou IPs que estavam na
lista de bloqueio E no inventario ao mesmo tempo: 4 em rads, 10 em
easy-tecnologias. Era a peca que faltava do "apago e volta".

**Causa:** `_matches_scope` exigia que **site** e **topologia**
(`pon`/`onu_id`/`onu_serial`) continuassem iguais aos do momento da exclusao.
Essas coisas mudam na operacao normal. Casos reais medidos:
- `100.65.10.101/.102`: bloqueio guardou `site='BARRA DE SAO MIGUEL'`, a linha
  ja estava como `'PRAIA BONITA'` (site renomeado) -> nao casava.
- `100.65.10.138`: bloqueio guardou `onu_id='8'`, linha em `onu_id='9'`
  (camera remanejada de ONU) -> nao casava.

Resultado: o bloqueio deixava de valer silenciosamente e a camera voltava.

**Correcao:** casar pelo que NAO muda -- **conector + IP**. O IP ja e a chave do
registro e o conector identifica o cliente (mesma identidade de
`inventory_row_key`). A lista tambem ja e por tenant
(`tenants/<slug>/olt-ignored-ips.json`), entao nao ha risco entre clientes.
Site so decide quando NAO existe conector dos dois lados; topologia virou
informacao, nunca criterio.

**Validacao:** 14/14 casos reais de producao reconhecidos (4 rads + 10 easy).
Travas conferidas: mesmo IP com conector diferente NAO casa (False); mesmo
conector com site renomeado casa (True). Os tres testes de regressao passam.

**Nao endurecer de novo.** A tentacao e exigir mais campos "para ter certeza".
Foi exatamente isso que quebrou: quanto mais campos no criterio, mais facil o
bloqueio expirar sozinho quando o usuario reorganiza o inventario.

Imagem: `sightops-prod-api:20260819-bloqueio`.

---

## 2026-08-26 tarde — Cadastro de aluno: matricula como chave, importacao e site

**Contexto:** a escola cadastrou 91 pessoas e nada garantia unicidade. O mesmo
aluno gravado duas vezes criava dois registros (UUID diferente), e o ID na
controladora era digitado a mao -- foi assim que a ELISHAFAN acabou com
matricula `2` e usuario `1033` no equipamento, duplicada na catraca.

**O que mudou:**

**Matricula virou a chave de negocio.** Indice unico `(tenant_slug,
enrollment_code)` com matricula vazia fora do indice (visitante segue sem).
`save_person` casa por matricula: regravar atualiza em vez de criar outro UUID.
Dar a matricula de um aluno a outro e recusado com mensagem clara.

**ID da controladora deriva da matricula.** O aluno 1577 e o usuario 1577 no
equipamento -- uma identidade so, do cadastro a catraca. Matricula nao numerica
cai no proximo numero livre. **Nunca sorteia**: numero sorteado pode colidir com
quem ja existe no dispositivo, e ai o reconhecimento aponta para o aluno errado.

**Importacao por planilha** (`app/services/access_control_import.py` +
`POST /people/import`). Le XLSX e CSV, reconhece a coluna pelo nome (nao pela
posicao), e tem pre-visualizacao obrigatoria: sem `aplicar=true` nada e gravado.
Trata matricula que o Excel devolve como float, telefone em varios formatos,
matricula repetida no arquivo, e CSV do Excel brasileiro (`;` + latin-1).

**Site virou seletor, alimentado pelas CONTROLADORAS.** E o site do dispositivo
que vai no evento e decide por qual canal a notificacao sai; digitar a mao abria
divergencia de uma letra que quebrava o roteamento **em silencio**.

**Obrigatorio onde deve ser.** Matricula (aluno) e site sao exigidos no
**endpoint** do cadastro, nao no `save_person`. Primeira tentativa colocou a
exigencia no store e quebrou 8 testes que passavam -- aquele caminho tambem
serve importacao e rotinas internas.

**Atencao ao mexer:** `import_device_people` ainda grava
`enrollment_code = controller_user_id` para pessoa nova. Com a matricula virando
o ID da controladora isso deixou de conflitar na pratica, mas se um dia a escola
cadastrar no equipamento com numeracao propria, os dois mundos voltam a divergir.

**Dados corrigidos em producao (rads):** ELISHAFAN matricula `2` -> `1033`
(igual ao ID da controladora); 89 pessoas sem site receberam
`ESCOLA PRESIDENTE DUTRA`. Nenhuma pessoa tem mais matricula != ID da
controladora.

**Testes:** `sightops_access_control_matricula_test.py` e
`sightops_access_control_import_test.py`. As 4 falhas restantes da bateria
(`controller_import`, `route`, `routes`, `shell`) sao anteriores e nao foram
introduzidas aqui.

---

## 2026-08-27 manha — Nome de fabrica apagava titulo; Manutencao vazia no switch

**Sintoma 1:** os titulos das 48 cameras da San Marine, cadastrados a mao, sumiam
sozinhos. Um scan as 09:15 desfez o trabalho inteiro.

**Causa:** `_merge_inventory_rows` protege o titulo ja cadastrado, mas
`is_placeholder_title` so considerava placeholder o valor **vazio ou igual ao
IP**. "IP CAMERA" e o nome de fabrica da Hikvision e passava como titulo
legitimo -- entao cada varredura lia o nome de fabrica do equipamento e
sobrescrevia o nome do usuario.

**Correcao:** `TITULOS_DE_FABRICA` (IP CAMERA, IPCAMERA, CAMERA, NETWORK CAMERA,
IPC, DVR, NVR, SEM NOME, NO NAME) tambem conta como placeholder. Testado no
container antes de publicar: scan trazendo "IP CAMERA" nao derruba mais
"06 - PORTOES".

**Nao remover essa lista.** Sem ela, qualquer cliente perde os nomes das cameras
na proxima varredura, e o sintoma e lento de perceber -- so aparece quando
alguem repara que a tabela voltou a ficar generica.

**Sintoma 2:** Manutencao > Cameras IP aparecia **vazia** para a San Marine.

**Causa:** `loadMntCam()` pedia `/api/cameras?mode=olt` fixo. Cada cliente usa um
modo OU outro:

    san-marine    olt:   0   switch:  48
    rads          olt: 536   switch:   0
    easy-tecno.   olt: 382   switch:   0
    inforbr       olt:  42   switch:   0

A San Marine e o primeiro cliente de switch, entao a tela sempre esteve vazia
para ela -- inclusive as operacoes em lote (reboot, senha, renomear, NTP).

**Correcao:** seletor de visao (Basico/OLT/Switch) na barra de filtros, como
caixa `<select>` no estilo do filtro de sites. Os tres modos ficam sempre
visiveis de proposito: esconder o vazio parecia perda de funcionalidade.
Escolha guardada em sessionStorage.

**Dados aplicados em producao (san-marine):** as 48 cameras receberam titulo,
switch, porta e VLAN a partir do relatorio de 07/08. Gravado via
`load/save_inventory_json`, **nao** no `cam-inventory-switch.json` -- o
inventario vive no banco e o arquivo e resquicio legado; editar o arquivo nao
muda nada na tela.

**Achado operacional:** SWITCH-06 e SWITCH-08 estao com 6 de 6 cameras offline
cada. 12 das 15 quedas concentradas em dois switches quase nunca sao 12
defeitos.

---

## 2026-08-27 tarde — Seguranca do webhook do WhatsApp e token em repouso

Revisao de seguranca encontrou duas falhas, ambas introduzidas na migracao para
a Cloud API destes dois dias.

**1. Webhook publico sem verificacao de assinatura (Alto).** O POST em
`/api/access-control/whatsapp/meta/{tenant}` e publico -- a Meta chama de fora --
e o GET conferia o verify token, mas o POST nao conferia nada. Com apenas a URL
era possivel:

- forjar mensagem recebida e injetar item de triagem no cliente;
- fazer o sistema **responder para um numero escolhido pelo atacante**, porque o
  destino da resposta automatica vem do `from` do payload. As mensagens sairiam
  do numero oficial da escola, cobradas do cliente, queimando a reputacao da
  conta.

E nem era preciso saber o `phone_number_id`: sem `metadata`, o codigo cai no
`tenant_slug` da URL.

**Correcao:** `assinatura_webhook_valida()` confere `X-Hub-Signature-256` (HMAC
SHA-256 sobre os **bytes crus** -- reserializar o JSON mudaria o resultado).
Payload invalido e descartado com **200**, nunca erro: erro faz a Meta reenviar
e depois desativar o webhook.

**Armadilha que me pegou:** a primeira versao passou em todos os testes locais e
**nao bloqueava nada**. O App Secret e um so, do app inteiro, mas eu o guardei na
config por site da RADS -- e a checagem roda ANTES de resolver de quem e a
mensagem, entao nao achava segredo e liberava. O segredo mora no cliente **dono
do app** (slug da URL), e o endpoint entra nesse contexto antes de verificar.

Provado contra producao pela internet: sem assinatura e com assinatura forjada
-> `ignored`; com assinatura valida -> `handled`.

**2. Token da Meta em texto puro (Medio).** `access_token` e `app_secret`
ficavam legiveis nas configuracoes, enquanto senha de OLT/camera/switch ja
passava por `app.core.crypto`. O token nao expira e envia mensagem em nome da
escola.

**Correcao:** cifrados na gravacao, decifrados na leitura. `decrypt()` devolve
como veio o valor sem prefixo, entao token gravado antes continua funcionando.
Os valores que ja estavam em claro foram regravados cifrados em producao.

**Campo vazio preserva o segredo atual** -- editar outro campo do formulario nao
pode apagar credencial. Ha teste cobrindo isso.

**Nao afrouxar:** sem App Secret configurado a checagem nao roda e o webhook
fica aberto (bloquear pararia quem ja esta no ar). Isso e proposital, mas gera
WARNING a cada chamada. Cliente novo com webhook precisa do App Secret.

**Teste:** `scripts/sightops_whatsapp_webhook_seguranca_test.py`.

## 2026-09-01 — Driver de escrita da OLT VSOL EPON (Japaratinga) e a armadilha do auth-mode

Implementado `add_onu_vsol`/`delete_onu_vsol`/`reboot_onu_vsol` em
`app/cli/tools/olt_vsol_epon.py` (plano
`docs/superpowers/plans/2026-09-01-olt-vsol-epon-write-driver.md`),
encaixados em `olt_service.py`/`olt_capabilities.py`, com painéis novos na
tela Implantação > ONU. Corrige um bug real que já existia (não usado):
`build_delete_onu_vsol_command` montava `deregister` (só desconecta) em vez
de `no onu auth onuid <id>` (remove a autorização de verdade) — confirmado
no manual e validado ao vivo.

**Achado crítico só descoberto testando ao vivo (cliente RADS, OLT
192.168.200.2, ONU 0/2/7):** o comando corrigido (`no onu auth onuid`)
rodava sem erro, mas a ONU voltava sozinha em 1-2 segundos — o mesmo
sintoma do bug antigo. Causa: as 4 PONs desta OLT estavam com
`onu auth-mode disable` (autenticação desligada), modo em que a OLT
autoriza automaticamente QUALQUER ONU fisicamente conectada, ignorando
whitelist e binding de onu-id. Nenhum comando de exclusão resolve isso
sozinho nesse modo — é preciso `onu auth-mode mac` ativo.

**Corrigido ao vivo, com cuidado:** populei a whitelist (`onu mac-auth add
<mac>`) de todas as 21 ONUs já conectadas nas 4 PONs (8+7+6+0) ANTES de
trocar o modo — sem isso, trocar o modo direto derruba geral. Troquei PON
por PON, com checagem de quem continuava online e reversão automática pra
`disable` se algo caísse (não caiu nada de vez — só um soluço de ~10s de
reconexão durante a troca, que se resolveu sozinho). Confirmado depois:
excluir/reautorizar/reiniciar funcionam corretamente com `mac-auth` ativo.
Ver memória `vsol-japaratinga-authmode-macauth.md` pra detalhe completo.

**Efeito colateral permanente e intencional:** a partir de agora, ONU nova
que o cliente conectar nesta OLT NÃO entra sozinha — precisa ser autorizada
explicitamente (tela "Autorizar ONU" ou `add_onu_vsol`). Antes disso era
plug-and-play sem controle nenhum. Se aparecer relato de "ONU nova não
conecta" nesta OLT específica, isso é o comportamento esperado agora, não
regressão.

**Dois achados menores da validação ao vivo, já corrigidos e revalidados ao
vivo na sequência:**
- `onu_signal_vsol`/`collect_onu_telemetry_vsol` usavam
  `show onu <id> ctc pon monitor_status` pra ler potência óptica -- nesta
  OLT esse comando só informa se o monitoramento periódico está
  ligado/desligado (sempre veio `disable`), nunca a leitura real, então
  `onu_rx` nunca aparecia. Comando certo, achado via `show onu ? `no CLI:
  `show onu opm-diag` -- traz temperatura/tensão/bias/TX/RX de **toda a
  PON numa tabela só** (mais rápido que um `monitor_status` por ONU, que
  era o que `collect_onu_telemetry_vsol` fazia). Novo parser
  `parse_onu_opm_diag`, mesmos nomes de campo de saída (`onu_rx`, `onu_tx`,
  `temperatura`, `voltagem`, `bias`). Revalidado ao vivo: `onu_rx: -11.52`
  na ONU 0/2/7.
- `add_onu_vsol` tentava ler a posição de volta 3x com 2s de espera (6s
  total) -- na prática o registro real da OLT levou entre 10 e 30s, então
  `pending: true` (sem onu_id) era o caso comum, não raro. Aumentado pra 6
  tentativas de 5s (30s no total).

**Teste:** `scripts/sightops_olt_vsol_add_onu_test.py` (autorizar/excluir/
reiniciar) e `scripts/sightops_olt_vsol_opm_diag_test.py` (parser de
potência óptica + `onu_signal_vsol`, novo). Nenhum deploy em produção feito
ainda -- a branch foi mesclada no `main` local, toda validação ao vivo
(incluindo estes dois fixes) rodou direto contra o código do worktree/main
local, sem tocar no container de produção, via `docker run --rm` efêmero
na mesma network (`sightops-prod-platform`).
