from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _prepare_env(tmp: Path) -> None:
    os.environ["DATA_DIR"] = str(tmp)
    os.environ["SIGHTOPS_DB_PATH"] = str(tmp / "sightops.db")
    os.environ["DATABASE_BACKEND"] = "sqlite"
    os.environ.pop("DATABASE_URL", None)


def test_people_are_tenant_scoped() -> None:
    with tempfile.TemporaryDirectory() as d:
        _prepare_env(Path(d))

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import (
            access_control_summary,
            delete_person,
            list_people,
            save_person,
        )

        token_a = set_current_tenant_slug("escola-a")
        try:
            person = save_person(
                {
                    "full_name": "Aluno Teste",
                    "document_id": "11111111111",
                    "person_type": "student",
                    "enrollment_code": "A-001",
                    "guardian_name": "Responsavel",
                    "guardian_phone": "(82) 99999-0000",
                }
            )
            assert access_control_summary()["people_total"] == 1
            assert len(list_people()) == 1
        finally:
            reset_current_tenant_slug(token_a)

        token_b = set_current_tenant_slug("escola-b")
        try:
            assert access_control_summary()["people_total"] == 0
            assert list_people() == []
            assert delete_person(person["id"]) is False
        finally:
            reset_current_tenant_slug(token_b)

        token_a = set_current_tenant_slug("escola-a")
        try:
            assert delete_person(person["id"]) is True
            assert access_control_summary()["people_total"] == 0
        finally:
            reset_current_tenant_slug(token_a)


def test_list_people_filters_by_type() -> None:
    with tempfile.TemporaryDirectory() as d:
        _prepare_env(Path(d))

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import list_people, save_person

        token = set_current_tenant_slug("escola-tipo")
        try:
            save_person({"full_name": "Aluno Um", "document_id": "11111111111", "person_type": "student"})
            save_person({"full_name": "Func Um", "document_id": "22222222222", "person_type": "employee"})
            save_person({"full_name": "Visita Um", "document_id": "33333333333", "person_type": "visitor"})

            assert len(list_people()) == 3
            assert [p["full_name"] for p in list_people(person_type="student")] == ["Aluno Um"]
            assert [p["full_name"] for p in list_people(person_type="employee")] == ["Func Um"]
            assert [p["full_name"] for p in list_people(person_type="visitor")] == ["Visita Um"]
            # valor invalido/desconhecido nao filtra nada (mesmo comportamento de active="")
            assert len(list_people(person_type="bogus")) == 3
        finally:
            reset_current_tenant_slug(token)


def test_list_people_filters_by_site_and_lists_distinct_sites() -> None:
    with tempfile.TemporaryDirectory() as d:
        _prepare_env(Path(d))

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import list_people, list_people_sites, save_person

        token = set_current_tenant_slug("escola-site")
        try:
            save_person({"full_name": "Aluno Sede", "document_id": "11111111111", "site": "Sede"})
            save_person({"full_name": "Aluno Anexo", "document_id": "22222222222", "site": "Anexo"})
            save_person({"full_name": "Aluno Sem Site", "document_id": "33333333333", "site": ""})

            assert list_people_sites() == ["Anexo", "Sede"]
            assert [p["full_name"] for p in list_people(site="Sede")] == ["Aluno Sede"]
            assert [p["full_name"] for p in list_people(site="Anexo")] == ["Aluno Anexo"]
            assert len(list_people(site="")) == 3
            assert len(list_people(site="Inexistente")) == 0
        finally:
            reset_current_tenant_slug(token)


def test_person_stores_controller_id_and_face_photo_path() -> None:
    with tempfile.TemporaryDirectory() as d:
        _prepare_env(Path(d))

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import list_people, save_person, update_person_face_photo

        token = set_current_tenant_slug("escola-face")
        try:
            person = save_person(
                {
                    "full_name": "Elishafan Teste",
                    "document_id": "11111111111",
                    "site": "Sede",
                    "controller_user_id": "1001",
                }
            )
            assert person["controller_user_id"] == "1001"
            assert person["face_photo_path"] == ""
            assert person["face_photo_updated_at"] == ""

            updated = update_person_face_photo(person["id"], "access-control/faces/1001.jpg")
            assert updated["controller_user_id"] == "1001"
            assert updated["face_photo_path"] == "access-control/faces/1001.jpg"
            assert updated["face_photo_updated_at"]
            assert list_people()[0]["face_photo_path"] == "access-control/faces/1001.jpg"
        finally:
            reset_current_tenant_slug(token)


if __name__ == "__main__":
    test_people_are_tenant_scoped()
    test_list_people_filters_by_type()
    test_list_people_filters_by_site_and_lists_distinct_sites()
    test_person_stores_controller_id_and_face_photo_path()
    print("OK access control store")
