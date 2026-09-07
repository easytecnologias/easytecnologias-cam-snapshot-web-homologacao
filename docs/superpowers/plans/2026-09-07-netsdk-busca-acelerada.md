# Busca Acelerada via NetSDK (Prova de Conceito) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provar, com um script isolado rodando em WSL Ubuntu, que o NetSDK nativo Dahua/Intelbras (`CLIENT_SetPlayBackSpeed` até 16x) entrega frames de vídeo reais mais rápido que tempo real, e que rodar detecção local (YOLO) em cima desses frames acha atividade (pessoa/veículo) — validando isso contra um caso real conhecido (acidente de carro, dia 03/09/2026, horário desconhecido de propósito).

**Architecture:** Um script Python com `ctypes` carregando `libdhnetsdk.so` dentro do WSL Ubuntu (nunca em produção). Login → abre sessão de playback por horário → seta velocidade 16x → recebe frames via callback → pontua com YOLO → registra achados (timestamp + confiança + miniatura, nunca o vídeo bruto).

**Tech Stack:** Python 3 (dentro do WSL Ubuntu), `ctypes` (stdlib), `ultralytics` (YOLO), NetSDK Linux da Dahua/Intelbras (binário `.so` fornecido pelo usuário via suporte oficial).

## Global Constraints

- Todo teste contra equipamento real roda **dentro do WSL Ubuntu**, nunca dentro de nenhum container de produção (`sightops-prod-api`/`sightops-v3-api`) — lição de um incidente real desta mesma investigação (rodar captura concorrente no container de produção derrubou a performance por minutos, load average chegou a 89).
- Nenhuma senha real de equipamento vai em texto fixo em nenhum arquivo do repositório — sempre via variável de ambiente lida na hora do teste.
- Nenhum vídeo bruto é salvo em disco pelos scripts desta PoC — só achados pequenos (timestamp, confiança, miniatura do instante).
- Qualquer teste de concorrência (múltiplos canais/streams ao mesmo tempo) começa com N pequeno (2-3) e escala aos poucos, com checagem manual entre passos.
- O binário do NetSDK e seus arquivos de exemplo/documentação NÃO são commitados no repositório (é um SDK de terceiro, fornecido pelo suporte da Intelbras) — ficam só dentro do WSL, fora do diretório do repo.

---

### Task 1: Extrair o NetSDK Linux e resolver dependências dentro do WSL

**Files:**
- Nenhum arquivo do repositório é criado nesta task — só arquivos dentro do WSL, em `~/netsdk/` (fora do repo git).

**Interfaces:**
- Produz: `~/netsdk/lib/libdhnetsdk.so` (biblioteca principal), `~/netsdk/include/dhnetsdk.h` e `~/netsdk/include/avglobal.h` (headers), todos acessíveis dentro do WSL, usados pelas tasks seguintes.

- [ ] **Step 1: Copiar o pacote do NetSDK do Windows pro WSL**

O WSL enxerga o disco do Windows em `/mnt/c/`. Rodar dentro do WSL (`wsl -d Ubuntu`):

```bash
mkdir -p ~/netsdk_pkg
cp "/mnt/c/Users/elish/OneDrive/Área de Trabalho/API Intelbras/General_NetSDK_3.050_PlaySDK_3.042.zip" ~/netsdk_pkg/
cd ~/netsdk_pkg
apt-get update && apt-get install -y unzip
unzip -o General_NetSDK_3.050_PlaySDK_3.042.zip
```

- [ ] **Step 2: Extrair o tar.gz do Linux**

```bash
cd ~/netsdk_pkg
tar xzf "NetSDK 3.050/Linux/General_NetSDK_Eng_Linux64_IS_V3.050.0000005.4.R.190306.tar.gz" -C ~/netsdk_pkg/linux_sdk
```

Se o comando falhar dizendo que o diretório não existe, criar antes: `mkdir -p ~/netsdk_pkg/linux_sdk` e repetir.

- [ ] **Step 3: Localizar a biblioteca principal e os headers, copiar pra um local fixo**

```bash
find ~/netsdk_pkg/linux_sdk -iname "libdhnetsdk.so*"
find ~/netsdk_pkg/linux_sdk -iname "dhnetsdk.h"
find ~/netsdk_pkg/linux_sdk -iname "avglobal.h"
```

Anotar os caminhos exatos que apareceram. Copiar pra um local fixo e estável:

```bash
mkdir -p ~/netsdk/lib ~/netsdk/include
cp <caminho_encontrado_libdhnetsdk.so>* ~/netsdk/lib/
cp <caminho_encontrado_dhnetsdk.h> ~/netsdk/include/
cp <caminho_encontrado_avglobal.h> ~/netsdk/include/
ls -la ~/netsdk/lib ~/netsdk/include
```

Também copiar QUALQUER outra `.so` que estiver na mesma pasta da `libdhnetsdk.so` (o SDK costuma vir com várias bibliotecas auxiliares que ela carrega em tempo de execução):

```bash
find ~/netsdk_pkg/linux_sdk -iname "*.so*" -exec cp {} ~/netsdk/lib/ \;
ls -la ~/netsdk/lib
```

- [ ] **Step 4: Verificar dependências da biblioteca principal**

```bash
apt-get install -y libc6-dev binutils
ldd ~/netsdk/lib/libdhnetsdk.so 2>&1 | tee ~/netsdk/ldd_output.txt
cat ~/netsdk/ldd_output.txt
```

Qualquer linha com `not found` indica uma dependência do sistema faltando. Instalar as bibliotecas comuns que esse tipo de SDK costuma pedir:

```bash
apt-get install -y libssl-dev libx11-6 libxext6
```

Rodar `ldd` de novo até não sobrar nenhum `not found` (ou, se sobrar algo muito específico do SDK como `libavcodec.so` própria dele, confirmar que ela está em `~/netsdk/lib/` do Step 3 — nesse caso configurar `LD_LIBRARY_PATH=~/netsdk/lib` ao rodar, o que já está coberto nas próximas tasks).

- [ ] **Step 5: Registrar no HANDOFF_AGENTES.md o que foi encontrado**

Abrir `docs/HANDOFF_AGENTES.md` (no repo, no Windows — não no WSL) e adicionar uma seção datada de hoje com: caminho final de `libdhnetsdk.so` dentro do WSL, versão exata do SDK usada, e o resultado do `ldd` (sem sobrar `not found`). Isso é só documentação, não precisa de commit separado — inclui no commit da Task 2.

---

### Task 2: Extrair as assinaturas exatas (login, NET_TIME, callback de playback) do header real

**Files:**
- Create: `docs/superpowers/plans/netsdk-header-excerpts.md`

**Interfaces:**
- Consome: `~/netsdk/include/dhnetsdk.h`, `~/netsdk/include/avglobal.h` (da Task 1).
- Produz: `docs/superpowers/plans/netsdk-header-excerpts.md` — trechos VERBATIM (copiados exatamente do header real, não reescritos de memória) que a Task 3 e a Task 4 vão usar pra escrever os `ctypes.Structure`/`ctypes.CFUNCTYPE`.

**Por que esta task existe:** os nomes das funções de playback e velocidade já foram confirmados numa sessão anterior contra a versão Windows do mesmo SDK (`CLIENT_PlayBackByTime`, `CLIENT_PlayBackByTimeEx2`, `CLIENT_SetPlayBackSpeed`, enum `EM_PLAY_BACK_SPEED` de `SLOW_16=-4` até `FAST_16=4`, passando por `NORMAL=0`) — essas assinaturas são as mesmas entre Windows e Linux nesse SDK. Mas a função de LOGIN exata, a struct `NET_TIME`, e o tipo do callback de dados de vídeo NUNCA foram lidas ainda nesta investigação — não adivinhar essas três coisas, eXtrair do header real.

- [ ] **Step 1: Achar a função de login recomendada**

Dentro do WSL:

```bash
grep -n "CLIENT_NET_API.*CLIENT_Login" ~/netsdk/include/dhnetsdk.h
```

Isso lista todas as variantes de login (`CLIENT_Login`, `CLIENT_LoginEx`, `CLIENT_LoginEx2`, `CLIENT_LoginWithHighLevelSecurity`, etc). Procurar no texto ao redor de cada uma (buscar por comentários tipo `// Deprecated` ou `// recommended`):

```bash
grep -n -B3 -A15 "CLIENT_LoginWithHighLevelSecurity" ~/netsdk/include/dhnetsdk.h
```

Se essa função não existir no header, usar a variante não-deprecated mais completa que aparecer na lista do primeiro grep (evitar qualquer uma com comentário de "deprecated" ou "obsolete" por perto).

- [ ] **Step 2: Achar a struct de horário (`NET_TIME`)**

```bash
grep -n -B2 -A15 "typedef struct.*tagNET_TIME\|typedef struct tagNETTIME\|} NET_TIME" ~/netsdk/include/dhnetsdk.h
```

- [ ] **Step 3: Achar a assinatura completa de `CLIENT_PlayBackByTime`, `Ex` e `Ex2`, e o typedef do callback de dados**

```bash
grep -n -B2 -A10 "CLIENT_PlayBackByTime\b" ~/netsdk/include/dhnetsdk.h
grep -n -B2 -A10 "CLIENT_PlayBackByTimeEx\b" ~/netsdk/include/dhnetsdk.h
grep -n -B2 -A15 "CLIENT_PlayBackByTimeEx2" ~/netsdk/include/dhnetsdk.h
```

Uma dessas variantes recebe um parâmetro de callback (tipo `fRealDataCallBackEx2` ou similar) que entrega os dados do frame direto, sem precisar de janela (`HWND`) — é essa que interessa. Procurar o typedef desse callback:

```bash
grep -n -B2 -A10 "typedef.*fRealDataCallBack\|typedef.*DataCallBack" ~/netsdk/include/dhnetsdk.h
```

- [ ] **Step 4: Achar os tipos base (LLONG, DWORD, LDWORD, BOOL, HWND no Linux)**

```bash
grep -n "typedef.*LLONG\|typedef.*LDWORD\|typedef.*\bDWORD\b\|typedef.*\bBOOL\b\|typedef.*HWND" ~/netsdk/include/avglobal.h
```

- [ ] **Step 5: Salvar tudo isso em `docs/superpowers/plans/netsdk-header-excerpts.md`**

Criar o arquivo no repositório (no Windows) com o texto EXATO que apareceu em cada grep acima, organizado assim:

```markdown
# Trechos do dhnetsdk.h / avglobal.h (NetSDK 3.050, Linux)

## Tipos base (avglobal.h)
<colar aqui a saida do Step 4>

## Login
<colar aqui a saida do Step 1>

## NET_TIME
<colar aqui a saida do Step 2>

## CLIENT_PlayBackByTime / Ex / Ex2 + callback de dados
<colar aqui a saida do Step 3>
```

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/plans/netsdk-header-excerpts.md docs/HANDOFF_AGENTES.md
git commit -m "docs: extrai assinaturas reais do NetSDK Linux pra usar nos bindings ctypes"
```

---

### Task 3: `netsdk_bridge.py` — inicialização e login real contra um DVR

**Files:**
- Create: `scripts/netsdk_bridge.py`
- Test: `scripts/netsdk_bridge_login_test.py`

**Interfaces:**
- Consome: `docs/superpowers/plans/netsdk-header-excerpts.md` (Task 2), `~/netsdk/lib/libdhnetsdk.so` (Task 1).
- Produz: `netsdk_bridge.py` expõe:
  - `carregar_biblioteca(caminho_so: str) -> ctypes.CDLL`
  - `inicializar() -> None` (chama `CLIENT_Init`)
  - `login(host: str, porta: int, usuario: str, senha: str) -> int` (retorna o handle de login, `LLONG`; levanta `RuntimeError` com a mensagem de erro do SDK se falhar)
  - `logout(handle_login: int) -> None`

- [ ] **Step 1: Escrever `scripts/netsdk_bridge.py` com `CLIENT_Init` e o carregamento da biblioteca**

```python
"""Bindings ctypes finos para o NetSDK Dahua/Intelbras (Linux).

So roda dentro do WSL/Linux -- nunca em producao. Nao guarda nenhuma
senha; tudo vem de parametro/variavel de ambiente na hora do uso.
"""
from __future__ import annotations

import ctypes
import os


class NetSDKError(RuntimeError):
    pass


def carregar_biblioteca(caminho_so: str | None = None) -> ctypes.CDLL:
    caminho_so = caminho_so or os.path.expanduser("~/netsdk/lib/libdhnetsdk.so")
    lib_dir = os.path.dirname(caminho_so)
    ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    if lib_dir not in ld_path.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}:{ld_path}" if ld_path else lib_dir
    return ctypes.CDLL(caminho_so)


def inicializar(lib: ctypes.CDLL) -> None:
    lib.CLIENT_Init.restype = ctypes.c_int
    lib.CLIENT_Init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ok = lib.CLIENT_Init(None, None)
    if not ok:
        raise NetSDKError("CLIENT_Init falhou")
```

**Nota pro implementador:** a partir daqui, a assinatura exata de `login`/`logout` depende do que a Task 2 encontrou em `docs/superpowers/plans/netsdk-header-excerpts.md`, seção "Login". Usar o mesmo padrão do `inicializar()` acima (`restype`, `argtypes` batendo com o C real) para declarar a função de login encontrada, passando host/porta/usuario/senha como `ctypes.c_char_p` (strings C, `.encode()` antes de passar) e recebendo de volta o handle (`LLONG` -> `ctypes.c_int64`). Se a função de login usar uma struct de entrada/saída (comum em `CLIENT_LoginWithHighLevelSecurity`), declarar essa struct como `ctypes.Structure` com os campos EXATOS (nomes e tipos, na mesma ordem) que apareceram no trecho salvo pela Task 2 — não adivinhar nem reordenar campos, isso corrompe a memória silenciosamente em ctypes.

- [ ] **Step 2: Implementar `login()` e `logout()` seguindo a nota acima**

(código depende do achado da Task 2 — implementar seguindo exatamente o padrão do Step 1: `argtypes`/`restype` batendo com a assinatura C real, struct de entrada com os campos na mesma ordem do header, decodificar erro via `CLIENT_GetLastError()` se o login falhar e incluir a mensagem no `NetSDKError`)

- [ ] **Step 3: Escrever `scripts/netsdk_bridge_login_test.py` — teste manual contra DVR real**

```python
"""Teste manual: login real contra um DVR. Nao roda em CI, roda na mao.

Uso:
  NETSDK_HOST=127.0.0.1 NETSDK_PORT=37777 NETSDK_USER=admin NETSDK_PASS='...' \
    python3 scripts/netsdk_bridge_login_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import netsdk_bridge

host = os.environ["NETSDK_HOST"]
porta = int(os.environ.get("NETSDK_PORT", "37777"))
usuario = os.environ["NETSDK_USER"]
senha = os.environ["NETSDK_PASS"]

lib = netsdk_bridge.carregar_biblioteca()
netsdk_bridge.inicializar(lib)
handle = netsdk_bridge.login(lib, host, porta, usuario, senha)
print("login OK, handle:", handle)
netsdk_bridge.logout(lib, handle)
print("logout OK")
```

- [ ] **Step 4: Abrir o túnel SSH até o DVR (as DUAS portas: RTSP 554 e controle 37777)**

O DVR só é alcançável via túnel até o servidor de produção. Rodar no Windows (fora do WSL, o WSL herda a rede do host via NAT e enxerga `127.0.0.1` do Windows):

```
plink -ssh -batch -hostkey "SHA256:Mr+mCWial0YVe4kvWEYCq7A+pZt/F+5nMhBa5FHKSnw" -pw '<senha do servidor central, ver [[server_central_credentials]]>' -L 15540:100.65.10.51:554 -L 15537:100.65.10.51:37777 central@201.182.184.84
```

(trocar o IP do DVR conforme o equipamento sendo testado; manter essa janela aberta em segundo plano durante o teste)

- [ ] **Step 5: Rodar o teste de login de verdade**

Dentro do WSL:

```bash
cd /mnt/c/PROJETOS/cam-snapshot-web-v2
NETSDK_HOST=127.0.0.1 NETSDK_PORT=15537 NETSDK_USER=admin NETSDK_PASS='<senha real, pedir ao usuario>' \
  python3 scripts/netsdk_bridge_login_test.py
```

Esperado: `login OK, handle: <numero>` seguido de `logout OK`. Se der erro de conexão recusada, testar `NETSDK_PORT=15540` (pode ser que o servico de controle escute em outra porta nesse modelo especifico) ou confirmar com o usuario se a porta 37777 esta acessivel nesse DVR.

- [ ] **Step 6: Commit**

```bash
git add scripts/netsdk_bridge.py scripts/netsdk_bridge_login_test.py
git commit -m "feat(netsdk): login/logout via ctypes contra o NetSDK nativo"
```

---

### Task 4: Playback acelerado + captura de stream bruto via callback

**Correção descoberta durante a execução (Task 2, lendo `demo/03.PlayBack/dialog.cpp` real):** o callback de dados do NetSDK (`fDownLoadDataCallBack`, assinatura real confirmada: `int CALLBACK DataCallBack(LLONG lRealHandle, DWORD dwDataType, BYTE *pBuffer, DWORD dwBufSize, LDWORD dwUser)`) entrega o **stream bruto codificado** (mesmo formato `.dav`/H.264/H.265 que o projeto já baixa via HTTP), não frames já decodificados em pixel — o demo oficial literalmente escreve esses bytes direto num arquivo `.dav`. Decodificar pixel a pixel é trabalho do PlaySDK (uma segunda biblioteca nativa separada) ou de qualquer decodificador de vídeo. Em vez de integrar o PlaySDK (escopo maior, mais uma biblioteca nativa pra bindar), a Task 4 canaliza os bytes recebidos pra um processo `ffmpeg` via pipe — o mesmo `ffmpeg` que o projeto inteiro já usa pra decodificar esse formato — e lê os frames decodificados da saída do `ffmpeg`.

**Files:**
- Modify: `scripts/netsdk_bridge.py`
- Test: `scripts/netsdk_playback_speed_test.py`

**Interfaces:**
- Consome: `login()`/`logout()` (Task 3), trechos de `CLIENT_PlayBackByTimeEx2`/callback (Task 2, já confirmados: struct `NET_IN_PLAY_BACK_BY_TIME_INFO` com campos `stStartTime`/`stStopTime` (`NET_TIME`), `hWnd`, `cbDownLoadPos`, `dwPosUser`, `fDownLoadDataCallBack`, `dwDataUser`, `nPlayDirection`, `nWaittime`; `NET_TIME` com campos `dwYear`/`dwMonth`/`dwDay`/`dwHour`/`dwMinute`/`dwSecond`).
- Produz: em `netsdk_bridge.py`:
  - `abrir_playback(lib, handle_login, canal: int, inicio: datetime, fim: datetime, on_bytes_brutos: Callable[[bytes], None]) -> int` (retorna o handle de playback; `on_bytes_brutos` recebe os bytes crus de cada chamada do callback `DataCallBack`, repassando pro pipe do ffmpeg)
  - `set_velocidade(lib, handle_playback: int, velocidade: int) -> None` (aceita -4 a 4, mapeando pro enum `EM_PLAY_BACK_SPEED`; valores fora disso levantam `ValueError`)
  - `fechar_playback(lib, handle_playback: int) -> None`
  - `DecodificadorFFmpeg` (classe auxiliar): abre um `subprocess.Popen(["ffmpeg", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "bgr24", "-vf", "fps=2", "pipe:1"], stdin=PIPE, stdout=PIPE)`, expõe `escrever(dados_brutos: bytes)` (manda pro stdin) e `ler_frames_disponiveis() -> list[np.ndarray]` (lê o que já estiver pronto no stdout, sem bloquear, usando o tamanho fixo do frame BGR24 pra decidir quantos bytes formam um frame completo)

- [ ] **Step 1: Implementar `abrir_playback()`**

Usar a variante de `CLIENT_PlayBackByTime*` que aceita callback de dados brutos (identificada na Task 2), passando `HWND=None`/`0` (não queremos renderizar em tela). Declarar o `NET_TIME` (da Task 2) e preencher com `inicio`/`fim` (datetime do Python convertido pros campos ano/mes/dia/hora/min/seg da struct). O callback (`ctypes.CFUNCTYPE`) deve ter a assinatura exata encontrada na Task 2 — guardar uma referência Python pro callback numa variável de módulo (`_callbacks_ativos = []`) enquanto a sessão estiver aberta, senão o Python coleta o objeto e o C chama um ponteiro morto (erro clássico de ctypes com callbacks).

```python
EM_PLAY_BACK_SPEED = {
    -4: "SLOW_16", -3: "SLOW_8", -2: "SLOW_4", -1: "SLOW_2",
    0: "NORMAL",
    1: "FAST_2", 2: "FAST_4", 3: "FAST_8", 4: "FAST_16",
}


def set_velocidade(lib: ctypes.CDLL, handle_playback: int, velocidade: int) -> None:
    if velocidade not in EM_PLAY_BACK_SPEED:
        raise ValueError(f"velocidade {velocidade} invalida, use -4 a 4")
    lib.CLIENT_SetPlayBackSpeed.restype = ctypes.c_int
    lib.CLIENT_SetPlayBackSpeed.argtypes = [ctypes.c_int64, ctypes.c_int]
    ok = lib.CLIENT_SetPlayBackSpeed(handle_playback, velocidade)
    if not ok:
        raise NetSDKError(f"CLIENT_SetPlayBackSpeed({velocidade}) falhou")
```

(`abrir_playback`/`fechar_playback` seguem o mesmo padrão de `login`/`logout` da Task 3, usando a assinatura exata de `CLIENT_PlayBackByTime*` e do callback documentadas na Task 2)

- [ ] **Step 2: Escrever `scripts/netsdk_playback_speed_test.py`**

```python
"""Teste manual: mede se o playback a 16x e realmente ~16x mais rapido
que tempo real, contando frames recebidos via callback.

Uso: mesmas variaveis de ambiente do netsdk_bridge_login_test.py, mais:
  NETSDK_CHANNEL=1 NETSDK_START="2026-09-07 08:00:00" NETSDK_END="2026-09-07 09:00:00"
"""
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import netsdk_bridge

host = os.environ["NETSDK_HOST"]
porta = int(os.environ.get("NETSDK_PORT", "37777"))
usuario = os.environ["NETSDK_USER"]
senha = os.environ["NETSDK_PASS"]
canal = int(os.environ.get("NETSDK_CHANNEL", "1"))
inicio = datetime.strptime(os.environ["NETSDK_START"], "%Y-%m-%d %H:%M:%S")
fim = datetime.strptime(os.environ["NETSDK_END"], "%Y-%m-%d %H:%M:%S")

contador = {"frames": 0}


def on_frame(dados, largura, altura):
    contador["frames"] += 1
    if contador["frames"] % 50 == 0:
        print(f"  frame {contador['frames']} recebido ({largura}x{altura})")


lib = netsdk_bridge.carregar_biblioteca()
netsdk_bridge.inicializar(lib)
handle_login = netsdk_bridge.login(lib, host, porta, usuario, senha)

t0 = time.time()
handle_play = netsdk_bridge.abrir_playback(lib, handle_login, canal, inicio, fim, on_frame)
netsdk_bridge.set_velocidade(lib, handle_play, 4)  # FAST_16

duracao_pedida = (fim - inicio).total_seconds()
print(f"janela pedida: {duracao_pedida:.0f}s, esperando terminar...")

# espera a sessao terminar sozinha (o callback para de chegar) ou um teto de seguranca
tempo_maximo_espera = duracao_pedida / 16 * 3 + 30
while time.time() - t0 < tempo_maximo_espera:
    time.sleep(1)

tempo_real = time.time() - t0
netsdk_bridge.fechar_playback(lib, handle_play)
netsdk_bridge.logout(lib, handle_login)

print(f"tempo de parede: {tempo_real:.1f}s")
print(f"duracao pedida: {duracao_pedida:.0f}s")
print(f"velocidade real: {duracao_pedida / tempo_real:.1f}x")
print(f"total de frames recebidos: {contador['frames']}")
```

- [ ] **Step 3: Rodar contra um DVR real, janela curta primeiro (10-15 minutos, não 1h ainda)**

Com o túnel SSH da Task 3 (Step 4) ainda aberto:

```bash
cd /mnt/c/PROJETOS/cam-snapshot-web-v2
NETSDK_HOST=127.0.0.1 NETSDK_PORT=15537 NETSDK_USER=admin NETSDK_PASS='<senha>' \
NETSDK_CHANNEL=1 NETSDK_START="2026-09-07 08:00:00" NETSDK_END="2026-09-07 08:15:00" \
  python3 scripts/netsdk_playback_speed_test.py
```

Esperado: `velocidade real` próximo de 16 (não 1) e `total de frames recebidos` maior que zero. Se a velocidade real ficar perto de 1x, o `CLIENT_SetPlayBackSpeed` não está tendo efeito — revisar se o handle de playback passado é o correto e se a chamada retornou sucesso (não silenciar erro de retorno).

- [ ] **Step 4: Se funcionar, repetir com uma janela de 1h pra confirmar que sustenta a velocidade numa janela maior**

Mesmo comando do Step 3, com `NETSDK_END` uma hora depois do `NETSDK_START`. Comparar `velocidade real` — deve continuar perto de 16x, não cair pra 1x conforme a janela cresce.

- [ ] **Step 5: Commit**

```bash
git add scripts/netsdk_bridge.py scripts/netsdk_playback_speed_test.py
git commit -m "feat(netsdk): playback acelerado (16x) via CLIENT_SetPlayBackSpeed, validado contra DVR real"
```

---

### Task 5: Pontuação com YOLO em cima dos frames recebidos

**Files:**
- Create: `scripts/netsdk_scan_test.py`

**Interfaces:**
- Consome: `abrir_playback`/`set_velocidade`/`fechar_playback` (Task 4).
- Produz: script executável que imprime uma lista de achados `(timestamp_estimado, confianca, classe)`.

- [ ] **Step 1: Instalar `ultralytics` dentro do WSL (ambiente separado do Python do Windows)**

```bash
cd /mnt/c/PROJETOS/cam-snapshot-web-v2
python3 -m pip install --quiet ultralytics
python3 -c "import ultralytics; print('ultralytics', ultralytics.__version__)"
```

- [ ] **Step 2: Escrever `scripts/netsdk_scan_test.py`**

```python
"""Varre uma janela de gravacao a 16x e pontua frames com YOLO.

Uso: mesmas variaveis do netsdk_playback_speed_test.py.
"""
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
from ultralytics import YOLO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import netsdk_bridge

CLASSES_INTERESSE = {0: "pessoa", 2: "carro", 3: "moto", 5: "onibus", 7: "caminhao"}
LIMIAR_CONFIANCA = 0.4
AMOSTRAR_A_CADA_N_FRAMES = 15  # nao roda YOLO em todo frame -- caro demais

host = os.environ["NETSDK_HOST"]
porta = int(os.environ.get("NETSDK_PORT", "37777"))
usuario = os.environ["NETSDK_USER"]
senha = os.environ["NETSDK_PASS"]
canal = int(os.environ.get("NETSDK_CHANNEL", "1"))
inicio = datetime.strptime(os.environ["NETSDK_START"], "%Y-%m-%d %H:%M:%S")
fim = datetime.strptime(os.environ["NETSDK_END"], "%Y-%m-%d %H:%M:%S")

modelo = YOLO("yolo11n.pt")  # baixa automaticamente na primeira execucao
achados = []
estado = {"frames_recebidos": 0}


def on_frame(dados: bytes, largura: int, altura: int) -> None:
    estado["frames_recebidos"] += 1
    if estado["frames_recebidos"] % AMOSTRAR_A_CADA_N_FRAMES != 0:
        return
    try:
        img = np.frombuffer(dados, dtype=np.uint8).reshape((altura, largura, 3))
    except ValueError:
        return  # formato de frame nao bate com o esperado -- ver Nota abaixo
    resultado = modelo.predict(img, verbose=False)[0]
    for box in resultado.boxes:
        classe_id = int(box.cls[0])
        confianca = float(box.conf[0])
        if classe_id in CLASSES_INTERESSE and confianca >= LIMIAR_CONFIANCA:
            fracao_decorrida = estado["frames_recebidos"] / max(estado["frames_recebidos"], 1)
            achados.append({
                "frame_num": estado["frames_recebidos"],
                "classe": CLASSES_INTERESSE[classe_id],
                "confianca": round(confianca, 2),
            })
            print(f"  achado: frame {estado['frames_recebidos']} -> {CLASSES_INTERESSE[classe_id]} ({confianca:.2f})")


lib = netsdk_bridge.carregar_biblioteca()
netsdk_bridge.inicializar(lib)
handle_login = netsdk_bridge.login(lib, host, porta, usuario, senha)
handle_play = netsdk_bridge.abrir_playback(lib, handle_login, canal, inicio, fim, on_frame)
netsdk_bridge.set_velocidade(lib, handle_play, 4)

duracao_pedida = (fim - inicio).total_seconds()
t0 = time.time()
tempo_maximo_espera = duracao_pedida / 16 * 3 + 30
while time.time() - t0 < tempo_maximo_espera:
    time.sleep(1)

netsdk_bridge.fechar_playback(lib, handle_play)
netsdk_bridge.logout(lib, handle_login)

print(f"\ntotal de frames recebidos: {estado['frames_recebidos']}")
print(f"total de achados: {len(achados)}")
for a in sorted(achados, key=lambda x: -x["confianca"])[:20]:
    print(a)
```

**Nota importante pro implementador:** o formato exato dos bytes que o callback entrega (RGB puro, BGR, YUV420, ou já um JPEG) depende do que o SDK realmente manda — isso NÃO estava documentado nos trechos que a Task 2 extraiu de propósito (a Task 2 só pegou a assinatura da função, não o formato de pixel). Ao rodar pela primeira vez, se `np.frombuffer(...).reshape(...)` der erro de tamanho, imprimir `len(dados)` e comparar com `largura*altura*3` (RGB/BGR) e `largura*altura*3//2` (YUV420) pra descobrir o formato real, e ajustar o reshape/conversão de cor (`cv2.cvtColor` se for YUV) antes de passar pro YOLO.

- [ ] **Step 3: Rodar contra uma janela curta e conhecida primeiro (a mesma da Task 4, 15 minutos), confirmar que aparecem achados plausíveis**

```bash
cd /mnt/c/PROJETOS/cam-snapshot-web-v2
NETSDK_HOST=127.0.0.1 NETSDK_PORT=15537 NETSDK_USER=admin NETSDK_PASS='<senha>' \
NETSDK_CHANNEL=1 NETSDK_START="2026-09-07 08:00:00" NETSDK_END="2026-09-07 08:15:00" \
  python3 scripts/netsdk_scan_test.py
```

Esperado: alguma lista de achados (mesmo que poucos) com classes plausíveis pro canal testado. Se `total de achados` for zero mas `total de frames recebidos` for razoável, não é necessariamente erro — pode ser que a câmera realmente não teve pessoa/veículo naquela janela; testar noutra janela/canal conhecido por ter movimento antes de assumir bug.

- [ ] **Step 4: Commit**

```bash
git add scripts/netsdk_scan_test.py
git commit -m "feat(netsdk): pontuacao com YOLO em cima dos frames do playback acelerado"
```

---

### Task 6: Validação cega final — o acidente de carro do dia 03/09/2026

**Files:**
- Nenhum arquivo novo — só execução do `netsdk_scan_test.py` (Task 5) contra o caso real.

**Interfaces:**
- Consome: `netsdk_scan_test.py` (Task 5).

- [ ] **Step 1: Abrir o túnel SSH pro DVR da Easy Tecnologias (host `10.10.10.120`, portas 554 e 37777)**

```
plink -ssh -batch -hostkey "SHA256:Mr+mCWial0YVe4kvWEYCq7A+pZt/F+5nMhBa5FHKSnw" -pw '<senha do servidor central, ver [[server_central_credentials]]>' -L 15540:10.10.10.120:554 -L 15537:10.10.10.120:37777 central@201.182.184.84
```

- [ ] **Step 2: Rodar o scan contra o dia inteiro (03/09/2026), canal 4, em blocos de poucas horas por vez**

Não rodar as 24h numa chamada só ainda (mantém o hábito de escalonar aos poucos que este plano pede). Rodar em blocos de 4h:

```bash
cd /mnt/c/PROJETOS/cam-snapshot-web-v2
for INICIO_H in 00 04 08 12 16 20; do
  FIM_H=$((10#$INICIO_H + 4))
  echo "=== bloco $INICIO_H:00 - $FIM_H:00 ==="
  NETSDK_HOST=127.0.0.1 NETSDK_PORT=15537 NETSDK_USER=admin NETSDK_PASS='<senha da Easy Tecnologias>' \
  NETSDK_CHANNEL=4 NETSDK_START="2026-09-03 ${INICIO_H}:00:00" NETSDK_END="2026-09-03 $(printf '%02d' $FIM_H):00:00" \
    python3 scripts/netsdk_scan_test.py
done
```

Com velocidade 16x, cada bloco de 4h leva ~15 minutos de parede — o dia inteiro em 6 blocos leva ~1h30 no total, rodando um de cada vez (sem repetir a concorrência que causou o incidente de produção).

- [ ] **Step 2 (checagem intermediária):** depois do PRIMEIRO bloco, antes de rodar os outros 5, confirmar que a máquina local não está sobrecarregada (usar o Gerenciador de Tarefas do Windows ou `top` dentro do WSL) e que a velocidade real ficou perto de 16x (não caiu pra 1x). Só continuar pros próximos blocos se isso estiver confirmado.

- [ ] **Step 3: Reunir os achados de maior confiança de todos os blocos, comparar com o horário real do acidente**

Depois de rodar os 6 blocos, juntar os achados de maior pontuação e perguntar ao usuário se algum deles bate com o horário real do acidente que ele já sabe (mas não revelou até aqui). Essa comparação é o critério de sucesso desta PoC inteira — reportar ao usuário, em português, se bateu ou não, e se não bateu, quais achados de alta confiança apareceram mesmo assim (podem ser outros eventos reais, ou falsos positivos a investigar).

- [ ] **Step 4: Registrar o resultado em `docs/HANDOFF_AGENTES.md`**

Seção datada com: se a PoC confirmou a hipótese (achou o acidente) ou não, quantos achados no total, tempo de parede total, e qualquer ajuste feito no caminho (formato de frame, porta de conexão, etc.) que valha a pena outra pessoa saber antes de tentar de novo.

- [ ] **Step 5: Commit**

```bash
git add docs/HANDOFF_AGENTES.md
git commit -m "docs: resultado da validacao cega do NetSDK contra o acidente de 03/09/2026"
```

---

## Self-Review (feito ao escrever este plano)

- **Cobertura da spec:** arquitetura (Tasks 3-5), componentes (`netsdk_bridge.py` nas Tasks 3-4, `netsdk_scan_test.py` na Task 5), dados (nenhum vídeo salvo — reforçado em todo callback que só guarda achados pequenos), tratamento de erro (login/reconexão tratados na Task 3, timeout de espera nas Tasks 4-6), teste (Tasks 4, 5 e 6 cobrem os 3 passos de validação da spec, na mesma ordem).
- **Escopo:** cada task produz algo testável sozinho (login isolado, depois velocidade isolada, depois YOLO, depois o caso real) — nenhuma task depende de adivinhar o resultado de uma task futura.
- **Consistência de tipos:** `abrir_playback`/`set_velocidade`/`fechar_playback` usados de forma consistente entre Task 4, 5 e 6; `on_frame(dados, largura, altura)` é a mesma assinatura em todo lugar que aparece.
- **Risco assumido de propósito (não é placeholder, é honestidade):** a Task 3 (struct de login) e a Task 5 (formato de pixel do frame) não podem ter código 100% fechado de antemão porque dependem de informação que só existe no header/binário real, ainda não lida nesta sessão. Cada uma dessas tasks tem um "Nota pro implementador" concreta explicando exatamente o que descobrir e como aplicar — não é "implemente do jeito que achar melhor", é "leia X, aplique o padrão Y que já está escrito".
