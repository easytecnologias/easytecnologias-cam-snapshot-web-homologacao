# Busca acelerada via NetSDK (prova de conceito) — Design

**Goal:** Provar que dá pra usar o NetSDK nativo da Dahua/Intelbras (reprodução acelerada até 16x + callback de frames) combinado com um "agente" de visão computacional local (YOLO) pra achar atividade (pessoa/veículo) num dia inteiro de gravação sem o operador saber o horário, sem baixar o vídeo bruto inteiro e sem sobrecarregar o servidor de produção.

**Escopo desta fase:** só a prova de conceito (um DVR real, um canal, uma janela curta primeiro) — não é a feature completa integrada ao SightOps. Não inclui: UI, endpoint HTTP no backend, persistência em banco, suporte a múltiplos fabricantes (Hikvision fica de fora), nem varredura de múltiplos canais em paralelo.

## Contexto (por que isso, por que agora)

O SightOps já tem uma tela de busca por IA em gravação (`IA-NVR`), mas ela baixa o vídeo em tempo real via HTTP (`RPC_Loadfile`/`loadfile.cgi`) e por isso está limitada a janelas de até 10 minutos (`NVR_AI_SEARCH_MAX_WINDOW_MIN`) — baixar e converter mais que isso, num site de link fraco, já estourou timeout numa busca real do usuário (1067s, "conversão excedeu o tempo limite").

O caso de uso real do usuário é diferente do que essa tela resolve: "aconteceu um roubo, não sei nem o dia às vezes", ou "mandaram eu olhar se pegaram fio numa semana inteira". Isso exige varrer um período longo (um dia a uma semana) numa câmera só, sem saber a hora.

Investigamos três caminhos antes de chegar neste:

1. **Baixar tudo e processar localmente** — descartado como base geral: um dia de gravação são ~23GB por câmera; mesmo processando em blocos e descartando o bruto, ainda haveria acúmulo de armazenamento e banda proporcional ao volume de clientes (rejeitado explicitamente pelo usuário: "eu teria que ter muito armazenamento, ainda não é uma solução comercial boa").
2. **Deixar o DVR marcar os eventos nativamente** (`SmartMotionDetect` + `mediaFileFind`, ou a API JSON nova `SmdDataFinder`/`analyseTaskManager`) — testado ao vivo em 2 modelos diferentes (Intelbras iNVD 5132 e NVD 7132, firmwares de 2025-03 e 2025-06): a detecção de IA embarcada só existe em 1 a 4 canais de 32 por limite de hardware do produto (confirmado pela ficha técnica oficial da Intelbras), e a API JSON nova retorna erro genérico em ambos os equipamentos — pertence a uma geração de hardware que o parque atual não tem. Não serve como base geral, só como bônus pontual em poucos canais.
3. **RTSP `cam/playback` simples** — funciona (testado ao vivo, ~7s pra ver qualquer instante), mas só reproduz em tempo real (`speed≈1.02x` medido baixando 10 minutos) — inviável pra varrer um dia inteiro sozinho.

O NetSDK nativo (`CLIENT_SetPlayBackSpeed`, até `FAST_16`) é o único caminho encontrado que acelera de verdade a reprodução sem depender de hardware de IA do DVR. É o mecanismo que os apps oficiais (Intelbras ISIC Lite, SIM Next) usam.

**Licença do SDK:** obtido pelo usuário diretamente com o suporte oficial da Intelbras (não é download anônimo/pirata). Não há EULA/termo de licença embutido no pacote nem na documentação HTTP oficial — não é um bloqueio identificado, mas fica registrado que a origem é o suporte oficial, não uma fonte pública.

**Lição de um incidente real desta investigação:** rodar captura/decodificação de vídeo concorrente dentro do container `sightops-prod-api` derrubou a performance do servidor de produção (load average 89) por alguns minutos. Por isso esta prova de conceito roda isolada (WSL Ubuntu na máquina do usuário), nunca em produção.

## Arquitetura

Um script Python isolado, rodando em WSL Ubuntu (ambiente Linux — o mesmo tipo de binário `.so` que rodaria depois num container de produção, evitando retrabalho de portar de Windows pra Linux mais tarde).

Fluxo:

```
login (CLIENT_LoginWithHighLevelSecurity)
  -> CLIENT_PlayBackByTime (abre sessao de reproducao por canal/horario, sem janela — so callback de dados)
  -> CLIENT_SetPlayBackSpeed(EM_PLAY_BACK_SPEED_FAST_16)
  -> callback recebe frames decodificados
       -> a cada N frames, roda deteccao (YOLO) e pontua (pessoa/veiculo presente + confianca)
       -> se pontuacao > limiar, registra achado (timestamp + confianca + miniatura)
  -> CLIENT_StopPlayBack
  -> logout
```

Nenhum vídeo bruto é salvo em disco — só os achados pequenos (timestamp, pontuação, miniatura do instante pontuado).

## Componentes

- **`scripts/netsdk_bridge.py`** (novo): camada fina de `ctypes` sobre `libdhnetsdk.so`. Expõe uma função:
  `escanear_canal(host, usuario, senha, canal, inicio, fim, velocidade=16, on_evento=callback) -> None`
  — a função chama `on_evento(timestamp, confianca, frame)` pra cada detecção acima do limiar, e devolve o controle quando a janela pedida termina ou dá erro.
- **`scripts/netsdk_scan_test.py`** (novo): script de teste standalone que chama `escanear_canal` contra um DVR real (parâmetros de host/canal/janela via linha de comando ou constantes no topo do arquivo, sem hardcode de senha real no arquivo — passar por variável de ambiente), usa o `ultralytics` (YOLO, já instalado) pra pontuar cada frame recebido, e imprime/salva os achados.

Nenhum outro arquivo do backend do SightOps é tocado nesta fase — é um script isolado, não uma rota nova nem integração com `nvr_search_service.py`.

## Tratamento de erro

- Login falhar (rede/credencial errada): erro claro, aborta, não tenta retry silencioso.
- Sessão de playback cair no meio (rede instável): tenta reconectar 1 vez; se falhar de novo, marca esse trecho como "não verificado" no resultado final e segue pro próximo trecho, em vez de abortar a busca inteira.
- Callback não entregar frame por mais de N segundos (trava): timeout, aborta aquele canal/janela, segue.

## Teste

Validação manual, não automatizada (é prova de conceito, não feature em produção):

1. Rodar `escanear_canal` contra um DVR real, 1 canal, janela curta (1-2h) — confirmar que `FAST_16` realmente entrega frames ~16x mais rápido que tempo real (medir tempo de parede vs. duração da janela pedida) e que o YOLO consegue pontuar os frames recebidos.
2. Só depois de (1) funcionar, expandir pra uma janela maior (o dia inteiro, 03/09/2026, canal 4, no DVR da Easy Tecnologias) e comparar o resultado com o caso real conhecido (o acidente de carro que o usuário já presenciou, mas cujo horário exato não foi revelado de propósito — serve de validação cega).

## Fora de escopo (fica pra depois, se a prova de conceito funcionar)

- Integração com o backend do SightOps (endpoint, UI, persistência).
- Rodar em produção (container isolado com limite de CPU/memória — não dentro de `sightops-prod-api`).
- Suporte a múltiplos canais em paralelo, múltiplos DVRs, outros fabricantes (Hikvision).
- Migrar de `ctypes` direto para um microsserviço separado (decisão adiada pra depois da prova de conceito confirmar que a abordagem central funciona).
