"""Teste manual: mede velocidade real com CLIENT_MultiPlayBack (stream leve)
em vez de CLIENT_PlayBackByTimeEx2 (stream principal).

Uso: mesmas variaveis do netsdk_playback_speed_test.py.
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
canal = int(os.environ.get("NETSDK_CHANNEL", "1")) - 1
inicio = datetime.strptime(os.environ["NETSDK_START"], "%Y-%m-%d %H:%M:%S")
fim = datetime.strptime(os.environ["NETSDK_END"], "%Y-%m-%d %H:%M:%S")

contador = {"bytes": 0, "chamadas": 0}


def on_bytes(dados: bytes) -> None:
    contador["bytes"] += len(dados)
    contador["chamadas"] += 1


lib = netsdk_bridge.carregar_biblioteca()
netsdk_bridge.inicializar(lib)
handle_login = netsdk_bridge.login(lib, host, porta, usuario, senha)

t0 = time.time()
handle_play = netsdk_bridge.abrir_playback_leve(lib, handle_login, canal, inicio, fim, on_bytes, resolucao=b"CIF", bitrate_kbps=512)
netsdk_bridge.set_velocidade(lib, handle_play, 4)  # FAST_16

duracao_pedida = (fim - inicio).total_seconds()
print(f"janela pedida: {duracao_pedida:.0f}s, esperando terminar (por inatividade)...")

TETO_ABSOLUTO_SEG = 10 * 60
OCIOSO_SEG = 6
ultimo_total = -1
ultima_mudanca = time.time()
while True:
    time.sleep(1)
    agora = time.time()
    if contador["bytes"] != ultimo_total:
        ultimo_total = contador["bytes"]
        ultima_mudanca = agora
    if agora - ultima_mudanca > OCIOSO_SEG:
        break
    if agora - t0 > TETO_ABSOLUTO_SEG:
        print("teto absoluto de espera atingido")
        break

tempo_real = time.time() - t0
netsdk_bridge.fechar_playback(lib, handle_play)
netsdk_bridge.logout(lib, handle_login)

print(f"tempo de parede: {tempo_real:.1f}s")
print(f"duracao pedida: {duracao_pedida:.0f}s")
print(f"velocidade real: {duracao_pedida / tempo_real:.1f}x")
print(f"total de bytes recebidos: {contador['bytes']/1024:.0f} KB em {contador['chamadas']} chamadas")
print(f"taxa: {contador['bytes']/1024/tempo_real:.0f} KB/s")
