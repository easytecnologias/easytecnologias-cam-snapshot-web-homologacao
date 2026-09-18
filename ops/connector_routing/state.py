"""Estado persistido do roteamento por conector (mapa connector_id -> index).

O index de cada conector NUNCA pode mudar entre execucoes (todos os recursos
derivam dele), entao ele mora num JSON simples. Escrita atomica (tmp + rename)
pra nunca deixar o arquivo pela metade se o processo morrer no meio.

Nao guarda nenhum segredo -- so numeros/derivados. A chave privada do WireGuard
continua so em /etc/wireguard, lida em runtime, nunca aqui.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_STATE_PATH = os.getenv(
    "CONNECTOR_ROUTING_STATE", "/etc/sightops/connector_routing_state.json"
)


def _empty() -> Dict[str, Any]:
    return {"connectors": {}}


def load_state(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path or DEFAULT_STATE_PATH)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("connectors", {})
    if not isinstance(data["connectors"], dict):
        data["connectors"] = {}
    return data


def save_state(state: Dict[str, Any], path: Optional[str] = None) -> None:
    p = Path(path or DEFAULT_STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp-{os.getpid()}")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(p))  # rename atomico no mesmo filesystem
