"""Capabilities por driver de OLT.

Este modulo e a barreira entre a tela generica de ONU e os comandos reais de
cada fabricante/modelo. Sem ele, uma OLT desconhecida pode cair no driver
padrao da 8820i e executar comandos errados.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from fastapi import HTTPException


_CAPABILITY_LABELS = {
    "collect_macs": "sincronizar inventario",
    "telemetry": "coletar telemetria",
    "discover_onus": "descobrir ONUs",
    "add_onu": "autorizar ONU",
    "find_onu": "localizar ONU",
    "delete_onu": "excluir ONU",
    "onu_signal": "consultar sinal/MACs",
    "reboot_onu": "reiniciar ONU",
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def normalize_olt_driver(vendor: Any = "", model: Any = "") -> str:
    vendor_key = _norm(vendor)
    model_key = _norm(model)
    if vendor_key in ("vsol", "v-sol", "vsolution") or model_key.startswith(("vsol", "v1600", "epon-olt")):
        return "vsol_epon"
    if vendor_key == "intelbras":
        if model_key in {"4840e", "4840", "intelbras-4840e", "intelbras-4840e-epon", "4840e-epon"}:
            return "intelbras_4840e"
        if model_key in {"8820i", "8820", "intelbras-8820i"}:
            return "intelbras_8820i"
    if model_key in {"4840e", "4840", "intelbras-4840e", "intelbras-4840e-epon", "4840e-epon"}:
        return "intelbras_4840e"
    if model_key in {"8820i", "8820", "intelbras-8820i"}:
        return "intelbras_8820i"
    return "unknown"


def _base_caps() -> Dict[str, bool]:
    return {key: False for key in _CAPABILITY_LABELS}


def olt_capabilities(vendor: Any = "", model: Any = "") -> Dict[str, Any]:
    driver = normalize_olt_driver(vendor, model)
    caps = _base_caps()
    notes = ""

    if driver == "intelbras_8820i":
        caps.update({
            "collect_macs": True,
            "telemetry": True,
            "discover_onus": True,
            "add_onu": True,
            "find_onu": True,
            "delete_onu": True,
            "onu_signal": True,
            "reboot_onu": True,
        })
        label = "Intelbras 8820i"
        notes = "Provisionamento e consulta homologados para o fluxo Intelbras 8820i."
    elif driver == "intelbras_4840e":
        caps.update({
            "collect_macs": True,
            "telemetry": True,
            "discover_onus": True,
            "add_onu": True,
            "find_onu": True,
            "delete_onu": True,
            "onu_signal": True,
            "reboot_onu": True,
        })
        label = "Intelbras 4840E"
        notes = "Homologada para inventario, telemetria, autorizar/localizar/excluir/reiniciar ONU e consultar sinal (whitelist por MAC)."
    elif driver == "vsol_epon":
        caps.update({
            "collect_macs": True,
            "telemetry": True,
            "discover_onus": True,
            "find_onu": True,
            "onu_signal": True,
            "add_onu": True,
            "delete_onu": True,
            "reboot_onu": True,
        })
        label = "VSOL EPON"
        notes = "Homologada: inventario, telemetria, descoberta, consulta, autorizar, excluir e reiniciar ONU."
    else:
        label = "OLT nao homologada"
        notes = "Cadastre o modelo para inventario, mas comandos na OLT ficam bloqueados ate homologacao."

    return {
        "driver": driver,
        "label": label,
        "capabilities": caps,
        "labels": dict(_CAPABILITY_LABELS),
        "notes": notes,
    }


def _value(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def require_olt_capability(req_or_olt: Any, capability: str, action_label: str = "") -> Dict[str, Any]:
    vendor = _value(req_or_olt, "olt_vendor") or _value(req_or_olt, "vendor")
    model = _value(req_or_olt, "olt_model") or _value(req_or_olt, "model")
    info = olt_capabilities(vendor, model)
    if info["capabilities"].get(capability):
        return info
    label = info.get("label") or "OLT"
    action = action_label or _CAPABILITY_LABELS.get(capability) or capability
    raise HTTPException(
        status_code=422,
        detail=(
            f"{label} ainda nao suporta {action} no SightOps. "
            f"{info.get('notes') or 'Homologue este driver antes de executar comandos na OLT.'}"
        ),
    )
