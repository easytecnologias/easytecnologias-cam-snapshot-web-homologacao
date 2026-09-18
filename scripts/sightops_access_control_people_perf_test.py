from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_DB_PATH"] = os.path.join(tmp, "sightops-access-control-people-perf.db")
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-access-control-people-perf-test-key"

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import (
            ensure_access_control_schema,
            list_provision_status_for_people,
            save_device,
            save_person,
            upsert_provision_status,
        )

        token = set_current_tenant_slug("escola-a")
        try:
            ensure_access_control_schema()
            people = [
                save_person({
                    "full_name": f"Aluno {idx:03d}",
                    "document_id": f"{idx + 1:011d}",
                    "site": "ESCOLA",
                    "active": True,
                })
                for idx in range(3)
            ]
            device = save_device(
                {
                    "name": "Entrada",
                    "site": "ESCOLA",
                    "host": "10.0.0.10",
                    "username": "admin",
                    "password": "12345678",
                    "access_direction": "entrada",
                    "active": True,
                }
            )
            upsert_provision_status(people[0]["id"], device["id"], "ok")
            upsert_provision_status(people[1]["id"], device["id"], "failed", "Senha invalida")

            grouped = list_provision_status_for_people([p["id"] for p in people])

            assert set(grouped) == {people[0]["id"], people[1]["id"], people[2]["id"]}
            assert grouped[people[0]["id"]][0]["status"] == "ok"
            assert grouped[people[1]["id"]][0]["last_error"] == "Senha invalida"
            assert grouped[people[2]["id"]] == []
        finally:
            reset_current_tenant_slug(token)

    print("access-control people performance regression ok")


if __name__ == "__main__":
    main()
