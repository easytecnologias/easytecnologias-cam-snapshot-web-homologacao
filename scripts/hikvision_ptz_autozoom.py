"""Deteccao (pessoa/carro/moto) + zoom motorizado automatico na Hikvision PTZ
+ leitura de placa via OCR, standalone (mesmo padrao do lpr_pipeline_monitor.py:
nao integrado ao SightOps ainda, o usuario nao decidiu onde isso vai morar).

Fluxo por ciclo:
  1. Camera fica na posicao HOME (visao larga).
  2. Tira snapshot largo, roda YOLO (pessoa/carro/moto).
  3. Se achou algo novo (fora do cooldown), entra em malha fechada:
     re-detecta a cada passo e corrige pan/tilt pra centralizar o alvo,
     depois avanca o zoom aos poucos verificando a cada passo -- nao existe
     calibracao fixa de graus/pixel porque o motor desse eixo tem resposta
     nao-linear (testado: passos pequenos nao moviam quase nada, um passo
     maior mudava a cena inteira). Malha fechada com re-deteccao contorna
     isso sem precisar confiar num valor de calibracao.
  4. Pessoa -> mira e enquadra a regiao da cabeca (nao faz reconhecimento
     facial, so aponta e amplia). Carro/moto -> enquadra o veiculo inteiro
     e manda pro servico de OCR (sightops-lpr-ocr, via tunel SSH local).
  5. Sempre volta pra HOME no final do ciclo, sucesso ou nao.
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import numpy as np
import requests
from requests.auth import HTTPDigestAuth
from ultralytics import YOLO

CAM_URL = "http://10.10.8.15"
CAM_USER = "admin"
CAM_PASS = "cam!perucaba@$"
OCR_URL = "http://localhost:18600/ler-placa"

HOME_AZ, HOME_EL, HOME_ZOOM = 3323, 98, 10
ZOOM_MIN, ZOOM_MAX = 10, 150

CLASSES_ALVO = {0: "pessoa", 2: "carro", 3: "moto"}
CONF_MINIMA = 0.40

CENTRO_TOLERANCIA = 0.10  # fracao do meio-frame considerada "centralizado"
PASSO_INICIAL = 40
PASSO_MAXIMO = 260
MAX_ITER_CENTRALIZAR = 5
MAX_PASSOS_ZOOM = 6
ZOOM_INCREMENTO = 22

LARGURA_ALVO_VEICULO = 0.55   # veiculo deve ocupar ~55% da largura do frame
LARGURA_ALVO_CABECA = 0.20    # regiao da cabeca deve ocupar ~20% da largura

ESPERA_MOTOR = 2.2
INTERVALO_CICLO = 3
COOLDOWN_MESMA_CLASSE = 20  # segundos antes de reprocessar o mesmo tipo de alvo


@dataclass
class Deteccao:
    classe_id: int
    classe: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def largura(self) -> float:
        return self.x2 - self.x1

    @property
    def altura(self) -> float:
        return self.y2 - self.y1


class CameraPTZ:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.auth = HTTPDigestAuth(user, password)
        self.base = base_url

    def mover(self, azimuth: int, elevation: int, zoom: int) -> None:
        azimuth = max(0, min(3600, int(round(azimuth))))
        elevation = max(0, min(900, int(round(elevation))))
        zoom = max(ZOOM_MIN, min(ZOOM_MAX, int(round(zoom))))
        body = (
            f"<PTZData><AbsoluteHigh><elevation>{elevation}</elevation>"
            f"<azimuth>{azimuth}</azimuth><absoluteZoom>{zoom}</absoluteZoom>"
            f"</AbsoluteHigh></PTZData>"
        )
        r = requests.put(
            f"{self.base}/ISAPI/PTZCtrl/channels/1/absolute",
            auth=self.auth, data=body, timeout=10,
        )
        r.raise_for_status()

    def snapshot(self) -> bytes:
        r = requests.get(
            f"{self.base}/ISAPI/Streaming/channels/101/picture",
            auth=self.auth, timeout=10,
        )
        r.raise_for_status()
        return r.content

    def ir_para_home(self) -> None:
        self.mover(HOME_AZ, HOME_EL, HOME_ZOOM)
        time.sleep(ESPERA_MOTOR)


def _decode(conteudo: bytes) -> np.ndarray:
    import cv2
    arr = np.frombuffer(conteudo, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def detectar(modelo: YOLO, frame_bgr: np.ndarray) -> list[Deteccao]:
    res = modelo.predict(frame_bgr, classes=list(CLASSES_ALVO), conf=CONF_MINIMA, verbose=False)[0]
    saida = []
    for b in res.boxes:
        cid = int(b.cls[0].item())
        x1, y1, x2, y2 = b.xyxy[0].tolist()
        saida.append(Deteccao(cid, CLASSES_ALVO[cid], float(b.conf[0].item()), x1, y1, x2, y2))
    return saida


def _melhor_deteccao(dets: list[Deteccao], classe_id: int | None = None) -> Deteccao | None:
    candidatos = [d for d in dets if classe_id is None or d.classe_id == classe_id]
    if not candidatos:
        return None
    return max(candidatos, key=lambda d: d.conf * d.largura * d.altura)


def _corrigir_para_centro(
    cam: CameraPTZ, modelo: YOLO, classe_id: int,
    az: int, el: int, zoom: int, largura_frame: int, altura_frame: int,
    alvo_cx_frac: float = 0.5, alvo_cy_frac: float = 0.5,
) -> tuple[int, int, Deteccao | None]:
    """Ajusta az/el iterativamente re-detectando a cada passo, sem confiar
    numa constante grau/pixel fixa (o eixo de azimute tem resposta nao-linear
    nesta camera). Retorna (az, el, ultima_deteccao)."""
    passo = PASSO_INICIAL
    ultima_det: Deteccao | None = None
    ultimo_erro: float | None = None

    for _ in range(MAX_ITER_CENTRALIZAR):
        cam.mover(az, el, zoom)
        time.sleep(ESPERA_MOTOR)
        frame = _decode(cam.snapshot())
        if frame is None:
            break
        dets = detectar(modelo, frame)
        det = _melhor_deteccao(dets, classe_id)
        if det is None:
            break
        ultima_det = det

        dx_frac = (det.cx / largura_frame) - alvo_cx_frac
        dy_frac = (det.cy / altura_frame) - alvo_cy_frac
        erro = abs(dx_frac) + abs(dy_frac)

        if abs(dx_frac) < CENTRO_TOLERANCIA and abs(dy_frac) < CENTRO_TOLERANCIA:
            break

        if ultimo_erro is not None and erro >= ultimo_erro:
            # o passo nao ajudou (ou piorou) -- aumenta a magnitude e inverte
            # o sinal do eixo que nao melhorou, contornando a nao-linearidade
            passo = min(passo * 2, PASSO_MAXIMO)
        ultimo_erro = erro

        az = az - int(np.sign(dx_frac) * passo) if dx_frac != 0 else az
        el = el + int(np.sign(dy_frac) * passo) if dy_frac != 0 else el
        az = max(0, min(3600, az))
        el = max(0, min(900, el))

    return az, el, ultima_det


def _avancar_zoom(
    cam: CameraPTZ, modelo: YOLO, classe_id: int,
    az: int, el: int, zoom: int, largura_frame: int, altura_frame: int,
    largura_alvo_frac: float, foco_cabeca: bool,
) -> tuple[bytes | None, Deteccao | None]:
    ultimo_frame_bytes: bytes | None = None
    ultima_det: Deteccao | None = None

    for _ in range(MAX_PASSOS_ZOOM):
        az, el, det = _corrigir_para_centro(
            cam, modelo, classe_id, az, el, zoom, largura_frame, altura_frame,
        )
        if det is None:
            return ultimo_frame_bytes, ultima_det

        ultima_det = det
        conteudo = cam.snapshot()
        ultimo_frame_bytes = conteudo

        frac_atual = det.largura / largura_frame
        if foco_cabeca:
            # mira a partir daqui na regiao da cabeca (top ~22% da caixa da pessoa)
            altura_cabeca = det.altura * 0.22
            alvo_cy_frac = (det.y1 + altura_cabeca / 2) / altura_frame
            frac_atual = altura_cabeca / altura_frame
        else:
            alvo_cy_frac = 0.5

        if frac_atual >= largura_alvo_frac or zoom >= ZOOM_MAX:
            break

        zoom = min(zoom + ZOOM_INCREMENTO, ZOOM_MAX)
        if foco_cabeca:
            az, el, det2 = _corrigir_para_centro(
                cam, modelo, classe_id, az, el, zoom, largura_frame, altura_frame,
                alvo_cy_frac=alvo_cy_frac,
            )
        else:
            az, el, det2 = _corrigir_para_centro(
                cam, modelo, classe_id, az, el, zoom, largura_frame, altura_frame,
            )
        if det2 is not None:
            ultima_det = det2

    return ultimo_frame_bytes, ultima_det


def ler_placa(conteudo_imagem: bytes) -> dict:
    r = requests.post(
        OCR_URL, files={"imagem": ("zoom.jpg", conteudo_imagem, "image/jpeg")}, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def processar_alvo(cam: CameraPTZ, modelo: YOLO, det: Deteccao, largura_frame: int, altura_frame: int) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] alvo: {det.classe} conf={det.conf:.2f} bbox=({det.x1:.0f},{det.y1:.0f},{det.x2:.0f},{det.y2:.0f})", flush=True)

    az, el = HOME_AZ, HOME_EL
    az, el, det_centro = _corrigir_para_centro(cam, modelo, det.classe_id, az, el, HOME_ZOOM, largura_frame, altura_frame)
    if det_centro is None:
        print("  -> perdeu o alvo ao tentar centralizar, abortando este ciclo", flush=True)
        return

    if det.classe_id == 0:
        frame_bytes, det_final = _avancar_zoom(
            cam, modelo, det.classe_id, az, el, HOME_ZOOM, largura_frame, altura_frame,
            largura_alvo_frac=LARGURA_ALVO_CABECA, foco_cabeca=True,
        )
        if frame_bytes is None:
            print("  -> perdeu a pessoa durante o zoom", flush=True)
            return
        nome = f"tmp/ptz_autozoom/pessoa_{int(time.time())}.jpg"
        open(nome, "wb").write(frame_bytes)
        print(f"  -> zoom no rosto salvo em {nome}", flush=True)
    else:
        frame_bytes, det_final = _avancar_zoom(
            cam, modelo, det.classe_id, az, el, HOME_ZOOM, largura_frame, altura_frame,
            largura_alvo_frac=LARGURA_ALVO_VEICULO, foco_cabeca=False,
        )
        if frame_bytes is None:
            print("  -> perdeu o veiculo durante o zoom", flush=True)
            return
        nome = f"tmp/ptz_autozoom/{det.classe}_{int(time.time())}.jpg"
        open(nome, "wb").write(frame_bytes)
        try:
            resultado = ler_placa(frame_bytes)
        except Exception as exc:  # noqa: BLE001
            print(f"  -> erro no OCR: {exc}", flush=True)
            resultado = None
        print(f"  -> zoom salvo em {nome}, OCR: {resultado}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duracao-seg", type=int, default=180)
    args = parser.parse_args()

    import os
    os.makedirs("tmp/ptz_autozoom", exist_ok=True)

    cam = CameraPTZ(CAM_URL, CAM_USER, CAM_PASS)
    modelo = YOLO("yolo11n.pt")

    cam.ir_para_home()
    frame0 = _decode(cam.snapshot())
    altura_frame, largura_frame = frame0.shape[:2]
    print(f"[{time.strftime('%H:%M:%S')}] home ok, frame {largura_frame}x{altura_frame}. monitorando por {args.duracao_seg}s...", flush=True)

    ultimo_processado: dict[int, float] = {}
    fim = time.time() + args.duracao_seg

    while time.time() < fim:
        try:
            cam.ir_para_home()
            frame = _decode(cam.snapshot())
            dets = detectar(modelo, frame)
            agora = time.time()
            dets = [d for d in dets if agora - ultimo_processado.get(d.classe_id, 0) > COOLDOWN_MESMA_CLASSE]

            if dets:
                alvo = max(dets, key=lambda d: d.conf * d.largura * d.altura)
                ultimo_processado[alvo.classe_id] = agora
                processar_alvo(cam, modelo, alvo, largura_frame, altura_frame)
                cam.ir_para_home()
            else:
                print(f"[{time.strftime('%H:%M:%S')}] nada na pista", flush=True)
        except Exception as exc:  # noqa: BLE001 -- script de teste, loga e segue
            print(f"[{time.strftime('%H:%M:%S')}] erro no ciclo: {exc}", flush=True)
            try:
                cam.ir_para_home()
            except Exception:
                pass

        time.sleep(INTERVALO_CICLO)

    cam.ir_para_home()
    print("fim, camera de volta pra home", flush=True)


if __name__ == "__main__":
    main()
