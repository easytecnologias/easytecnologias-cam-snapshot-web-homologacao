"""Deteccao continua (via RTSP ao vivo) na zona da estrada de terra --
so tenta OCR quando o veiculo ja esta grande o suficiente no frame.

Historico de decisoes (ver docs/HANDOFF_AGENTES.md):
  - Reagir a 1 snapshot isolado depois de um evento VMD e fragil demais
    (3 veiculos reais perdidos seguidos) -> trocado por processar o RTSP
    ao vivo frame a frame na zona da estrada de terra (prioridade
    combinada com o usuario, ja que 1 camera nao cobre as 2 zonas com
    atencao total ao mesmo tempo).
  - Zoom dirigido (parar o RTSP, centralizar e ampliar no veiculo antes
    de tentar OCR) foi tentado e abandonado: 26/26 tentativas reais
    perderam o veiculo, mesmo insistindo em centralizar (margem 12%)
    antes de cada passo de zoom. O motor de azimute desse eixo tem
    resposta nao-linear e o zoom optico amplia em torno do CENTRO DO
    FRAME, nao do veiculo -- qualquer erro residual de centralizacao e
    amplificado a cada incremento de zoom. Mais simples e confiavel so
    esperar: o video e continuo, o veiculo reaparece maior nos proximos
    frames conforme se aproxima, sem precisar mexer no motor.

A pista pavimentada (zoom optico, longe) fica em segundo plano: a cada
CICLO_ESTRADA_SEG a gente fecha o RTSP, da uma espiada rapida (3
snapshots espacados) num preset FIXO ja calibrado manualmente (isso sim
funciona bem -- ja rendeu 2 leituras de placa com confianca >0.98), e
volta pro home/RTSP.
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np
import requests
from requests.auth import HTTPDigestAuth
from ultralytics import YOLO

CAM_URL = "http://10.10.8.15"
CAM_USER = "admin"
CAM_PASS = "cam!perucaba@$"
RTSP_URL = f"rtsp://{CAM_USER}:{CAM_PASS}@10.10.8.15:554/Streaming/Channels/101"
OCR_URL = "http://localhost:18600/ler-placa"

HOME = (3323, 98, 10)
ZONA_TERRA_CROP = (0, 300, 650, 950)  # x1,y1,x2,y2 em 1920x1080
ZONA_PISTA_PTZ = (3473, 58, 70)

CLASSES_ALVO = {0: "pessoa", 2: "carro", 3: "moto"}
CONF_MINIMA = 0.35
PULAR_FRAMES = 4
COOLDOWN_DETECCAO = 6

CICLO_ESTRADA_SEG = 45
ESPERA_MOTOR = 2.5
OUT_DIR = "tmp/ptz_continuo"

LARGURA_ALVO_OCR = 0.45  # veiculo precisa ocupar ~45% da largura do frame antes de tentar OCR


class CameraPTZ:
    def __init__(self) -> None:
        self.auth = HTTPDigestAuth(CAM_USER, CAM_PASS)
        self.posicao_atual: tuple[int, int, int] | None = None

    def mover(self, azimuth: int, elevation: int, zoom: int, espera: float = ESPERA_MOTOR) -> None:
        azimuth = int(max(0, min(3600, azimuth)))
        elevation = int(max(0, min(900, elevation)))
        zoom = int(max(10, min(150, zoom)))
        alvo = (azimuth, elevation, zoom)
        if self.posicao_atual == alvo:
            return
        body = (
            f"<PTZData><AbsoluteHigh><elevation>{elevation}</elevation>"
            f"<azimuth>{azimuth}</azimuth><absoluteZoom>{zoom}</absoluteZoom>"
            f"</AbsoluteHigh></PTZData>"
        )
        r = requests.put(f"{CAM_URL}/ISAPI/PTZCtrl/channels/1/absolute", auth=self.auth, data=body, timeout=10)
        r.raise_for_status()
        self.posicao_atual = alvo
        time.sleep(espera)

    def snapshot(self) -> np.ndarray | None:
        r = requests.get(f"{CAM_URL}/ISAPI/Streaming/channels/101/picture", auth=self.auth, timeout=10)
        if r.status_code != 200:
            return None
        arr = np.frombuffer(r.content, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def ler_placa(conteudo_jpg: bytes) -> dict:
    r = requests.post(OCR_URL, files={"imagem": ("v.jpg", conteudo_jpg, "image/jpeg")}, timeout=15)
    r.raise_for_status()
    return r.json()


def salvar_e_ocr(imagem: np.ndarray, origem: str, classe: str, conf: float, tentar_ocr: bool) -> None:
    ok, jpg_bytes = cv2.imencode(".jpg", imagem)
    ts = int(time.time())
    caminho = f"{OUT_DIR}/{origem}_{classe}_{ts}.jpg"
    if ok:
        open(caminho, "wb").write(jpg_bytes.tobytes())
    print(f"[{time.strftime('%H:%M:%S')}] {origem}: {classe} conf={conf:.2f} -> {caminho}", flush=True)
    if tentar_ocr and ok:
        try:
            resultado = ler_placa(jpg_bytes.tobytes())
        except Exception as exc:  # noqa: BLE001
            resultado = {"erro": str(exc)}
        print(f"  -> OCR: {resultado}", flush=True)


def olhar_estrada(cam: CameraPTZ, modelo: YOLO, duracao_seg: int) -> None:
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"[{time.strftime('%H:%M:%S')}] falha ao abrir RTSP", flush=True)
        time.sleep(2)
        return
    print(f"[{time.strftime('%H:%M:%S')}] olhando estrada de terra ao vivo ({duracao_seg}s)...", flush=True)

    fim = time.time() + duracao_seg
    n_frame = 0
    ultima_deteccao: dict[int, float] = {}
    x1, y1, x2, y2 = ZONA_TERRA_CROP

    try:
        while time.time() < fim:
            ok, frame = cap.read()
            if not ok or frame is None:
                print(f"[{time.strftime('%H:%M:%S')}] RTSP caiu, reabrindo...", flush=True)
                return
            n_frame += 1
            if n_frame % PULAR_FRAMES != 0:
                continue

            crop = frame[y1:y2, x1:x2]
            res = modelo.predict(crop, classes=list(CLASSES_ALVO), conf=CONF_MINIMA, verbose=False)[0]
            if len(res.boxes) == 0:
                continue

            agora = time.time()
            novas = [b for b in res.boxes if agora - ultima_deteccao.get(int(b.cls[0].item()), 0) > COOLDOWN_DETECCAO]
            if not novas:
                continue
            for b in novas:
                ultima_deteccao[int(b.cls[0].item())] = agora

            for b in novas:
                cid = int(b.cls[0].item())
                classe = CLASSES_ALVO[cid]
                conf = float(b.conf[0].item())
                bx1, by1, bx2, by2 = b.xyxy[0].tolist()

                if cid not in (2, 3):  # pessoa: so registra, sem zoom/OCR
                    salvar_e_ocr(crop, "estrada_terra", classe, conf, tentar_ocr=False)
                    continue

                frac_largura = (bx2 - bx1) / crop.shape[1]
                if frac_largura >= LARGURA_ALVO_OCR:
                    salvar_e_ocr(crop, "estrada_terra", classe, conf, tentar_ocr=True)
                # veiculo ainda longe/pequeno: zoom dirigido testado ao vivo e
                # abandonado (26/26 tentativas perderam o veiculo -- o motor
                # de azimute nao coopera com ajuste fino automatico, mesmo
                # centralizando com insistencia antes de cada passo de zoom).
                # mais simples e confiavel so esperar -- o video e continuo,
                # ele vai reaparecer maior em frames seguintes conforme se
                # aproxima, sem precisar mexer no motor.
    finally:
        cap.release()


def espiar_pista(cam: CameraPTZ, modelo: YOLO) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] espiando pista pavimentada...", flush=True)
    cam.mover(*ZONA_PISTA_PTZ)

    achou = False
    for _ in range(3):
        frame = cam.snapshot()
        if frame is not None:
            res = modelo.predict(frame, classes=list(CLASSES_ALVO), conf=CONF_MINIMA, verbose=False)[0]
            if len(res.boxes) > 0:
                achou = True
                for b in res.boxes:
                    cid = int(b.cls[0].item())
                    salvar_e_ocr(frame, "pista_curva", CLASSES_ALVO[cid], float(b.conf[0].item()), tentar_ocr=cid in (2, 3))
                break
        time.sleep(1)

    if not achou:
        print(f"[{time.strftime('%H:%M:%S')}] pista_curva: nada", flush=True)

    cam.mover(*HOME)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    cam = CameraPTZ()
    modelo = YOLO("yolo11n.pt")
    cam.mover(*HOME)

    duracao_total = int(os.environ.get("DURACAO_SEG", "1800"))
    fim = time.time() + duracao_total

    while time.time() < fim:
        try:
            olhar_estrada(cam, modelo, CICLO_ESTRADA_SEG)
            cam.mover(*HOME)
            espiar_pista(cam, modelo)
        except Exception as exc:  # noqa: BLE001
            print(f"[{time.strftime('%H:%M:%S')}] erro: {exc}", flush=True)
            try:
                cam.mover(*HOME)
            except Exception:
                pass
            time.sleep(3)

    print("fim", flush=True)


if __name__ == "__main__":
    main()
