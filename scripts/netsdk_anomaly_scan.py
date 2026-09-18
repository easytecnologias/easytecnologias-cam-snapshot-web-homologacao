"""Detecta veiculo que estava se movendo e parou de forma anormal --
sinal real de acidente/saida de pista, diferente de so "classificou como
carro" (que nao distingue trafego normal de um veiculo acidentado).

Usa tracking do YOLO (ByteTrack, via ultralytics) pra seguir o MESMO
objeto entre frames, em vez de avaliar cada frame isolado. Testado as
cegas contra um clipe com acidente conhecido antes de usar numa varredura
ao vivo -- ver docs/HANDOFF_AGENTES.md, entrada 2026-09-07 (continuacao).

Uso: python3 scripts/netsdk_anomaly_scan.py <arquivo_de_video>
"""
import sys
from collections import defaultdict

from ultralytics import YOLO

CLASSES_VEICULO = {2: "carro", 3: "moto", 5: "onibus", 7: "caminhao"}
LIMIAR_CONFIANCA = 0.35

JANELA_MOVIMENTO = 6       # frames observados pra decidir se "estava se movendo"
JANELA_PARADA = 6          # frames observados pra decidir se "parou agora"
DESLOC_MOVIMENTO_PX = 10   # deslocamento medio por frame acima disso = "em movimento"
DESLOC_PARADA_PX = 4       # deslocamento medio por frame abaixo disso = "parado"


def distancia(a, b):
    return ((a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def desloc_medio(pontos):
    if len(pontos) < 2:
        return 0.0
    return sum(distancia(a, b) for a, b in zip(pontos, pontos[1:])) / (len(pontos) - 1)


def main(video_path: str) -> None:
    modelo = YOLO("yolo11n.pt")
    historico: dict[int, list[tuple[int, float, float, str]]] = defaultdict(list)
    avisado: set[int] = set()

    resultados = modelo.track(
        video_path,
        classes=list(CLASSES_VEICULO),
        conf=LIMIAR_CONFIANCA,
        persist=True,
        stream=True,
        verbose=False,
        tracker="bytetrack.yaml",
    )

    for frame_idx, r in enumerate(resultados):
        if r.boxes is None or r.boxes.id is None:
            continue
        for box, track_id in zip(r.boxes, r.boxes.id):
            tid = int(track_id)
            classe_id = int(box.cls[0])
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            classe = CLASSES_VEICULO.get(classe_id, "?")
            historico[tid].append((frame_idx, cx, cy, classe))

            hist = historico[tid]
            if tid in avisado or len(hist) < JANELA_MOVIMENTO + JANELA_PARADA:
                continue

            recentes = hist[-JANELA_PARADA:]
            anteriores = hist[-(JANELA_PARADA + JANELA_MOVIMENTO):-JANELA_PARADA]

            estava_em_movimento = desloc_medio(anteriores) > DESLOC_MOVIMENTO_PX
            parou_agora = desloc_medio(recentes) < DESLOC_PARADA_PX

            if estava_em_movimento and parou_agora:
                avisado.add(tid)
                print(
                    f"SUSPEITO -- frame {frame_idx}, track {tid} ({classe}): "
                    f"estava em movimento (desloc {desloc_medio(anteriores):.1f}px/frame) "
                    f"e parou em ({cx:.0f},{cy:.0f}) "
                    f"(desloc {desloc_medio(recentes):.1f}px/frame)"
                )

    print(f"\nfim -- {len(avisado)} track(s) suspeito(s): {sorted(avisado)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "video.mp4")
