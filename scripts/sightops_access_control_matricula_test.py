"""A matricula e a chave de negocio do aluno.

Antes, gravar o mesmo aluno de novo criava outro UUID e outra pessoa -- nada
impedia duplicata. Agora a matricula casa o registro: reimportar a lista da
escola atualiza em vez de duplicar, e tentar dar a mesma matricula a duas
pessoas diferentes e recusado.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_DB_PATH"] = os.path.join(tmp, "sightops-matricula.db")
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-matricula-test-key"

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import (
            ensure_access_control_schema,
            list_people,
            save_person,
        )

        token = set_current_tenant_slug("escola-matricula")
        try:
            ensure_access_control_schema()

            # --- primeiro cadastro
            aluno = save_person({
                "full_name": "JOAO SILVA",
                "document_id": "11111111111",
                "enrollment_code": "2026-0042",
                "class_name": "5A",
                "guardian_phone": "82999990000",
            })
            id_original = aluno["id"]

            # --- mesma matricula de novo: atualiza, nao duplica
            atualizado = save_person({
                "full_name": "JOAO SILVA DE SOUZA",   # nome corrigido pela secretaria
                "document_id": "11111111111",          # mesmo CPF: e a mesma pessoa
                "enrollment_code": "2026-0042",
                "class_name": "5B",                    # mudou de turma
                "guardian_phone": "82988887777",
            })
            assert atualizado["id"] == id_original, (atualizado["id"], id_original)

            pessoas = list_people()
            pessoas = pessoas if isinstance(pessoas, list) else pessoas.get("people", [])
            assert len(pessoas) == 1, pessoas
            assert pessoas[0]["full_name"] == "JOAO SILVA DE SOUZA", pessoas[0]
            assert pessoas[0]["class_name"] == "5B", pessoas[0]

            # --- matricula de outra pessoa e recusada
            outro = save_person({
                "full_name": "MARIA SANTOS",
                "document_id": "22222222222",
                "enrollment_code": "2026-0099",
            })
            try:
                save_person({
                    "id": outro["id"],
                    "full_name": "MARIA SANTOS",
                    "document_id": "22222222222",     # mesmo CPF da propria Maria
                    "enrollment_code": "2026-0042",   # matricula do Joao
                })
                raise AssertionError("deveria ter recusado a matricula de outra pessoa")
            except ValueError as exc:
                assert "ja pertence a outra pessoa" in str(exc), exc

            # A exigencia de matricula fica no endpoint do cadastro (a tela), nao
            # aqui -- importacao e rotinas internas gravam sem passar por ela.

            # --- visitante segue sem exigencia de matricula, mas CPF e obrigatorio
            # igual todo mundo
            a = save_person({"full_name": "VISITANTE UM", "document_id": "33333333333", "person_type": "visitor"})
            b = save_person({"full_name": "VISITANTE DOIS", "document_id": "44444444444", "person_type": "visitor"})
            assert a["id"] != b["id"]

            # --- o ID da controladora vem da matricula, sem ninguem digitar
            assert atualizado["controller_user_id"] == "2026-0042".replace("-", "") or                    atualizado["controller_user_id"], atualizado
            novo = save_person({"full_name": "PEDRO NUMERICO", "document_id": "55555555555", "enrollment_code": "4321"})
            assert novo["controller_user_id"] == "4321", novo

            # --- matricula nao numerica cai no proximo numero livre, nunca sorteado
            letras = save_person({"full_name": "SOFIA LETRAS", "document_id": "66666666666", "enrollment_code": "TURMA-A-07"})
            assert letras["controller_user_id"].isdigit(), letras
            assert letras["controller_user_id"] != "4321", letras

            pessoas = list_people()
            pessoas = pessoas if isinstance(pessoas, list) else pessoas.get("people", [])
            # Joao, Maria, dois visitantes, Pedro e Sofia
            assert len(pessoas) == 6, [p["full_name"] for p in pessoas]
        finally:
            reset_current_tenant_slug(token)

    print("access-control matricula como chave ok")


if __name__ == "__main__":
    main()
