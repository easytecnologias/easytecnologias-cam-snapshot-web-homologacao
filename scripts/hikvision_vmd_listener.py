"""Escuta o alertStream ISAPI da Hikvision (10.10.8.15) em tempo real e
imprime qualquer evento que NAO seja o heartbeat padrao de videoloss --
serve pra confirmar (e descobrir o formato exato) do evento de VMD
(deteccao de movimento) configurado nas duas zonas via grid.

Contexto: a deteccao de linha "Smart" (LineDetection) aceita o PUT (200 OK)
mas nao persiste de verdade (os LineItem sempre voltam desabilitados) --
provavelmente falta de licenca desse recurso avancado nesse hardware
especifico. O VMD basico (grid) persistiu normalmente, entao a estrategia
agora e reagir a eventos de VMD em vez de fazer polling as cegas.
"""
from __future__ import annotations

import time

import requests
from requests.auth import HTTPDigestAuth

CAM_URL = "http://10.10.8.15"
CAM_USER = "admin"
CAM_PASS = "cam!perucaba@$"


def main() -> None:
    auth = HTTPDigestAuth(CAM_USER, CAM_PASS)
    print(f"[{time.strftime('%H:%M:%S')}] conectando ao alertStream...", flush=True)

    with requests.get(f"{CAM_URL}/ISAPI/Event/notification/alertStream", auth=auth, stream=True, timeout=None) as r:
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
            if "videoloss" in texto:
                continue  # heartbeat, ignora
            print(f"[{time.strftime('%H:%M:%S')}] EVENTO NAO-VIDEOLOSS:", flush=True)
            print(texto, flush=True)
            print("---", flush=True)


if __name__ == "__main__":
    main()
