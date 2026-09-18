"""Varre uma janela de gravacao acelerada (NetSDK) e pontua frames com YOLO.

O NetSDK entrega o stream BRUTO codificado via callback (nao pixel
decodificado) -- ver docs/superpowers/plans/netsdk-header-excerpts.md.
Esse script canaliza esses bytes pra um processo ffmpeg via pipe, que
decodifica e reescala pra um tamanho fixo, e le os frames prontos do
stdout do ffmpeg numa thread separada.

Uso: mesmas variaveis do netsdk_playback_speed_test.py.
"""
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import numpy as np
from ultralytics import YOLO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import netsdk_bridge

LARGURA, ALTURA = 640, 360
TAMANHO_FRAME = LARGURA * ALTURA * 3  # bgr24
FPS_SAIDA = 2  # bate com o "fps=2" do filtro do ffmpeg abaixo

CLASSES_INTERESSE = {0: "pessoa", 2: "carro", 3: "moto", 5: "onibus", 7: "caminhao"}
LIMIAR_CONFIANCA = 0.4

host = os.environ["NETSDK_HOST"]
porta = int(os.environ.get("NETSDK_PORT", "37777"))
usuario = os.environ["NETSDK_USER"]
senha = os.environ["NETSDK_PASS"]
canal = int(os.environ.get("NETSDK_CHANNEL", "1")) - 1  # env var e 1-based (como o usuario ve no DVR); NetSDK e 0-based
inicio = datetime.strptime(os.environ["NETSDK_START"], "%Y-%m-%d %H:%M:%S")
fim = datetime.strptime(os.environ["NETSDK_END"], "%Y-%m-%d %H:%M:%S")

modelo = YOLO("yolo11n.pt")  # baixa automaticamente na primeira execucao
achados = []
estado = {"bytes_enviados": 0, "frames_lidos": 0, "encerrar": False}

ffmpeg_proc = subprocess.Popen(
    [
        "ffmpeg", "-loglevel", "error",
        "-i", "pipe:0",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-vf", f"fps={FPS_SAIDA},scale={LARGURA}:{ALTURA}",
        "pipe:1",
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
)


def ler_frames():
    tempo_inicio_janela = inicio
    while not estado["encerrar"]:
        buf = ffmpeg_proc.stdout.read(TAMANHO_FRAME)
        if len(buf) < TAMANHO_FRAME:
            if ffmpeg_proc.poll() is not None:
                break
            continue
        estado["frames_lidos"] += 1
        # Aproximacao: assume que o "fps=2" do ffmpeg decima uniformemente
        # em cima do tempo real do stream original (PTS do H.264/H.265),
        # nao do tempo de parede da varredura -- vale enquanto a camera
        # marca frame rate correto no stream. Nao tratar como exato ao
        # segundo sem conferir contra o instante real (ex.: RTSP direto).
        horario_real = tempo_inicio_janela + timedelta(seconds=(estado["frames_lidos"] - 1) / FPS_SAIDA)
        img = np.frombuffer(buf, dtype=np.uint8).reshape((ALTURA, LARGURA, 3))
        resultado = modelo.predict(img, verbose=False)[0]
        for box in resultado.boxes:
            classe_id = int(box.cls[0])
            confianca = float(box.conf[0])
            if classe_id in CLASSES_INTERESSE and confianca >= LIMIAR_CONFIANCA:
                achados.append({
                    "frame_num": estado["frames_lidos"],
                    "horario": horario_real.strftime("%Y-%m-%d %H:%M:%S"),
                    "classe": CLASSES_INTERESSE[classe_id],
                    "confianca": round(confianca, 2),
                })
                print(f"  achado: {horario_real:%H:%M:%S} (frame {estado['frames_lidos']}) -> {CLASSES_INTERESSE[classe_id]} ({confianca:.2f})")
        if estado["frames_lidos"] % 50 == 0:
            print(f"  {estado['frames_lidos']} frames processados")


def on_bytes(dados: bytes) -> None:
    estado["bytes_enviados"] += len(dados)
    try:
        ffmpeg_proc.stdin.write(dados)
    except BrokenPipeError:
        pass


thread_leitura = threading.Thread(target=ler_frames, daemon=True)
thread_leitura.start()

lib = netsdk_bridge.carregar_biblioteca()
netsdk_bridge.inicializar(lib)
handle_login = netsdk_bridge.login(lib, host, porta, usuario, senha)

t0 = time.time()
handle_play = netsdk_bridge.abrir_playback(lib, handle_login, canal, inicio, fim, on_bytes)
netsdk_bridge.set_velocidade(lib, handle_play, 4)  # FAST_16

duracao_pedida = (fim - inicio).total_seconds()
print(f"janela pedida: {duracao_pedida:.0f}s, esperando terminar (por inatividade)...")

TETO_ABSOLUTO_SEG = 20 * 60
OCIOSO_SEG = 6
ultimo_total = -1
ultima_mudanca = time.time()
while True:
    time.sleep(1)
    agora = time.time()
    if estado["bytes_enviados"] != ultimo_total:
        ultimo_total = estado["bytes_enviados"]
        ultima_mudanca = agora
    if agora - ultima_mudanca > OCIOSO_SEG:
        break
    if agora - t0 > TETO_ABSOLUTO_SEG:
        print("teto absoluto de espera atingido")
        break

tempo_real = time.time() - t0
netsdk_bridge.fechar_playback(lib, handle_play)
netsdk_bridge.logout(lib, handle_login)

try:
    ffmpeg_proc.stdin.close()
except BrokenPipeError:
    pass
time.sleep(2)  # da tempo do ffmpeg esvaziar o buffer final
estado["encerrar"] = True
ffmpeg_proc.terminate()

print(f"\ntempo de parede: {tempo_real:.1f}s (janela pedida: {duracao_pedida:.0f}s, velocidade {duracao_pedida/tempo_real:.1f}x)")
print(f"frames processados pelo YOLO: {estado['frames_lidos']}")
print(f"total de achados: {len(achados)}")
for a in sorted(achados, key=lambda x: -x["confianca"])[:20]:
    print(a)
