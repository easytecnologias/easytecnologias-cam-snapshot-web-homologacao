"""Importacao de alunos por planilha.

Cobre o que quebra na vida real com planilha de secretaria: coluna com nome
diferente, matricula que veio como numero do Excel, telefone em formatos
variados, matricula repetida no proprio arquivo, e reimportacao da lista
corrigida -- que precisa atualizar, nao duplicar.

CPF entrou como segunda chave obrigatoria em 2026-09-04, depois de achar em
producao a mesma aluna cadastrada duas vezes (matricula trocou de ano pro
outro, CPF continuou igual, e nada cruzava os dois pra perceber que era a
mesma pessoa). Os casos de CPF ficam nos blocos marcados abaixo.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path


def planilha(linhas: list[list], nome_aba: str = "Alunos") -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    aba = wb.active
    aba.title = nome_aba
    for linha in linhas:
        aba.append(linha)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_DB_PATH"] = os.path.join(tmp, "sightops-import.db")
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-import-test-key"

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_import import analisar_planilha, aplicar_planilha
        from app.services.access_control_store import ensure_access_control_schema, list_people

        token = set_current_tenant_slug("escola-import")
        try:
            ensure_access_control_schema()

            # --- cabecalho com nomes que uma secretaria usaria, fora de ordem,
            #     com acento e maiuscula
            arquivo = planilha([
                ["Turma", "Matrícula", "Nome do Aluno", "Celular do Responsável", "Responsável", "CPF"],
                ["5A", 1577, "ADRIEL FONSECA", "(82) 99999-0000", "MARIA FONSECA", "111.111.111-11"],
                ["5A", 1578, "BRENO ANJOS", "82 98888-7777", "JOSE ANJOS", "222.222.222-22"],
                ["5B", 1579, "CLARA LIMA", "", "ANA LIMA", "333.333.333-33"],   # sem telefone: entra assim mesmo
            ])
            analise = analisar_planilha(arquivo, site="ESCOLA TESTE")
            assert analise["total_linhas"] == 3, analise
            assert len(analise["criar"]) == 3, analise["criar"]
            assert not analise["atualizar"], analise["atualizar"]
            assert not analise["recusados"], analise["recusados"]
            assert analise["sem_telefone"] == 1, analise

            # matricula numerica do Excel nao pode virar "1577.0"
            assert analise["criar"][0]["enrollment_code"] == "1577", analise["criar"][0]
            # telefone normalizado com DDI
            assert analise["criar"][0]["guardian_phone"] == "5582999990000", analise["criar"][0]
            assert analise["criar"][1]["guardian_phone"] == "5582988887777", analise["criar"][1]
            # CPF normalizado -- so digitos, pontuacao da planilha cai fora
            assert analise["criar"][0]["document_id"] == "11111111111", analise["criar"][0]

            # --- analisar nao grava nada
            assert len(list_people()) == 0, list_people()

            resultado = aplicar_planilha(arquivo, site="ESCOLA TESTE")
            assert resultado["criados"] == 3, resultado
            assert resultado["atualizados"] == 0, resultado
            pessoas = list_people()
            assert len(pessoas) == 3, [p["full_name"] for p in pessoas]

            # --- reimportar a lista corrigida: atualiza, nao duplica
            corrigida = planilha([
                ["Matrícula", "Nome do Aluno", "Telefone", "Turma", "CPF"],
                [1577, "ADRIEL FERNANDO FONSECA", "82999990000", "6A", "111.111.111-11"],   # nome e turma corrigidos
                [1579, "CLARA LIMA", "82977776666", "5B", "333.333.333-33"],                # ganhou telefone
                [1580, "DAVI SANTOS", "82966665555", "6A", "444.444.444-44"],               # aluno novo
            ])
            analise2 = analisar_planilha(corrigida, site="ESCOLA TESTE")
            assert len(analise2["atualizar"]) == 2, analise2["atualizar"]
            assert len(analise2["criar"]) == 1, analise2["criar"]

            resultado2 = aplicar_planilha(corrigida, site="ESCOLA TESTE")
            assert resultado2["atualizados"] == 2, resultado2
            assert resultado2["criados"] == 1, resultado2
            pessoas = list_people()
            assert len(pessoas) == 4, [p["full_name"] for p in pessoas]

            por_matricula = {p["enrollment_code"]: p for p in pessoas}
            assert por_matricula["1577"]["full_name"] == "ADRIEL FERNANDO FONSECA"
            assert por_matricula["1577"]["class_name"] == "6A"
            assert por_matricula["1579"]["guardian_phone"] == "5582977776666"

            # --- linhas problematicas sao recusadas, sem derrubar o resto
            suja = planilha([
                ["Matrícula", "Nome", "Telefone", "CPF"],
                [1581, "ELIAS COSTA", "82955554444", "555.555.555-55"],
                [1581, "ELIAS COSTA DUPLICADO", "82944443333", "666.666.666-66"],   # matricula repetida
                ["", "SEM MATRICULA", "82933332222", "777.777.777-77"],            # sem chave
                [1582, "FABIO ROCHA", "123", "888.888.888-88"],                    # telefone impossivel
                [1583, "GUSTAVO SEM CPF", "82922221111", ""],                      # sem CPF
                [1584, "HELENA CPF CURTO", "82911110000", "123"],                  # CPF com menos de 11 digitos
            ])
            analise3 = analisar_planilha(suja)
            assert len(analise3["criar"]) == 1, analise3["criar"]
            assert len(analise3["recusados"]) == 5, analise3["recusados"]
            motivos = " | ".join(r["motivo"] for r in analise3["recusados"])
            assert "repetida" in motivos, motivos
            assert "branco" in motivos, motivos
            assert "telefone invalido" in motivos, motivos
            assert motivos.count("CPF obrigatorio") == 2, motivos  # sem CPF + CPF curto

            # --- planilha sem as colunas obrigatorias e recusada inteira
            try:
                analisar_planilha(planilha([["Turma", "Observacao"], ["5A", "nada"]]))
                raise AssertionError("deveria ter recusado planilha sem matricula/nome/cpf")
            except ValueError as exc:
                assert "matricula" in str(exc), exc
                assert "cpf" in str(exc), exc

            # --- CSV do Excel brasileiro: ponto e virgula e latin-1
            csv = "Matrícula;Nome;Telefone;CPF\n1590;GABRIEL SOUZA;82911112222;999.999.999-99\n".encode("latin-1")
            analise4 = analisar_planilha(csv, nome_arquivo="alunos.csv")
            assert len(analise4["criar"]) == 1, analise4
            assert analise4["criar"][0]["full_name"] == "GABRIEL SOUZA", analise4["criar"][0]

            # ====================================================================
            # --- CPF como segunda chave: cenarios que motivaram a mudanca
            # ====================================================================

            # CPF repetido dentro do MESMO arquivo -- recusa a segunda ocorrencia
            cpf_duplicado_no_arquivo = planilha([
                ["Matrícula", "Nome", "CPF"],
                [2001, "IGOR PRIMEIRO", "121.212.121-21"],
                [2002, "IGOR SEGUNDO", "121.212.121-21"],   # mesmo CPF, matricula diferente
            ])
            analise5 = analisar_planilha(cpf_duplicado_no_arquivo, site="ESCOLA TESTE")
            assert len(analise5["criar"]) == 1, analise5["criar"]
            assert len(analise5["recusados"]) == 1, analise5["recusados"]
            assert "CPF repetido" in analise5["recusados"][0]["motivo"], analise5["recusados"]

            # Mesma pessoa, matricula trocou de ano pro outro, CPF continua igual
            # -- e o bug real que aconteceu em producao (Maria com matriculas
            # 6668 e 496, mesmo CPF, virou duas pessoas). Agora tem que casar
            # pelo CPF e ATUALIZAR a mesma pessoa, nao criar outra.
            matricula_2026 = planilha([
                ["Matrícula", "Nome", "CPF"],
                [3001, "JULIA REMATRICULA", "313.131.313-13"],
            ])
            aplicar_planilha(matricula_2026, site="ESCOLA TESTE")
            antes = {p["document_id"]: p for p in list_people()}["31313131313"]
            assert antes["enrollment_code"] == "3001", antes

            matricula_2027 = planilha([
                ["Matrícula", "Nome", "CPF"],
                [3002, "JULIA REMATRICULA", "313.131.313-13"],   # CPF igual, matricula nova
            ])
            analise6 = analisar_planilha(matricula_2027, site="ESCOLA TESTE")
            assert len(analise6["atualizar"]) == 1, analise6
            assert not analise6["criar"], analise6["criar"]
            assert analise6["atualizar"][0]["id_existente"] == antes["id"], analise6["atualizar"][0]

            aplicar_planilha(matricula_2027, site="ESCOLA TESTE")
            pessoas_julia = [p for p in list_people() if p["document_id"] == "31313131313"]
            assert len(pessoas_julia) == 1, pessoas_julia  # continua sendo UMA pessoa so
            assert pessoas_julia[0]["enrollment_code"] == "3002", pessoas_julia[0]  # matricula atualizada

            # Matricula aponta pra uma pessoa e CPF aponta pra outra -- conflito
            # real (erro de digitacao na planilha), recusa em vez de adivinhar.
            # 1577 e a matricula do Adriel; 222.222.222-22 e o CPF do Breno.
            conflito = planilha([
                ["Matrícula", "Nome", "CPF"],
                [1577, "OUTRO NOME QUALQUER", "222.222.222-22"],
            ])
            analise7 = analisar_planilha(conflito, site="ESCOLA TESTE")
            assert not analise7["criar"], analise7["criar"]
            assert not analise7["atualizar"], analise7["atualizar"]
            assert len(analise7["recusados"]) == 1, analise7["recusados"]
            assert "pessoas diferentes" in analise7["recusados"][0]["motivo"], analise7["recusados"]
        finally:
            reset_current_tenant_slug(token)

    print("access-control import de planilha ok")


if __name__ == "__main__":
    main()
