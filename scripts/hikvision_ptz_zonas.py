"""Patrulha entre duas zonas marcadas pelo usuario na Hikvision PTZ
(10.10.8.15, "SPEED PISTA NOVA"), tentando ler placa/pessoa em cada uma.

Diferente do hikvision_ptz_autozoom.py (que tenta detectar em qualquer lugar
do quadro largo e depois perseguir com zoom em malha fechada), aqui as duas
zonas de interesse sao fixas -- foi constatado na pratica que:

  - zona "estrada_terra": bem perto da camera, ja tem resolucao de sobra na
    visao larga (da pra ver marca de pneu no chao) -- so precisa de recorte
    digital, sem mexer no motor.
  - zona "pista_curva": longe, na pista pavimentada -- uma moto real testada
    ao vivo nao apareceu nem em recorte digital ampliado 5x (viram bolha sem
    detalhe). Precisa de zoom optico de verdade. Preset calibrado na pratica
    (apontando o motor e conferindo o enquadramento visualmente, ja que o
    eixo de azimute tem resposta nao-linear e nao da pra confiar numa
    constante grau/pixel -- ver hikvision_ptz_autozoom.py): partindo do home
    (az=3323, el=98, zoom=10), mover pra (az=3473, el=58, zoom=70) enquadra
    bem a faixa da curva.

Sempre volta pro home entre os ciclos.
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
OCR_URL = "http://localhost:18600/ler-placa"

HOME = (3323, 98, 10)

ZONAS = {
    "estrada_terra": {
        "ptz": None,  # fica no home
        "crop": (60, 480, 560, 900),  # x1,y1,x2,y2 na imagem 1920x1080 do home
        "upscale": 2,
    },
    "pista_curva": {
        "ptz": (3473, 58, 70),
        "crop": None,  # ja vem enquadrado pelo zoom optico
        "upscale": 1,
    },
}

CLASSES_ALVO = {0: "pessoa", 2: "carro", 3: "moto"}
CONF_MINIMA = 0.30
ESPERA_MOTOR = 2.5
INTERVALO_CICLO = 3
COOLDOWN_ZONA_CLASSE = 15

OUT_DIR = "tmp/ptz_zonas"


class CameraPTZ:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.auth = HTTPDigestAuth(user, password)
        self.base = base_url

    def mover(self, azimuth: int, elevation: int, zoom: int) -> None:
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
    r = requests.post(OCR_URL, files={"imagem": ("zona.jpg", conteudo_jpg, "image/jpeg")}, timeout=15)
    r.raise_for_status()
    return r.json()


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    cam = CameraPTZ(CAM_URL, CAM_USER, CAM_PASS)
    modelo = YOLO("yolo11n.pt")

    ultimo_processado: dict[tuple[str, int], float] = {}
    duracao = int(os.environ.get("DURACAO_SEG", "600"))
    fim = time.time() + duracao

    print(f"[{time.strftime('%H:%M:%S')}] patrulhando zonas {list(ZONAS)} por {duracao}s...", flush=True)

    while time.time() < fim:
        for nome, cfg in ZONAS.items():
            try:
                az, el, zoom = cfg["ptz"] if cfg["ptz"] else HOME
                cam.mover(az, el, zoom)
                time.sleep(ESPERA_MOTOR)

                bruto = cam.snapshot()
                frame = decode(bruto)
                imagem_zona = preparar_imagem(frame, cfg)

                res = modelo.predict(imagem_zona, classes=list(CLASSES_ALVO), conf=CONF_MINIMA, verbose=False)[0]
                agora = time.time()

                if len(res.boxes) == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] {nome}: nada", flush=True)
                    continue

                ok, jpg_bytes = cv2.imencode(".jpg", imagem_zona)
                jpg_bytes = jpg_bytes.tobytes() if ok else None

                for b in res.boxes:
                    cid = int(b.cls[0].item())
                    classe = CLASSES_ALVO[cid]
                    conf = float(b.conf[0].item())
                    chave = (nome, cid)
                    if agora - ultimo_processado.get(chave, 0) < COOLDOWN_ZONA_CLASSE:
                        continue
                    ultimo_processado[chave] = agora

                    ts = int(agora)
                    caminho = f"{OUT_DIR}/{nome}_{classe}_{ts}.jpg"
                    if jpg_bytes:
                        open(caminho, "wb").write(jpg_bytes)

                    print(f"[{time.strftime('%H:%M:%S')}] {nome}: {classe} conf={conf:.2f} -> {caminho}", flush=True)

                    if cid in (2, 3):  # carro ou moto -> recorta o veiculo e tenta OCR
                        # manda o frame inteiro pro OCR nao funciona -- a placa fica
                        # pequena demais em proporcao (testado ao vivo: 0 placas
                        # encontradas no frame completo, funcionou so recortando o
                        # veiculo antes de mandar).
                        x1, y1, x2, y2 = b.xyxy[0].tolist()
                        mx = (x2 - x1) * 0.15
                        my = (y2 - y1) * 0.25
                        h, w = imagem_zona.shape[:2]
                        rx1, ry1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
                        rx2, ry2 = min(w, int(x2 + mx)), min(h, int(y2 + my))
                        veiculo_crop = imagem_zona[ry1:ry2, rx1:rx2]
                        ok_v, veiculo_jpg = cv2.imencode(".jpg", veiculo_crop)
                        caminho_veiculo = f"{OUT_DIR}/{nome}_{classe}_{ts}_recorte.jpg"
                        if ok_v:
                            open(caminho_veiculo, "wb").write(veiculo_jpg.tobytes())
                            try:
                                resultado = ler_placa(veiculo_jpg.tobytes())
                            except Exception as exc:  # noqa: BLE001
                                resultado = {"erro": str(exc)}
                        else:
                            resultado = {"erro": "falha ao recortar veiculo"}
                        print(f"  -> OCR ({caminho_veiculo}): {resultado}", flush=True)

            except Exception as exc:  # noqa: BLE001 -- script de teste, loga e segue
                print(f"[{time.strftime('%H:%M:%S')}] erro na zona {nome}: {exc}", flush=True)

        cam.mover(*HOME)
        time.sleep(INTERVALO_CICLO)

    cam.mover(*HOME)
    print("fim, camera de volta pra home", flush=True)


if __name__ == "__main__":
    main()
