import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

from app.api.endpoints import maintenance
from app.api.endpoints.maintenance import (
    _camera_web_target_url,
    _is_proxy_allowed_host,
    _rewrite_camera_web_content,
)

# _camera_web_target_url agora exige que o IP pertenca ao inventario do
# tenant atual (corrige um bug real: qualquer usuario logado conseguia usar
# o proxy pra abrir a camera -- ou qualquer servico HTTP privado -- de OUTRO
# cliente so sabendo o IP). Esse teste e sobre construcao de URL/reescrita de
# HTML, nao sobre a checagem de posse em si, entao ela e substituida aqui.
_ip_belongs_to_current_tenant_original = maintenance._ip_belongs_to_current_tenant
maintenance._ip_belongs_to_current_tenant = lambda ip: True


def assert_true(value: bool, label: str) -> None:
    if not value:
        raise AssertionError(label)


def assert_equal(actual: str, expected: str) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def main() -> None:
    assert_true(_is_proxy_allowed_host("100.64.11.39"), "CGNAT deve ser permitido")
    assert_true(_is_proxy_allowed_host("10.50.11.1"), "RFC1918 deve ser permitido")
    assert_true(not _is_proxy_allowed_host("8.8.8.8"), "IP publico deve ser bloqueado")
    assert_equal(
        _camera_web_target_url("100.64.11.39", "doc/index.html", "a=1"),
        "http://100.64.11.39/doc/index.html?a=1",
    )
    try:
        _camera_web_target_url("8.8.8.8", "", "")
    except HTTPException as exc:
        assert_equal(str(exc.status_code), "400")
    else:
        raise AssertionError("IP publico nao foi bloqueado")

    assert_true(not _is_proxy_allowed_host("127.0.0.1"), "loopback deve ser bloqueado")
    assert_true(not _is_proxy_allowed_host("169.254.1.1"), "link-local deve ser bloqueado")

    maintenance._ip_belongs_to_current_tenant = lambda ip: False
    try:
        _camera_web_target_url("10.50.11.1", "", "")
    except HTTPException as exc:
        assert_equal(str(exc.status_code), "403")
    else:
        raise AssertionError("IP fora do inventario do tenant nao foi bloqueado")
    maintenance._ip_belongs_to_current_tenant = lambda ip: True

    html = (
        b'<html><head></head><body><script src="/doc/app.js"></script>'
        b'<script src="/jsBase/lib/jquery.js"></script><a href="/ISAPI/System">x</a>'
        b'<script>$.ajax({url:"/jsBase/lib/m.js"});</script></body></html>'
    )
    rewritten = _rewrite_camera_web_content(html, ip="100.64.11.39", content_type="text/html; charset=utf-8").decode()
    assert_true('<base href="/api/maintenance/web/100.64.11.39/">' in rewritten, "base href nao foi injetado corretamente")
    assert_true("window.__sightopsCameraProxy" in rewritten, "shim de proxy nao foi injetado")
    assert_true('\\"' not in rewritten, "HTML reescrito nao deve conter aspas escapadas no base")
    assert_true(
        "/api/maintenance/web/100.64.11.39/api/maintenance/web" not in rewritten,
        "proxy root nao pode ser duplicado",
    )
    assert_true("/api/maintenance/web/100.64.11.39/doc/app.js" in rewritten, "src absoluto nao foi reescrito")
    assert_true("/api/maintenance/web/100.64.11.39/jsBase/lib/jquery.js" in rewritten, "jsBase absoluto nao foi reescrito")
    assert_true('/api/maintenance/web/100.64.11.39/jsBase/lib/m.js' in rewritten, "url JS absoluta nao foi reescrita")
    assert_true("/api/maintenance/web/100.64.11.39/ISAPI/System" in rewritten, "href absoluto nao foi reescrito")

    # --- host de DVR/NVR (sem estar no inventario de camera) tambem e
    #     aceito pela checagem de posse, e host de nenhum dos dois inventarios
    #     continua bloqueado
    maintenance._ip_belongs_to_current_tenant = _ip_belongs_to_current_tenant_original
    maintenance._ip_in_inventory = lambda ip: False
    maintenance._host_in_recorder_inventory = lambda ip: True
    assert_true(maintenance._ip_belongs_to_current_tenant("10.50.11.2"), "host de DVR/NVR deveria ser aceito")

    maintenance._host_in_recorder_inventory = lambda ip: False
    assert_true(not maintenance._ip_belongs_to_current_tenant("10.50.11.2"), "host fora dos dois inventarios deveria ser recusado")
    print("ok")


if __name__ == "__main__":
    main()
