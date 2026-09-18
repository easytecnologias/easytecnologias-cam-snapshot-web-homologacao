"""Versao reativa (evento-driven) do pipeline de zonas: fica ouvindo o
alertStream ISAPI da Hikvision e, quando chega um evento VMD real (nao o
heartbeat de videoloss), tira um snapshot e roda deteccao nas duas zonas
(estrada_terra e pista_curva) -- assim nao perde veiculo nenhum entre polls
como acontecia no hikvision_ptz_zonas.py.

Descobertas que levaram a essa versao (ver docs/HANDOFF_AGENTES.md):
  - LineDetection ("linha de intrusao" nativa) aceita o PUT (200 OK) mas
    nao persiste -- os LineItem sempre voltam desabilitados. Provavel
    falta de licenca desse recurso "Smart" avancado nesse hardware.
  - VMD (deteccao de movimento por grid) basico funciona e persiste.
  - O alertStream so manda o evento VMD se o EventTrigger VMD-1 tiver uma
    notificacao "center" cadastrada -- por padrao a lista vem vazia e o
    evento simplesmente nunca aparece no stream, mesmo com VMD habilitado
    e disparando de verdade internamente (confirmado marcando a tela
    inteira: com "center" cadastrado, disparou a cada 1-2s so com o vento
    na grama). Configurado via PUT /ISAPI/Event/triggers/VMD-1/notifications.
  - O payload do evento VMD nao diz qual regiao do grid disparou -- so diz
    que houve movimento no canal. Por isso ao disparar, sempre confere as
    duas zonas (nao da pra saber de antemao qual delas foi).
"""
from __future__ import annotations

import os
import time
from threading import Thread

import cv2
import numpy as np
import requests
from requests.auth import HTTPDigestAuth
from ultralytics import YOLO

CAM_URL = "http://10.10.8.15"
CAM_USER = "admin"
CAM_PASS = "cam!perucaba@$"
OCR_URL = "http://localhost:18600/ler-placa"

HOME = (3323, 98, 10)
ZONAS = {
    "estrada_terra": {"ptz": None, "crop": (60, 480, 560, 900), "upscale": 2},
    "pista_curva": {"ptz": (3473, 58, 70), "crop": None, "upscale": 1},
}

CLASSES_ALVO = {0: "pessoa", 2: "carro", 3: "moto"}
CONF_MINIMA = 0.30
ESPERA_MOTOR = 2.5
COOLDOWN_EVENTO = 8  # segundos minimos entre processamentos (o veiculo pode gerar varios VMD seguidos)
OUT_DIR = "tmp/ptz_reativo"


class CameraPTZ:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.auth = HTTPDigestAuth(user, password)
        self.base = base_url
        self.posicao_atual: tuple[int, int, int] | None = None

    def mover(self, azimuth: int, elevation: int, zoom: int) -> bool:
        """Retorna True se realmente mandou o motor mexer (posicao mudou)."""
        alvo = (azimuth, elevation, zoom)
        if self.posicao_atual == alvo:
            return False
        body = (
            f"<PTZData><AbsoluteHigh><elevation>{elevation}</elevation>"
            f"<azimuth>{azimuth}</azimuth><absoluteZoom>{zoom}</absoluteZoom>"
            f"</AbsoluteHigh></PTZData>"
        )
        r = requests.put(f"{self.base}/ISAPI/PTZCtrl/channels/1/absolute", auth=self.auth, data=body, timeout=10)
        r.raise_for_status()
        self.posicao_atual = alvo
        return True

    def snapshot(self) -> bytes:
        r = requests.get(f"{self.base}/ISAPI/Streaming/channels/101/picture", auth=self.auth, timeout=10)
        r.raise_for_status()
        return r.content


def decode(conteudo: bytes) -> np.ndarray:
    arr = np.frombuffer(conteudo, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def preparar_imagem(frame_bgr: np.ndarray, cfg: dict) -> np.ndarray:
    if cfg["crop"]:
        x1, y1, x2, y2 = cfg["crop"]
        frame_bgr = frame_bgr[y1:y2, x1:x2]
    if cfg.get("upscale", 1) != 1:
        f = cfg["upscale"]
        frame_bgr = cv2.resize(frame_bgr, (frame_bgr.shape[1] * f, frame_bgr.shape[0] * f), interpolation=cv2.INTER_CUBIC)
    return frame_bgr


def ler_placa(conteudo_jpg: bytes) -> dict:
    r = requests.post(OCR_URL, files={"imagem": ("v.jpg", conteudo_jpg, "image/jpeg")}, timeout=15)
    r.raise_for_status()
    return r.json()


def processar_evento(cam: CameraPTZ, modelo: YOLO) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    algo_achado = False

    for nome, cfg in ZONAS.items():
        az, el, zoom = cfg["ptz"] if cfg["ptz"] else HOME
        moveu = cam.mover(az, el, zoom)
        if moveu:
            time.sleep(ESPERA_MOTOR)

        frame = decode(cam.snapshot())
        imagem_zona = preparar_imagem(frame, cfg)
        res = modelo.predict(imagem_zona, classes=list(CLASSES_ALVO), conf=CONF_MINIMA, verbose=False)[0]

        if len(res.boxes) == 0:
            print(f"[{time.strftime('%H:%M:%S')}] {nome}: nada", flush=True)
            continue

        algo_achado = True
        ok, jpg_bytes = cv2.imencode(".jpg", imagem_zona)
        for b in res.boxes:
            cid = int(b.cls[0].item())
            classe = CLASSES_ALVO[cid]
            conf = float(b.conf[0].item())
            ts = int(time.time())
            caminho = f"{OUT_DIR}/{nome}_{classe}_{ts}.jpg"
            if ok:
                open(caminho, "wb").write(jpg_bytes.tobytes())
            print(f"[{time.strftime('%H:%M:%S')}] {nome}: {classe} conf={conf:.2f} -> {caminho}", flush=True)

            if cid in (2, 3) and ok:
                x1, y1, x2, y2 = b.xyxy[0].tolist()
                mx, my = (x2 - x1) * 0.15, (y2 - y1) * 0.25
                h, w = imagem_zona.shape[:2]
                rx1, ry1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
                rx2, ry2 = min(w, int(x2 + mx)), min(h, int(y2 + my))
                veiculo_jpg = cv2.imencode(".jpg", imagem_zona[ry1:ry2, rx1:rx2])[1].tobytes()
                try:
                    resultado = ler_placa(veiculo_jpg)
                except Exception as exc:  # noqa: BLE001
                    resultado = {"erro": str(exc)}
                print(f"  -> OCR: {resultado}", flush=True)

    cam.mover(*HOME)
    if not algo_achado:
        print(f"[{time.strftime('%H:%M:%S')}] evento VMD mas nada encontrado nas zonas (falso positivo / vento / ja passou)", flush=True)


def main() -> None:
    cam = CameraPTZ(CAM_URL, CAM_USER, CAM_PASS)
    modelo = YOLO("yolo11n.pt")
    cam.mover(*HOME)

    print(f"[{time.strftime('%H:%M:%S')}] conectando ao alertStream (modo reativo)...", flush=True)
    ultimo_evento = 0.0

    with requests.get(f"{CAM_URL}/ISAPI/Event/notification/alertStream", auth=HTTPDigestAuth(CAM_USER, CAM_PASS), stream=True, timeout=None) as r:
        print(f"[{time.strftime('%H:%M:%S')}] status {r.status_code}, escutando...", flush=True)
        buf = b""
        for chunk in r.iter_content(chunk_size=512):
            if not chunk:
                continue
            buf += chunk
            if b"</EventNotificationAlert>" not in buf:
                continue
            texto = buf.decode("utf-8", errors="ignore")
            buf = b""
            if "videoloss" in texto or "<eventType>VMD</eventType>" not in texto or "<eventState>active</eventState>" not in texto:
                continue

            agora = time.time()
            if agora - ultimo_evento < COOLDOWN_EVENTO:
                continue
            ultimo_evento = agora

            print(f"[{time.strftime('%H:%M:%S')}] evento VMD ativo -- processando zonas...", flush=True)
            try:
                processar_evento(cam, modelo)
            except Exception as exc:  # noqa: BLE001
                print(f"[{time.strftime('%H:%M:%S')}] erro processando evento: {exc}", flush=True)
                try:
                    cam.mover(*HOME)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
