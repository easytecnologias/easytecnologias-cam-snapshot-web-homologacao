"""Pipeline de teste do LPR: liga deteccao de movimento + zoom motorizado
+ OCR de placa, tudo standalone (nao integrado ao SightOps ainda -- o
usuario ainda nao decidiu onde esse recurso vai morar na arquitetura).

Fluxo por ciclo:
  1. Tira snapshot da visao larga da camera.
  2. Compara com o snapshot anterior (diferenca de pixel simples, sem
     modelo nenhum -- mais barato que rodar YOLO de novo nesse servidor
     ja apertado de RAM).
  3. Se mudou significativamente, assume que pode ter um veiculo:
     da zoom, espera o motor assentar, tira snapshot, manda pro
     servico de OCR (sightops-lpr-ocr, ja rodando no mesmo host),
     imprime o resultado, e volta pro grande angular.

Os valores de zoom/focus do "aponta pra placa" (ZOOM_ALVO/FOCUS_ALVO)
sao um chute inicial -- calibrar olhando o snapshot zoomado de verdade
e ajustando ate a placa ficar legivel e enquadrada.
"""
from __future__ import annotations

import io
import sys
import time

import numpy as np
import requests
from PIL import Image
from requests.auth import HTTPDigestAuth

from intelbras_zoom_rpc import CameraZoomRPC

CAM_URL = "http://10.10.8.1"
CAM_USER = "admin"
CAM_PASS = "cam!perucaba@$"
OCR_URL = "http://localhost:18600/ler-placa"

ZOOM_ALVO, FOCUS_ALVO = 0.6, 0.6
ZOOM_LARGO, FOCUS_LARGO = 0.0, 0.5

LIMIAR_PIXEL = 15
PCT_MIN_MUDANCA = 0.02
ESPERA_MOTOR_SEG = 3
INTERVALO_CICLO_SEG = 2

auth = HTTPDigestAuth(CAM_USER, CAM_PASS)
snapshot_url = f"{CAM_URL}/cgi-bin/snapshot.cgi"


def pegar_snapshot() -> bytes:
    r = requests.get(snapshot_url, auth=auth, timeout=10)
    r.raise_for_status()
    return r.content


def para_cinza(conteudo: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(conteudo)).convert("L"))


def mudou_significativamente(anterior: np.ndarray, atual: np.ndarray) -> bool:
    if anterior.shape != atual.shape:
        return False
    diff = np.abs(anterior.astype(int) - atual.astype(int))
    fracao_mudada = (diff > LIMIAR_PIXEL).sum() / diff.size
    return fracao_mudada > PCT_MIN_MUDANCA


def ler_placa(conteudo_imagem: bytes) -> dict:
    r = requests.post(
        OCR_URL, files={"imagem": ("snap.jpg", conteudo_imagem, "image/jpeg")}, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def main() -> None:
    cam = CameraZoomRPC(CAM_URL, CAM_USER, CAM_PASS)
    cam.login()
    print(f"[{time.strftime('%H:%M:%S')}] Conectado a camera, monitorando (Ctrl+C pra parar)...", flush=True)

    anterior_cinza: np.ndarray | None = None

    while True:
        try:
            bruto = pegar_snapshot()
            atual_cinza = para_cinza(bruto)

            if anterior_cinza is not None and mudou_significativamente(anterior_cinza, atual_cinza):
                print(f"[{time.strftime('%H:%M:%S')}] Movimento detectado -- dando zoom na placa...", flush=True)
                cam.ajustar(zoom=ZOOM_ALVO, focus=FOCUS_ALVO)
                time.sleep(ESPERA_MOTOR_SEG)

                zoomado = pegar_snapshot()
                resultado = ler_placa(zoomado)
                print(f"  -> OCR: {resultado}", flush=True)

                cam.ajustar(zoom=ZOOM_LARGO, focus=FOCUS_LARGO)
                time.sleep(2)
                anterior_cinza = None  # forca reler a referencia no proximo ciclo
            else:
                anterior_cinza = atual_cinza
        except Exception as exc:  # noqa: BLE001 -- script de teste, so loga e segue
            print(f"[{time.strftime('%H:%M:%S')}] erro no ciclo: {exc}", flush=True)

        time.sleep(INTERVALO_CICLO_SEG)


if __name__ == "__main__":
    main()
