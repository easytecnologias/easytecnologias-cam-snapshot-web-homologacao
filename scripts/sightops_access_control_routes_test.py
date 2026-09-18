from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Dict
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.endpoints.access_control import (
    AccessDoorGroupRequest,
    AccessGroupRequest,
    AccessRuleRequest,
    api_access_control_people,
    api_access_control_person_face_photo_get,
    api_access_control_sync_person,
    api_access_control_test_device,
    api_access_control_save_door_group,
    api_access_control_save_group,
    api_access_control_save_rule,
    router,
)
from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
from app.services import db_store
from app.services.access_control_store import (
    list_pending_provisions,
    save_device,
    save_person,
    upsert_provision_status,
)


def test_new_routes_registered() -> None:
    # FastAPI registra um objeto de rota por decorator (@router.get, @router.post, ...),
    # entao GET e POST no mesmo path viram DUAS entradas em router.routes, cada uma com
    # route.methods = {"GET"} ou {"POST"} isoladamente -- nunca {"GET", "POST"} junto.
    # Por isso agregamos os metodos por path antes de comparar com o conjunto esperado.
    methods_by_path: Dict[str, set[str]] = {}
    for route in router.routes:
        methods_by_path.setdefault(route.path, set()).update(route.methods)
    paths = {(path, tuple(sorted(methods))) for path, methods in methods_by_path.items()}
    expected = {
        ("/api/access-control/devices", ("GET", "POST")),
        ("/api/access-control/devices/{device_id}", ("DELETE",)),
        ("/api/access-control/devices/{device_id}/open-door", ("POST",)),
        ("/api/access-control/devices/{device_id}/test", ("POST",)),
        ("/api/access-control/groups", ("GET", "POST")),
        ("/api/access-control/groups/{group_id}", ("DELETE",)),
        ("/api/access-control/door-groups", ("GET", "POST")),
        ("/api/access-control/door-groups/{door_group_id}", ("DELETE",)),
        ("/api/access-control/rules", ("GET", "POST")),
        ("/api/access-control/rules/{rule_id}", ("DELETE",)),
        ("/api/access-control/people/{person_id}/face-photo", ("GET", "POST")),
        ("/api/access-control/people/{person_id}/sync", ("POST",)),
        ("/api/access-control/events", ("GET",)),
    }
    missing = expected - paths
    assert not missing, f"rotas faltando: {missing}"


def test_face_photo_get_route_serves_saved_jpeg() -> None:
    token = set_current_tenant_slug("cliente-face-photo-get")
    try:
        person = save_person({"full_name": "Aluno Foto", "document_id": "11111111111", "controller_user_id": "1001"})
        with patch("app.api.endpoints.access_control.load_person_face_photo", return_value=b"jpg-bytes"):
            response = api_access_control_person_face_photo_get(person["id"])
        assert response.media_type == "image/jpeg"
        assert response.body == b"jpg-bytes"
    finally:
        reset_current_tenant_slug(token)


def test_device_connection_test_updates_status_and_model() -> None:
    token = set_current_tenant_slug("cliente-device-test")
    try:
        device = save_device({
            "name": "Intelbras Portaria",
            "site": "Sede",
            "vendor": "intelbras",
            "host": "10.10.10.175",
            "username": "admin",
            "password": "senha-teste",
        })
        with patch(
            "app.api.endpoints.access_control.device_get_system_info",
            return_value={
                "deviceType": "SS 3542 MF W",
                "updateSerial": "ASI6214S-W",
                "serialNumber": "5PGM3702765BS",
            },
        ):
            result = api_access_control_test_device(device["id"])
        assert result["ok"] is True
        assert result["device"]["status"] == "online"
        assert result["device"]["model"] == "ASI6214S-W"
        assert result["device"]["last_seen_at"]
        assert result["info"]["serialNumber"] == "5PGM3702765BS"
    finally:
        reset_current_tenant_slug(token)


def test_people_list_includes_provision_summary() -> None:
    token = set_current_tenant_slug("cliente-people-status")
    try:
        person = save_person({"full_name": "Elishafan Status", "document_id": "22222222222", "site": "Sede"})
        ok_device = save_device({
            "name": "Intelbras Entrada",
            "site": "Sede",
            "vendor": "intelbras",
            "host": "10.10.10.175",
            "username": "admin",
            "password": "secret",
        })
        failed_device = save_device({
            "name": "Intelbras Saida",
            "site": "Sede",
            "vendor": "intelbras",
            "host": "10.10.10.176",
            "username": "admin",
            "password": "secret",
        })
        upsert_provision_status(person["id"], ok_device["id"], "ok")
        upsert_provision_status(person["id"], failed_device["id"], "failed", "face invalida")

        result = api_access_control_people(search="", active="", person_type="", site="")

        listed = result["people"][0]
        assert listed["provision_summary"]["status"] == "failed"
        assert listed["provision_summary"]["ok"] == 1
        assert listed["provision_summary"]["failed"] == 1
        assert "face invalida" in listed["provision_summary"]["last_error"]
    finally:
        reset_current_tenant_slug(token)


def test_manual_person_sync_returns_updated_provision_summary() -> None:
    token = set_current_tenant_slug("cliente-manual-sync")
    try:
        person = save_person({"full_name": "Elishafan Manual", "document_id": "33333333333", "site": "Sede"})
        device = save_device({
            "name": "Intelbras Portaria",
            "site": "Sede",
            "vendor": "intelbras",
            "host": "10.10.10.175",
            "username": "admin",
            "password": "secret",
        })
        group = api_access_control_save_group(AccessGroupRequest(name="Alunos", site="Sede", member_ids=[person["id"]]))["group"]
        door_group = api_access_control_save_door_group(
            AccessDoorGroupRequest(name="Portaria", site="Sede", device_ids=[device["id"]])
        )["door_group"]
        api_access_control_save_rule(AccessRuleRequest(people_group_id=group["id"], door_group_id=door_group["id"]))

        with patch("app.api.endpoints.access_control.provision_person_everywhere", return_value={"ok": True, "results": []}):
            upsert_provision_status(person["id"], device["id"], "ok")
            result = api_access_control_sync_person(person["id"])

        assert result["ok"] is True
        assert result["provision_summary"]["status"] == "ok"
        assert result["provision_summary"]["ok"] == 1
    finally:
        reset_current_tenant_slug(token)


def _pending_pairs() -> set[tuple[str, str]]:
    return {
        (str(row["person_id"]), str(row["device_id"]))
        for row in list_pending_provisions()
        if str(row.get("status")) == "pending"
    }


def test_normal_setup_order_enqueues_provisioning() -> None:
    """Ordem real de implantacao: cadastra pessoas -> cria grupo -> cria grupo de
    porta -> cria a regra que liga os dois. Antes desta correcao so o POST
    /people enfileirava provisionamento, entao essa sequencia terminava com ZERO
    linhas pendentes e o loop de fundo nunca provisionava ninguem
    (retry_pending_provisions so reprocessa o que ja existe). Nenhuma chamada a
    /people/{id}/sync aqui de proposito."""
    token = set_current_tenant_slug("cliente-provision-order")
    try:
        person = save_person({"full_name": "Ana Ordem", "document_id": "44444444444", "site": "Sede"})
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "xzydsP2011",
        })
        assert _pending_pairs() == set(), "nao deveria haver pendencia antes de existir regra"

        group = api_access_control_save_group(
            AccessGroupRequest(name="Alunos Manha", site="Sede", member_ids=[person["id"]])
        )["group"]
        # Ainda sem grupo de porta/regra: a pessoa nao alcanca nenhum dispositivo.
        assert _pending_pairs() == set()

        door_group = api_access_control_save_door_group(
            AccessDoorGroupRequest(name="Portao Principal", site="Sede", device_ids=[device["id"]])
        )["door_group"]
        assert _pending_pairs() == set()

        api_access_control_save_rule(
            AccessRuleRequest(people_group_id=group["id"], door_group_id=door_group["id"])
        )
        assert _pending_pairs() == {(person["id"], device["id"])}, (
            "salvar a regra tinha que enfileirar o par (pessoa, dispositivo) sem re-salvar a pessoa"
        )
    finally:
        reset_current_tenant_slug(token)


def test_adding_member_to_existing_group_enqueues_provisioning() -> None:
    """Caminho inverso: caminho de acesso ja montado, pessoa nova entra no grupo
    depois. Salvar o grupo (unica acao do operador) tem que enfileirar."""
    token = set_current_tenant_slug("cliente-provision-membro")
    try:
        first = save_person({"full_name": "Bruno Antigo", "document_id": "55555555555", "site": "Sede"})
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "xzydsP2011",
        })
        group = api_access_control_save_group(
            AccessGroupRequest(name="Alunos", site="Sede", member_ids=[first["id"]])
        )["group"]
        door_group = api_access_control_save_door_group(
            AccessDoorGroupRequest(name="Portao", site="Sede", device_ids=[device["id"]])
        )["door_group"]
        api_access_control_save_rule(
            AccessRuleRequest(people_group_id=group["id"], door_group_id=door_group["id"])
        )
        assert _pending_pairs() == {(first["id"], device["id"])}

        segundo = save_person({"full_name": "Carla Nova", "document_id": "66666666666", "site": "Sede"})
        api_access_control_save_group(
            AccessGroupRequest(
                id=group["id"], name="Alunos", site="Sede", member_ids=[first["id"], segundo["id"]]
            )
        )
        assert _pending_pairs() == {
            (first["id"], device["id"]),
            (segundo["id"], device["id"]),
        }
    finally:
        reset_current_tenant_slug(token)


def test_new_device_in_door_group_enqueues_provisioning() -> None:
    """Trocar/adicionar catraca num grupo de portas ja usado por uma regra ativa
    tem que enfileirar as pessoas alcancadas por essa regra."""
    token = set_current_tenant_slug("cliente-provision-porta")
    try:
        person = save_person({"full_name": "Diego Teste", "document_id": "77777777777", "site": "Sede"})
        device = save_device({
            "name": "Catraca A", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "xzydsP2011",
        })
        group = api_access_control_save_group(
            AccessGroupRequest(name="Funcionarios", site="Sede", member_ids=[person["id"]])
        )["group"]
        door_group = api_access_control_save_door_group(
            AccessDoorGroupRequest(name="Portoes", site="Sede", device_ids=[device["id"]])
        )["door_group"]
        api_access_control_save_rule(
            AccessRuleRequest(people_group_id=group["id"], door_group_id=door_group["id"])
        )
        novo = save_device({
            "name": "Catraca B", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.34", "username": "admin", "password": "xzydsP2011",
        })
        api_access_control_save_door_group(
            AccessDoorGroupRequest(
                id=door_group["id"], name="Portoes", site="Sede", device_ids=[device["id"], novo["id"]]
            )
        )
        assert (person["id"], novo["id"]) in _pending_pairs()
    finally:
        reset_current_tenant_slug(token)


def main() -> None:
    test_new_routes_registered()
    with tempfile.TemporaryDirectory(prefix="sightops-access-routes-") as tmp:
        db_store.SIGHTOPS_DB_PATH = Path(tmp) / "access.db"
        db_store.init_db()
        test_device_connection_test_updates_status_and_model()
        test_people_list_includes_provision_summary()
        test_manual_person_sync_returns_updated_provision_summary()
        test_normal_setup_order_enqueues_provisioning()
        test_adding_member_to_existing_group_enqueues_provisioning()
        test_new_device_in_door_group_enqueues_provisioning()
    print("OK access control routes: dispositivos, grupos, regras, sync e eventos registrados")


if __name__ == "__main__":
    main()
