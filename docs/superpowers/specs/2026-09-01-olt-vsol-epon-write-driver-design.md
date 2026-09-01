# Driver OLT VSOL EPON — Autorizar/Excluir/Reiniciar ONU — Design

## Contexto

O SightOps já tem um driver VSOL EPON funcional em
`app/cli/tools/olt_vsol_epon.py` (713 linhas), homologado em 20/08/2026
contra a OLT real do cliente RADS, conector Japaratinga (IP 192.168.200.2,
acessada via Mikrotik 100.66.10.1). Read-path completo e validado ao vivo:
`collect_macs_vsol`, `collect_onu_telemetry_vsol`, `discover_onus_vsol`,
`find_onu_vsol`, `onu_signal_vsol`. `olt_capabilities.py` já declara
`collect_macs`, `telemetry`, `discover_onus`, `find_onu`, `onu_signal` como
`True` pra `vsol_epon`, com a nota: "Autorizar e excluir ONU seguem
bloqueados até homologação do provisionamento."

**Objetivo desta entrega:** implementar de verdade `add_onu_vsol`,
`reboot_onu_vsol`, e **corrigir** `delete_onu_vsol` (existe uma função
`build_delete_onu_vsol_command` que monta comandos, mas nunca foi ligada a
uma função que abre sessão e executa — e, pior, os comandos que ela monta
são o comando ERRADO, ver abaixo). Ao final, os 5 métodos (descobrir,
autorizar, consultar, excluir, reiniciar) funcionam nesta OLT, igual à
8820i e à 4840E.

## Fonte primária usada neste design

Manual oficial "UPLINK EP Series OLT CLI User Manual v1.2" (mesma base de
firmware do prompt real já confirmado ao vivo, `epon-olt(config-pon-0/1)#`
— a estrutura de comandos do manual bate exatamente com o que o driver já
usa: `configure terminal` → `interface epon <slot/port>` → comandos `onu
...`). Seções relevantes: 17.1.1 a 17.1.4 (autenticação/whitelist), 17.1.2
("Remove authorized ONU"), 17.1.3 ("Deregister or reset ONU"), 17.2.12
("Restart ONU").

## Achado crítico: a exclusão já escrita no driver está errada

`build_delete_onu_vsol_command` (já existe, nunca foi ligada) monta:

```
deregister onu auth onuid <onuid>     # se tem onuid
deregister onu unauth <mac>           # se só tem mac
```

O manual (seção 17.1.3, "Deregister or reset ONU") diz que `deregister`
**só desconecta a ONU (fica offline), não remove a autorização** — é o
equivalente a "reiniciar/desconectar", não a "excluir". A remoção de
verdade é outro comando, seção 17.1.2 ("Remove authorized ONU"):

```
no onu auth onuid <onuid>
```

Ou seja, se essa função fosse ligada como estava, "Excluir ONU" na tela
teria desconectado a ONU sem tirar a autorização — ela voltaria sozinha
na hora seguinte. Este design substitui o comando por `no onu auth onuid
<onuid>`, o correto pra "excluir".

## Comandos confirmados (todos dentro de `interface epon <slot/port>`)

| Ação | Comando | Fonte |
|---|---|---|
| Autorizar (whitelist) | `onu mac-auth add <mac>` | Manual 17.1.4 |
| Ler onu-id atribuído | `show onu auth-info` (já parseado por `parse_onu_auth_info`) | já existe no driver |
| Excluir (remove autorização) | `no onu auth onuid <onuid>` | Manual 17.1.2 |
| Reiniciar | `onu <onuid> ctc reset` | Manual 17.2.12 |
| Modo de autenticação (leitura) | `onu auth-mode` — já deve estar em `mac`, dado que a OLT já está homologada nesse esquema; nunca sobrescrever | Manual 17.1.1 |

Endereçamento: PON no formato `0/N` (já tratado por `_rotulo_da_pon` /
`_entra_na_pon`, existentes). ONU é identificada por `onuid` (posição
inteira dentro da PON), não por MAC, para excluir/reiniciar — mesmo
padrão de leitura que `onu_signal_vsol`/`find_onu_vsol` já usam.

## Diferença de modelo: VSOL não é GPON nem é 4840E puro

- Como a 4840E: autorização é por MAC (whitelist), a OLT atribui a
  posição sozinha.
- Como a 8820i (GPON): excluir e reiniciar já são por posição
  (`pon`+`onuid`), não por MAC — não precisa resolver endereço primeiro.

## Decisões de escopo (confirmadas com o usuário)

- **Autorizar, corrigir excluir, reiniciar** — só isso. Descobrir/consultar
  já funcionam e não mudam.
- **Sem posicionamento manual** — mesma decisão já tomada pra 4840E: a OLT
  atribui o onu-id, não expomos escolha manual.
- **Tela**: blocos de campo dedicados por driver (mesmo padrão da 4840E:
  `onuAddFieldsVsol`, `onuQueryFieldsVsol` etc., escondidos por padrão,
  aparecem quando a OLT selecionada é VSOL) — decisão explícita do usuário
  de manter os três drivers visualmente isolados, mesmo VSOL usando
  campos de posição iguais aos da GPON pros passos de consultar/reiniciar/
  excluir. Sem reaproveitar os blocos GPON existentes.
- **Fora de escopo**: LOID/autenticação híbrida (só MAC-auth, que é o que
  já está configurado nesta OLT), template management (seção 17.8 do
  manual), qualquer coisa de voz/WiFi/WAN (ONU é câmera, não roteador
  residencial).

## Arquitetura

### Backend — estende o arquivo já existente

`app/cli/tools/olt_vsol_epon.py` ganha três funções novas, seguindo o
mesmo padrão de sessão já usado (`_com_sessao_vsol`, `_entra_na_pon`,
`_manda`, `_espera_prompt`):

- `add_onu_vsol(olt_ip, user, password, pon, mac, port=22, timeout=...)`
  — entra na PON, roda `onu mac-auth add <mac>`, confere que não veio
  erro, então lê `show onu auth-info` e localiza a ONU recém-autorizada
  pelo MAC (mesmo parser `parse_onu_auth_info` já existente) pra devolver
  o `onu_id` atribuído. Tenta até 3 vezes, com 2 segundos de espera entre
  cada `show onu auth-info` (a OLT pode levar alguns segundos pra MPCP
  registrar); se não aparecer depois das 3 tentativas, retorna `ok: True`
  com `onu_id: ""` e um aviso "autorizada, aguardando registro" em vez de
  travar esperando — decisão explícita: não é seguro segurar a sessão SSH
  indefinidamente.
- `delete_onu_vsol(olt_ip, user, password, pon, onuid, port=22, timeout=...)`
  — entra na PON, roda `no onu auth onuid <onuid>`, confere erro.
  `build_delete_onu_vsol_command` é **corrigida** (não removida — outras
  partes podem depender da assinatura) pra montar `no onu auth onuid
  <onuid>` em vez do `deregister` errado; a função nova a usa.
- `reboot_onu_vsol(olt_ip, user, password, pon, onuid, port=22, timeout=...)`
  — entra na PON, roda `onu <onuid> ctc reset`, confere erro. Sem
  confirmação y/n nesta OLT (diferente da 4840E) — mas ainda assim nunca
  envia comando algum sem antes ler a resposta do anterior, seguindo o
  padrão de sessão já usado em todo o arquivo.

Detecção de erro: reaproveita o texto real já visto nesta OLT
(`unknown command`, `% invalid`) — mesmo critério que `_entra_na_pon` já
usa.

### Backend — encaixe no `olt_service.py`

`_is_vsol(req)` já existe e já é usado em 4 pontos (`discover_onus`,
`collect_onu_telemetry`, `find_onu`, `onu_signal`). Este design adiciona
o mesmo `elif _is_vsol(req):` em três funções que hoje não têm esse
branch: `add_onu`, `delete_onu`, `reboot_onu`.

### Backend — capabilities

`olt_capabilities.py`, bloco `vsol_epon`: acrescenta `add_onu: True`,
`delete_onu: True`, `reboot_onu: True`. Nota atualizada removendo "seguem
bloqueados".

### Frontend — blocos dedicados por driver (padrão já estabelecido)

Mesmo mecanismo já usado pra 4840E (`onuIsEpon`/`onuToggleEponFields`,
generalizado): cada painel ganha um `onuIsVsol(row)` e um conjunto de
campos `...FieldsVsol`, escondido por padrão.

| Passo | Campos VSOL |
|---|---|
| Autorizar | PON (dropdown) + MAC (texto) — sem descrição/VLAN/porta: esta OLT não expõe esses campos no fluxo de autorização, só whitelist |
| Consultar sinal | PON + número da ONU (posição) — igual ao já existente pra leitura |
| Reiniciar | PON + número da ONU (posição) |
| Excluir | PON + número da ONU (posição), modal de confirmação mostrando MAC/estado antes de excluir (mesmo padrão 4840E: consulta a ONU antes de liberar o botão de confirmar) |

O histórico de ações (`log_onu_action`) já aceita `serial`/`vlan`
genéricos — pra VSOL, `vlan` fica vazio (não há VLAN por porta nesse
fluxo de autorização), `serial` carrega o MAC.

## Tratamento de erro

- **Nunca sobrescreve o modo de autenticação da PON.** Se
  `onu mac-auth add` vier com erro de "modo incompatível", devolve erro
  claro em vez de tentar mudar `onu auth-mode` sozinho — mesma cautela já
  aplicada na 4840E.
- **Excluir usa o comando corrigido** (`no onu auth onuid`, não
  `deregister`) — sem isso a ONU excluída voltaria sozinha.
- **Reiniciar não trava a sessão esperando confirmação** — este comando,
  ao contrário do `onu-reboot` da 4840E, não pede y/n (confirmado pelo
  manual, seção 17.2.12 lista `onu <onuid> ctc reset` sem menção de
  confirmação); ainda assim o teste ao vivo confirma isso antes de ir
  para produção.

## Testes

`scripts/sightops_olt_vsol_add_onu_test.py` (novo arquivo, mesmo padrão
`FakeChannel`/`FakeSSHClient` já usado em
`scripts/sightops_olt_vsol_cpe_mac_test.py` e nos testes da 4840E). Cobre:
autorização feliz (mac-auth add + leitura do onu_id via auth-info),
autorização sem registro imediato (não trava, devolve aviso), exclusão
usando o comando corrigido (`no onu auth onuid`, nunca `deregister`),
reinício, e um teste específico que prova que `build_delete_onu_vsol_command`
não monta mais `deregister onu auth onuid`.

## Validação em equipamento real

Igual ao padrão já usado na 4840E: antes de produção, testar contra uma
ONU real da OLT de Japaratinga que o usuário conhece fisicamente (autorizar
→ consultar → reiniciar → excluir → reautorizar), com autorização explícita
do usuário pra cada ação destrutiva antes de rodar.
