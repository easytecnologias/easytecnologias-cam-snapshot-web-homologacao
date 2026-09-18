"""Servico minimo de LPR (leitura de placa) via fast-alpr (ONNX, leve).

Recebe uma imagem (o snapshot com zoom ja dado na placa) e devolve o
texto lido. Fica isolado num container proprio -- nao entra no
sightops-prod-api -- porque e um pipeline diferente (deteccao+OCR de
placa via ONNX), nao a logica de negocio do SightOps.

Ver docs/HANDOFF_AGENTES.md, entrada 2026-09-07 (LPR) para o contexto:
zoom motorizado remoto ja resolvido em scripts/intelbras_zoom_rpc.py,
este servico e o proximo passo (ler a placa depois do zoom).
"""
from __future__ import annotations

import io

import numpy as np
from fastapi import FastAPI, File, UploadFile
from fast_alpr import ALPR
from PIL import Image

app = FastAPI(title="SightOps LPR OCR")

# Carrega uma vez, no start do processo -- nao a cada requisicao.
alpr = ALPR(
    detector_model="yolo-v9-t-384-license-plate-end2end",
    ocr_model="cct-s-v1-global-model",
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ler-placa")
async def ler_placa(imagem: UploadFile = File(...)):
    conteudo = await imagem.read()
    img = Image.open(io.BytesIO(conteudo)).convert("RGB")
    array = np.array(img)

    resultados = alpr.predict(array)
    placas = [
        {
            "texto": r.ocr.text if r.ocr else None,
            "confianca_ocr": r.ocr.confidence if r.ocr else None,
            "confianca_deteccao": r.detection.confidence,
            "caixa": {
                "x1": r.detection.bounding_box.x1, "y1": r.detection.bounding_box.y1,
                "x2": r.detection.bounding_box.x2, "y2": r.detection.bounding_box.y2,
            },
        }
        for r in resultados
    ]
    return {"placas_encontradas": placas}
