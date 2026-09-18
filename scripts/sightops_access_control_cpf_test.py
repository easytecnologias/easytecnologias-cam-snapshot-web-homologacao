"""CPF virou a segunda chave de identidade da pessoa, ao lado da matricula.

Motivo: em producao, a mesma aluna (Maria da Paixao Domingos de Oliveira)
apareceu duas vezes -- matricula 496 num ano, 6668 no outro, mesmo CPF --
porque nada cruzava as duas chaves. Sem CPF pra flagrar "e a mesma pessoa",
o sistema so via "matricula diferente" e criava outro cadastro. Aqui cobre:
CPF obrigatorio, CPF nunca repete, e a checagem cruzada matricula x CPF.
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
        os.environ["SIGHTOPS_DB_PATH"] = os.path.join(tmp, "sightops-cpf.db")
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-cpf-test-key"

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import (
            ensure_access_control_schema,
            list_people,
            save_person,
        )

        token = set_current_tenant_slug("escola-cpf")
        try:
            ensure_access_control_schema()

            # --- CPF e obrigatorio, pra qualquer tipo de pessoa
            try:
                save_person({"full_name": "SEM CPF NENHUM"})
                raise AssertionError("deveria ter recusado pessoa sem CPF")
            except ValueError as exc:
                assert "CPF" in str(exc), exc

            try:
                save_person({"full_name": "CPF CURTO", "document_id": "123"})
                raise AssertionError("deveria ter recusado CPF com menos de 11 digitos")
            except ValueError as exc:
                assert "11 digitos" in str(exc), exc

            # --- CPF com pontuacao entra, mas fica gravado so com digitos
            # (pra bater com o que formatDocument() no frontend espera pra
            # mostrar a mascara 999.999.999-99)
            pessoa = save_person({"full_name": "ANA PONTUACAO", "document_id": "111.222.333-44"})
            assert pessoa["document_id"] == "11122233344", pessoa

            # --- editar a MESMA pessoa (mesmo id) com o CPF dela mesma funciona
            editada = save_person({
                "id": pessoa["id"],
                "full_name": "ANA PONTUACAO SILVA",
                "document_id": "111.222.333-44",
            })
            assert editada["id"] == pessoa["id"], editada
            assert len(list_people()) == 1, list_people()

            # --- pessoa B, com id proprio, tentando roubar o CPF da pessoa A
            #     e recusado (mesma logica ja usada pra matricula: id explicito
            #     que nao bate com quem o CPF pertence = conflito de verdade)
            bruno = save_person({"full_name": "BRUNO OUTRO", "document_id": "555.666.777-88"})
            try:
                save_person({
                    "id": bruno["id"],
                    "full_name": "BRUNO TENTANDO ROUBAR CPF",
                    "document_id": "111.222.333-44",   # CPF da Ana
                })
                raise AssertionError("deveria ter recusado CPF de outra pessoa")
            except ValueError as exc:
                assert "ja pertence a outra pessoa" in str(exc), exc
            assert len(list_people()) == 2, list_people()  # nada foi criado/alterado

            # --- SEM id explicito, CPF que ja existe resolve pra quem ja tem
            #     esse CPF (mesmo comportamento ja usado pra matricula --
            #     reimportar/recadastrar com o CPF de alguem que ja existe
            #     atualiza essa pessoa, nao recusa nem cria outra)
            reconhecida = save_person({"full_name": "ANA PONTUACAO ATUALIZADA DE NOVO", "document_id": "111.222.333-44"})
            assert reconhecida["id"] == pessoa["id"], reconhecida
            assert len(list_people()) == 2, list_people()  # continua sendo Ana + Bruno

            # ====================================================================
            # --- o cenario real que motivou a mudanca: matricula trocou, CPF nao
            # ====================================================================
            junho = save_person({
                "full_name": "MARIA DA PAIXAO",
                "enrollment_code": "496",
                "document_id": "589.140.485-00",
            })
            setembro = save_person({
                "full_name": "MARIA DA PAIXAO DOMINGOS DE OLIVEIRA",
                "enrollment_code": "6668",   # matricula nova
                "document_id": "589.140.485-00",   # mesmo CPF
            })
            assert setembro["id"] == junho["id"], (
                "CPF igual tem que reconhecer a MESMA pessoa e atualizar, "
                "nao criar outra -- foi exatamente esse bug em producao"
            )
            assert setembro["enrollment_code"] == "6668", setembro
            pessoas_maria = [p for p in list_people() if p["document_id"] == "58914048500"]
            assert len(pessoas_maria) == 1, pessoas_maria

            # --- matricula de uma pessoa + CPF de outra pessoa = conflito,
            #     recusa em vez de adivinhar quem esta certo
            outra = save_person({
                "full_name": "OUTRA ALUNA",
                "enrollment_code": "7001",
                "document_id": "444.555.666-77",
            })
            try:
                save_person({
                    "full_name": "NOME QUALQUER",
                    "enrollment_code": "6668",          # matricula da Maria
                    "document_id": "444.555.666-77",    # CPF da Outra Aluna
                })
                raise AssertionError("deveria ter recusado matricula/CPF de pessoas diferentes")
            except ValueError as exc:
                assert "pertencem a pessoas diferentes" in str(exc), exc

            # nao deve ter criado nem tocado ninguem
            assert len(list_people()) == 4, [p["full_name"] for p in list_people()]

            # --- visitante e funcionario tambem exigem CPF (regra vale pra
            #     toda pessoa, nao so aluno)
            func = save_person({
                "full_name": "FUNCIONARIO TESTE",
                "person_type": "employee",
                "document_id": "121.212.121-21",
            })
            assert func["document_id"] == "12121212121", func
            try:
                save_person({"full_name": "VISITANTE SEM CPF", "person_type": "visitor"})
                raise AssertionError("visitante tambem precisa de CPF agora")
            except ValueError as exc:
                assert "CPF" in str(exc), exc
        finally:
            reset_current_tenant_slug(token)

    print("access-control CPF como segunda chave ok")


if __name__ == "__main__":
    main()
