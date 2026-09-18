from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.core.crypto import decrypt, encrypt
from app.services import db_store


def _clean_text(value: Any, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def _clean_phone(value: Any) -> str:
    raw = str(value or "").strip()
    return re.sub(r"[^\d+]", "", raw)[:32]


def _clean_cpf(value: Any) -> str:
    """So digitos -- bate com o que formatDocument() no frontend espera pra
    aplicar a mascara 999.999.999-99 na exibicao."""
    return re.sub(r"\D", "", str(value or ""))[:11]


def _bool_int(value: Any, default: bool = True) -> int:
    if value is None:
        return 1 if default else 0
    if isinstance(value, str):
        return 0 if value.strip().lower() in {"0", "false", "nao", "não", "no", "off"} else 1
    return 1 if bool(value) else 0


def normalize_access_direction(value: Any) -> str:
    clean = _clean_text(value, 32).lower()
    return clean if clean in {"entrada", "saida", "entrada_saida"} else "entrada"


def normalize_access_event_type(value: Any) -> str:
    clean = _clean_text(value, 32).lower()
    return clean if clean in {"entrada", "saida", "saida_manual"} else "entrada"


def _today_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# Colunas acrescentadas a tabelas que JA EXISTIAM antes deste plano
# (access_people e access_devices ja rodavam em homologacao). CREATE TABLE IF
# NOT EXISTS e no-op quando a tabela existe, entao sem esta migration aditiva
# o banco antigo nunca ganharia essas colunas e o primeiro list_people()/
# list_devices() quebraria com "no such column: site".
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("access_people", "site", "TEXT NOT NULL DEFAULT ''"),
    ("access_people", "controller_user_id", "TEXT NOT NULL DEFAULT ''"),
    ("access_people", "face_photo_path", "TEXT NOT NULL DEFAULT ''"),
    ("access_people", "face_photo_updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("access_devices", "password_enc", "TEXT NOT NULL DEFAULT ''"),
    ("access_devices", "status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("access_devices", "last_seen_at", "TEXT NOT NULL DEFAULT ''"),
    ("access_devices", "last_event_id", "TEXT NOT NULL DEFAULT ''"),
    ("access_devices", "access_direction", "TEXT NOT NULL DEFAULT 'entrada'"),
    ("access_events", "source", "TEXT NOT NULL DEFAULT 'device'"),
    ("access_events", "device_name", "TEXT NOT NULL DEFAULT ''"),
    ("access_events", "device_role", "TEXT NOT NULL DEFAULT 'entrada'"),
    ("access_events", "operator_user", "TEXT NOT NULL DEFAULT ''"),
    ("access_events", "manual_reason", "TEXT NOT NULL DEFAULT ''"),
    ("access_events", "notification_status", "TEXT NOT NULL DEFAULT ''"),
    ("access_events", "raw_event_id", "TEXT NOT NULL DEFAULT ''"),
    ("access_events", "raw_payload", "TEXT NOT NULL DEFAULT ''"),
    ("access_rules", "name", "TEXT NOT NULL DEFAULT ''"),
)


def _table_exists(c: Any, backend: str, table: str) -> bool:
    try:
        if str(backend or "sqlite").strip().lower() == "postgres":
            query = (
                "SELECT 1 AS ok FROM information_schema.tables "
                "WHERE table_name = ? AND table_schema NOT IN ('pg_catalog', 'information_schema')"
            )
        else:
            query = "SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = ?"
        return c.execute(db_store._sql_for_backend(backend, query), (table,)).fetchone() is not None
    except Exception:
        return False


def _column_exists(c: Any, backend: str, table: str, column: str) -> bool:
    """Mesma convencao de db_store (_sqlite_columns no SQLite, information_schema no Postgres)."""
    try:
        if str(backend or "sqlite").strip().lower() == "postgres":
            query = "SELECT 1 AS ok FROM information_schema.columns WHERE table_name = ? AND column_name = ?"
            return c.execute(db_store._sql_for_backend(backend, query), (table, column)).fetchone() is not None
        return column in db_store._sqlite_columns(c, table)
    except Exception:
        return False


def _apply_additive_columns(c: Any, backend: str) -> None:
    """ALTER TABLE ... ADD COLUMN guardado, para bancos que ja tinham as tabelas.

    Roda ANTES do script de CREATE TABLE/CREATE INDEX de proposito: os indices
    idx_access_people_tenant_site / idx_access_devices_tenant_site referenciam
    justamente as colunas novas, entao num banco antigo o CREATE INDEX
    estouraria ("no such column: site") antes de qualquer ALTER que viesse
    depois. Instalacao nova cai no guard de tabela inexistente e e atendida
    normalmente pelo CREATE TABLE IF NOT EXISTS logo abaixo, que ja traz todas
    as colunas.
    """
    is_postgres = str(backend or "sqlite").strip().lower() == "postgres"
    for table, column, column_ddl in _ADDITIVE_COLUMNS:
        if not _table_exists(c, backend, table):
            continue
        if _column_exists(c, backend, table, column):
            continue
        # table/column/column_ddl vem so da constante acima, nunca de input.
        # No Postgres ainda usamos IF NOT EXISTS como segunda rede: a consulta a
        # information_schema nao filtra por schema, entao a checagem acima pode
        # errar num banco com search_path incomum -- e ai o ALTER cru abortaria
        # o ensure_access_control_schema inteiro. SQLite nao aceita essa clausula.
        if_not_exists = "IF NOT EXISTS " if is_postgres else ""
        c.execute(f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{column} {column_ddl}")


def ensure_access_control_schema() -> None:
    backend = db_store._db_backend()
    with db_store._conn() as c:
        _apply_additive_columns(c, backend)
        db_store._exec_many_statements(
            c,
            backend,
            """
            CREATE TABLE IF NOT EXISTS access_people (
              id TEXT PRIMARY KEY,
              tenant_slug TEXT NOT NULL,
              full_name TEXT NOT NULL,
              person_type TEXT NOT NULL DEFAULT 'student',
              document_id TEXT NOT NULL DEFAULT '',
              enrollment_code TEXT NOT NULL DEFAULT '',
              class_name TEXT NOT NULL DEFAULT '',
              site TEXT NOT NULL DEFAULT '',
              controller_user_id TEXT NOT NULL DEFAULT '',
              face_photo_path TEXT NOT NULL DEFAULT '',
              face_photo_updated_at TEXT NOT NULL DEFAULT '',
              guardian_name TEXT NOT NULL DEFAULT '',
              guardian_phone TEXT NOT NULL DEFAULT '',
              whatsapp_enabled INTEGER NOT NULL DEFAULT 1,
              active INTEGER NOT NULL DEFAULT 1,
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_access_people_tenant_name
              ON access_people(tenant_slug, full_name);
            CREATE INDEX IF NOT EXISTS idx_access_people_tenant_status
              ON access_people(tenant_slug, active, person_type);
            CREATE INDEX IF NOT EXISTS idx_access_people_tenant_site
              ON access_people(tenant_slug, site);
            -- A matricula e a chave de negocio do aluno: e o que a escola usa e
            -- o que permite reimportar a lista sem duplicar ninguem. O id
            -- interno continua sendo UUID, mas nao e mais o criterio de
            -- duplicidade. Matricula vazia fica fora do indice (WHERE), porque
            -- cadastro sem matricula ainda e permitido.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_access_people_tenant_enrollment
              ON access_people(tenant_slug, enrollment_code)
              WHERE enrollment_code <> '';
            -- CPF virou obrigatorio e unico em 2026-09-04 (save_person ja
            -- recusa gravar sem CPF ou com CPF de outra pessoa). Registro
            -- antigo sem CPF continua existindo -- WHERE document_id <> ''
            -- deixa esses de fora do indice, so trava duplicata nova.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_access_people_tenant_document
              ON access_people(tenant_slug, document_id)
              WHERE document_id <> '';

            CREATE TABLE IF NOT EXISTS access_devices (
              id TEXT PRIMARY KEY,
              tenant_slug TEXT NOT NULL,
              site TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL,
              vendor TEXT NOT NULL DEFAULT '',
              model TEXT NOT NULL DEFAULT '',
              host TEXT NOT NULL DEFAULT '',
              connector_id TEXT NOT NULL DEFAULT '',
              username TEXT NOT NULL DEFAULT '',
              password_enc TEXT NOT NULL DEFAULT '',
              access_direction TEXT NOT NULL DEFAULT 'entrada',
              status TEXT NOT NULL DEFAULT 'unknown',
              last_seen_at TEXT NOT NULL DEFAULT '',
              last_event_id TEXT NOT NULL DEFAULT '',
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_access_devices_tenant_site
              ON access_devices(tenant_slug, site, active);

            CREATE TABLE IF NOT EXISTS access_groups (
              id TEXT PRIMARY KEY,
              tenant_slug TEXT NOT NULL,
              site TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_access_groups_tenant
              ON access_groups(tenant_slug, site, active);

            CREATE TABLE IF NOT EXISTS access_group_members (
              tenant_slug TEXT NOT NULL,
              group_id TEXT NOT NULL,
              person_id TEXT NOT NULL,
              PRIMARY KEY (tenant_slug, group_id, person_id)
            );
            CREATE INDEX IF NOT EXISTS idx_access_group_members_person
              ON access_group_members(tenant_slug, person_id);

            CREATE TABLE IF NOT EXISTS access_door_groups (
              id TEXT PRIMARY KEY,
              tenant_slug TEXT NOT NULL,
              site TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_access_door_groups_tenant
              ON access_door_groups(tenant_slug, site, active);

            CREATE TABLE IF NOT EXISTS access_door_group_members (
              tenant_slug TEXT NOT NULL,
              door_group_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              PRIMARY KEY (tenant_slug, door_group_id, device_id)
            );
            CREATE INDEX IF NOT EXISTS idx_access_door_group_members_device
              ON access_door_group_members(tenant_slug, device_id);

            CREATE TABLE IF NOT EXISTS access_rules (
              id TEXT PRIMARY KEY,
              tenant_slug TEXT NOT NULL,
              people_group_id TEXT NOT NULL,
              door_group_id TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              weekdays TEXT NOT NULL DEFAULT '1234567',
              time_start TEXT NOT NULL DEFAULT '',
              time_end TEXT NOT NULL DEFAULT '',
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_access_rules_tenant
              ON access_rules(tenant_slug, active);

            CREATE TABLE IF NOT EXISTS access_events (
              id TEXT PRIMARY KEY,
              tenant_slug TEXT NOT NULL,
              site TEXT NOT NULL DEFAULT '',
              device_id TEXT NOT NULL,
              person_id TEXT NOT NULL DEFAULT '',
              person_name_raw TEXT NOT NULL DEFAULT '',
              event_type TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'device',
              device_name TEXT NOT NULL DEFAULT '',
              device_role TEXT NOT NULL DEFAULT 'entrada',
              operator_user TEXT NOT NULL DEFAULT '',
              manual_reason TEXT NOT NULL DEFAULT '',
              notification_status TEXT NOT NULL DEFAULT '',
              raw_event_id TEXT NOT NULL DEFAULT '',
              raw_payload TEXT NOT NULL DEFAULT '',
              occurred_at TEXT NOT NULL,
              synced_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_access_events_tenant_time
              ON access_events(tenant_slug, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_access_events_person
              ON access_events(tenant_slug, person_id, occurred_at DESC);

            CREATE TABLE IF NOT EXISTS access_provision_status (
              tenant_slug TEXT NOT NULL,
              person_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              last_error TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (tenant_slug, person_id, device_id)
            );
            CREATE INDEX IF NOT EXISTS idx_access_provision_status_pending
              ON access_provision_status(tenant_slug, status);
            """,
        )


def _row_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    data["whatsapp_enabled"] = bool(int(data.get("whatsapp_enabled") or 0))
    data["active"] = bool(int(data.get("active") or 0))
    return data


def list_people(search: str = "", active: str = "", person_type: str = "", site: str = "") -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    term = _clean_text(search, 120).lower()
    active_filter = str(active or "").strip().lower()
    type_filter = _clean_text(person_type, 32).lower()
    site_filter = _clean_text(site, 120)
    where = ["tenant_slug = ?"]
    params: list[Any] = [tenant]
    if term:
        like = f"%{term}%"
        where.append(
            "(lower(full_name) LIKE ? OR lower(document_id) LIKE ? OR lower(enrollment_code) LIKE ? OR lower(guardian_phone) LIKE ?)"
        )
        params.extend([like, like, like, like])
    if active_filter in {"1", "true", "active", "ativo"}:
        where.append("active = 1")
    elif active_filter in {"0", "false", "inactive", "inativo"}:
        where.append("active = 0")
    if type_filter in {"student", "employee", "visitor"}:
        where.append("person_type = ?")
        params.append(type_filter)
    if site_filter:
        where.append("site = ?")
        params.append(site_filter)

    with db_store._conn() as c:
        rows = c.execute(
            f"""
            SELECT id, tenant_slug, full_name, person_type, document_id, enrollment_code,
                   class_name, site, controller_user_id, face_photo_path, face_photo_updated_at,
                   guardian_name, guardian_phone, whatsapp_enabled, active, notes, created_at, updated_at
            FROM access_people
            WHERE {' AND '.join(where)}
            ORDER BY active DESC, full_name COLLATE NOCASE
            """,
            tuple(params),
        ).fetchall()
    return [_row_dict(r) for r in rows]


def list_people_sites() -> List[str]:
    """Sites disponiveis para escolher no cadastro.

    A fonte principal sao as CONTROLADORAS: e o site do dispositivo que vai no
    evento e decide por qual canal a notificacao sai. Se a pessoa ficar num site
    que nenhuma controladora usa, o aviso nunca casa.

    Os sites ja gravados em pessoas entram junto para nao sumirem da lista
    enquanto o cadastro nao for ajustado.
    """
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    with db_store._conn() as c:
        dos_dispositivos = c.execute(
            "SELECT DISTINCT site FROM access_devices WHERE tenant_slug = ? AND site != ''",
            (tenant,),
        ).fetchall()
        das_pessoas = c.execute(
            "SELECT DISTINCT site FROM access_people WHERE tenant_slug = ? AND site != ''",
            (tenant,),
        ).fetchall()
    sites = {dict(r)["site"] for r in dos_dispositivos} | {dict(r)["site"] for r in das_pessoas}
    return sorted(sites, key=lambda s: s.casefold())


def _proximo_id_controladora(tenant: str, enrollment_code: str) -> str:
    """Numero do usuario no equipamento, derivado da matricula.

    A matricula e numerica e unica, entao serve direto. Quando nao for (ou ja
    estiver em uso por outra pessoa), pega o proximo numero livre -- nunca
    sorteia, porque numero sorteado pode cair em cima de alguem ja cadastrado
    na controladora, e ai o reconhecimento aponta para o aluno errado.
    """
    if enrollment_code.isdigit() and enrollment_code.lstrip("0"):
        with db_store._conn() as conn:
            usado = conn.execute(
                "SELECT 1 FROM access_people WHERE tenant_slug=? AND controller_user_id=?",
                (tenant, enrollment_code),
            ).fetchone()
        if not usado:
            return enrollment_code[:32]

    with db_store._conn() as conn:
        linhas = conn.execute(
            "SELECT controller_user_id FROM access_people WHERE tenant_slug=? AND controller_user_id<>''",
            (tenant,),
        ).fetchall()
    usados = {int(dict(r)["controller_user_id"]) for r in linhas
              if str(dict(r)["controller_user_id"]).isdigit()}
    proximo = max(usados) + 1 if usados else 1
    return str(proximo)


def save_person(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    person_id = _clean_text(payload.get("id"), 80)
    full_name = _clean_text(payload.get("full_name") or payload.get("name"), 160)
    if not full_name:
        raise ValueError("Informe o nome da pessoa.")
    person_type = _clean_text(payload.get("person_type") or "student", 32).lower() or "student"
    enrollment_code = _clean_text(payload.get("enrollment_code"), 64)

    # CPF e obrigatorio e nunca pode se repetir: e o unico identificador que
    # nao muda se a escola trocar a matricula do aluno de um ano pro outro
    # (foi exatamente essa troca que duplicou uma aluna em producao antes
    # desta checagem existir -- mesma pessoa, duas matriculas, sem CPF pra
    # flagrar que era a mesma gente).
    document_id = _clean_cpf(payload.get("document_id"))
    if not document_id:
        raise ValueError("Informe o CPF da pessoa.")
    if len(document_id) != 11:
        raise ValueError(f"CPF invalido: precisa ter 11 digitos ({document_id!r}).")

    # A matricula identifica o aluno para a escola. Sem casar por ela, gravar o
    # mesmo aluno de novo criaria outro UUID e outra pessoa -- que e o que
    # acontecia antes. Assim, reimportar a lista atualiza em vez de duplicar.
    existente_por_matricula = None
    if enrollment_code:
        with db_store._conn() as conn:
            achado = conn.execute(
                "SELECT id FROM access_people WHERE tenant_slug=? AND enrollment_code=?",
                (tenant, enrollment_code),
            ).fetchone()
        if achado:
            existente_por_matricula = dict(achado)["id"]
            if person_id and person_id != existente_por_matricula:
                raise ValueError(
                    f"A matricula {enrollment_code} ja pertence a outra pessoa neste cliente."
                )

    # CPF e a segunda chave de identidade, agora obrigatoria em toda pessoa --
    # casa por ele tambem pra pegar o caso "matricula trocou, pessoa e a
    # mesma" em vez de criar duplicata.
    existente_por_cpf = None
    with db_store._conn() as conn:
        achado_cpf = conn.execute(
            "SELECT id FROM access_people WHERE tenant_slug=? AND document_id=?",
            (tenant, document_id),
        ).fetchone()
    if achado_cpf:
        existente_por_cpf = dict(achado_cpf)["id"]
        if person_id and person_id != existente_por_cpf:
            raise ValueError(f"O CPF {document_id} ja pertence a outra pessoa neste cliente.")

    # Matricula e CPF apontando pra pessoas EXISTENTES diferentes = dado
    # inconsistente (planilha com erro de digitacao, por exemplo). Nao
    # adivinha qual esta certo -- recusa e deixa o humano corrigir.
    if (
        existente_por_matricula
        and existente_por_cpf
        and existente_por_matricula != existente_por_cpf
    ):
        raise ValueError(
            f"A matricula {enrollment_code} e o CPF {document_id} "
            "pertencem a pessoas diferentes neste cliente."
        )

    person_id = person_id or existente_por_matricula or existente_por_cpf or uuid.uuid4().hex
    class_name = _clean_text(payload.get("class_name"), 80)
    site = _clean_text(payload.get("site"), 120)
    controller_user_id = re.sub(r"\D", "", str(payload.get("controller_user_id") or ""))[:32]
    if not controller_user_id:
        # Uma identidade so, do cadastro a catraca: o aluno de matricula 1577 e
        # o usuario 1577 no equipamento. Sorteie um numero e voce perde a
        # rastreabilidade e arrisca colidir com quem ja existe no dispositivo.
        controller_user_id = _proximo_id_controladora(tenant, enrollment_code)
    guardian_name = _clean_text(payload.get("guardian_name"), 160)
    guardian_phone = _clean_phone(payload.get("guardian_phone"))
    whatsapp_enabled = _bool_int(payload.get("whatsapp_enabled"), True)
    active = _bool_int(payload.get("active"), True)
    notes = _clean_text(payload.get("notes"), 500)

    with db_store._conn() as c:
        c.execute(
            """
            INSERT INTO access_people(
              id, tenant_slug, full_name, person_type, document_id, enrollment_code,
              class_name, site, controller_user_id, guardian_name, guardian_phone,
              whatsapp_enabled, active, notes, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
              full_name=excluded.full_name,
              person_type=excluded.person_type,
              document_id=excluded.document_id,
              enrollment_code=excluded.enrollment_code,
              class_name=excluded.class_name,
              site=excluded.site,
              controller_user_id=excluded.controller_user_id,
              guardian_name=excluded.guardian_name,
              guardian_phone=excluded.guardian_phone,
              whatsapp_enabled=excluded.whatsapp_enabled,
              active=excluded.active,
              notes=excluded.notes,
              updated_at=datetime('now')
            WHERE access_people.tenant_slug=excluded.tenant_slug
            """,
            (
                person_id,
                tenant,
                full_name,
                person_type,
                document_id,
                enrollment_code,
                class_name,
                site,
                controller_user_id,
                guardian_name,
                guardian_phone,
                whatsapp_enabled,
                active,
                notes,
            ),
        )
        row = c.execute(
            """
            SELECT id, tenant_slug, full_name, person_type, document_id, enrollment_code,
                   class_name, site, controller_user_id, face_photo_path, face_photo_updated_at,
                   guardian_name, guardian_phone, whatsapp_enabled, active, notes, created_at, updated_at
            FROM access_people
            WHERE tenant_slug=? AND id=?
            """,
            (tenant, person_id),
        ).fetchone()
    if row is None:
        raise ValueError("Pessoa nao encontrada neste cliente.")
    return _row_dict(row)


def update_person_face_photo(person_id: str, face_photo_path: str) -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    pid = _clean_text(person_id, 80)
    clean_path = _clean_text(face_photo_path, 500)
    if not pid or not clean_path:
        raise ValueError("Pessoa ou foto facial invalida.")
    with db_store._conn() as c:
        c.execute(
            """
            UPDATE access_people
            SET face_photo_path=?, face_photo_updated_at=datetime('now'), updated_at=datetime('now')
            WHERE tenant_slug=? AND id=?
            """,
            (clean_path, tenant, pid),
        )
        row = c.execute(
            """
            SELECT id, tenant_slug, full_name, person_type, document_id, enrollment_code,
                   class_name, site, controller_user_id, face_photo_path, face_photo_updated_at,
                   guardian_name, guardian_phone, whatsapp_enabled, active, notes, created_at, updated_at
            FROM access_people
            WHERE tenant_slug=? AND id=?
            """,
            (tenant, pid),
        ).fetchone()
    if row is None:
        raise ValueError("Pessoa nao encontrada neste cliente.")
    return _row_dict(row)


def delete_person(person_id: str) -> bool:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    pid = _clean_text(person_id, 80)
    if not pid:
        return False
    with db_store._conn() as c:
        cur = c.execute("DELETE FROM access_people WHERE tenant_slug=? AND id=?", (tenant, pid))
        return int(cur.rowcount or 0) > 0


def access_control_summary() -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    today = _today_prefix()
    with db_store._conn() as c:
        people = c.execute(
            """
            SELECT
              COUNT(1) AS total,
              SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active_total,
              SUM(CASE WHEN person_type='student' THEN 1 ELSE 0 END) AS students
            FROM access_people
            WHERE tenant_slug=?
            """,
            (tenant,),
        ).fetchone()
        devices = c.execute(
            """
            SELECT
              COUNT(1) AS total,
              SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active_total
            FROM access_devices
            WHERE tenant_slug=?
            """,
            (tenant,),
        ).fetchone()
        events = c.execute(
            """
            SELECT
              COUNT(1) AS total,
              SUM(CASE WHEN event_type='entrada' THEN 1 ELSE 0 END) AS entries,
              SUM(CASE WHEN event_type='saida' THEN 1 ELSE 0 END) AS exits,
              SUM(CASE WHEN event_type='saida_manual' THEN 1 ELSE 0 END) AS manual_exits
            FROM access_events
            WHERE tenant_slug=? AND occurred_at LIKE ?
            """,
            (tenant, f"{today}%"),
        ).fetchone()
    people_data = dict(people) if people else {}
    devices_data = dict(devices) if devices else {}
    events_data = dict(events) if events else {}
    presence = access_presence_summary()
    return {
        "people_total": int(people_data.get("total") or 0),
        "people_active": int(people_data.get("active_total") or 0),
        "students": int(people_data.get("students") or 0),
        "devices_total": int(devices_data.get("total") or 0),
        "devices_active": int(devices_data.get("active_total") or 0),
        "events_today": int(events_data.get("total") or 0),
        "entries_today": int(events_data.get("entries") or 0),
        "exits_today": int(events_data.get("exits") or 0),
        "manual_exits_today": int(events_data.get("manual_exits") or 0),
        "inside_now": int(presence.get("inside_now") or 0),
        "whatsapp_queue": 0,
    }


def list_groups(site: str = "") -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    where = ["tenant_slug = ?"]
    params: list[Any] = [tenant]
    if site:
        where.append("site = ?")
        params.append(_clean_text(site, 120))
    with db_store._conn() as c:
        rows = c.execute(
            f"SELECT * FROM access_groups WHERE {' AND '.join(where)} ORDER BY active DESC, name COLLATE NOCASE",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def save_group(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    group_id = _clean_text(payload.get("id"), 80) or uuid.uuid4().hex
    name = _clean_text(payload.get("name"), 160)
    if not name:
        raise ValueError("Informe o nome do grupo.")
    site = _clean_text(payload.get("site"), 120)
    active = _bool_int(payload.get("active"), True)
    with db_store._conn() as c:
        c.execute(
            """
            INSERT INTO access_groups(id, tenant_slug, site, name, active, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
              site=excluded.site, name=excluded.name, active=excluded.active, updated_at=datetime('now')
            WHERE access_groups.tenant_slug=excluded.tenant_slug
            """,
            (group_id, tenant, site, name, active),
        )
        row = c.execute("SELECT * FROM access_groups WHERE tenant_slug=? AND id=?", (tenant, group_id)).fetchone()
    if row is None:
        raise ValueError("Grupo nao encontrado neste cliente.")
    return dict(row)


def delete_group(group_id: str) -> bool:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    gid = _clean_text(group_id, 80)
    if not gid:
        return False
    with db_store._conn() as c:
        cur = c.execute("DELETE FROM access_groups WHERE tenant_slug=? AND id=?", (tenant, gid))
        c.execute("DELETE FROM access_group_members WHERE tenant_slug=? AND group_id=?", (tenant, gid))
        return int(cur.rowcount or 0) > 0


def set_group_members(group_id: str, person_ids: List[str]) -> None:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    gid = _clean_text(group_id, 80)
    with db_store._conn() as c:
        c.execute("DELETE FROM access_group_members WHERE tenant_slug=? AND group_id=?", (tenant, gid))
        for pid in person_ids:
            clean_pid = _clean_text(pid, 80)
            if clean_pid:
                c.execute(
                    "INSERT INTO access_group_members(tenant_slug, group_id, person_id) VALUES(?, ?, ?) "
                    "ON CONFLICT(tenant_slug, group_id, person_id) DO NOTHING",
                    (tenant, gid, clean_pid),
                )


def list_group_members(group_id: str) -> List[str]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    gid = _clean_text(group_id, 80)
    with db_store._conn() as c:
        rows = c.execute(
            "SELECT person_id FROM access_group_members WHERE tenant_slug=? AND group_id=?", (tenant, gid)
        ).fetchall()
    return [r["person_id"] for r in rows]


def list_door_groups(site: str = "") -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    where = ["tenant_slug = ?"]
    params: list[Any] = [tenant]
    if site:
        where.append("site = ?")
        params.append(_clean_text(site, 120))
    with db_store._conn() as c:
        rows = c.execute(
            f"SELECT * FROM access_door_groups WHERE {' AND '.join(where)} ORDER BY active DESC, name COLLATE NOCASE",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def save_door_group(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    door_group_id = _clean_text(payload.get("id"), 80) or uuid.uuid4().hex
    name = _clean_text(payload.get("name"), 160)
    if not name:
        raise ValueError("Informe o nome do grupo de portas.")
    site = _clean_text(payload.get("site"), 120)
    active = _bool_int(payload.get("active"), True)
    with db_store._conn() as c:
        c.execute(
            """
            INSERT INTO access_door_groups(id, tenant_slug, site, name, active, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
              site=excluded.site, name=excluded.name, active=excluded.active, updated_at=datetime('now')
            WHERE access_door_groups.tenant_slug=excluded.tenant_slug
            """,
            (door_group_id, tenant, site, name, active),
        )
        row = c.execute(
            "SELECT * FROM access_door_groups WHERE tenant_slug=? AND id=?", (tenant, door_group_id)
        ).fetchone()
    if row is None:
        raise ValueError("Grupo de portas nao encontrado neste cliente.")
    return dict(row)


def delete_door_group(door_group_id: str) -> bool:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(door_group_id, 80)
    if not did:
        return False
    with db_store._conn() as c:
        cur = c.execute("DELETE FROM access_door_groups WHERE tenant_slug=? AND id=?", (tenant, did))
        c.execute("DELETE FROM access_door_group_members WHERE tenant_slug=? AND door_group_id=?", (tenant, did))
        return int(cur.rowcount or 0) > 0


def set_door_group_members(door_group_id: str, device_ids: List[str]) -> None:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(door_group_id, 80)
    with db_store._conn() as c:
        c.execute("DELETE FROM access_door_group_members WHERE tenant_slug=? AND door_group_id=?", (tenant, did))
        for dev_id in device_ids:
            clean_dev = _clean_text(dev_id, 80)
            if clean_dev:
                c.execute(
                    "INSERT INTO access_door_group_members(tenant_slug, door_group_id, device_id) VALUES(?, ?, ?) "
                    "ON CONFLICT(tenant_slug, door_group_id, device_id) DO NOTHING",
                    (tenant, did, clean_dev),
                )


def list_door_group_members(door_group_id: str) -> List[str]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(door_group_id, 80)
    with db_store._conn() as c:
        rows = c.execute(
            "SELECT device_id FROM access_door_group_members WHERE tenant_slug=? AND door_group_id=?", (tenant, did)
        ).fetchall()
    return [r["device_id"] for r in rows]


def list_rules() -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    with db_store._conn() as c:
        rows = c.execute(
            "SELECT * FROM access_rules WHERE tenant_slug=? ORDER BY active DESC, created_at",
            (tenant,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_rule(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    rule_id = _clean_text(payload.get("id"), 80) or uuid.uuid4().hex
    people_group_id = _clean_text(payload.get("people_group_id"), 80)
    door_group_id = _clean_text(payload.get("door_group_id"), 80)
    if not people_group_id or not door_group_id:
        raise ValueError("Informe o grupo de pessoas e o grupo de portas.")
    name = _clean_text(payload.get("name"), 120)
    weekdays = re.sub(r"[^1-7]", "", str(payload.get("weekdays") or "1234567")) or "1234567"
    time_start = _clean_text(payload.get("time_start"), 5)
    time_end = _clean_text(payload.get("time_end"), 5)
    active = _bool_int(payload.get("active"), True)
    with db_store._conn() as c:
        c.execute(
            """
            INSERT INTO access_rules(
              id, tenant_slug, people_group_id, door_group_id, name, weekdays, time_start, time_end,
              active, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
              people_group_id=excluded.people_group_id,
              door_group_id=excluded.door_group_id,
              name=excluded.name,
              weekdays=excluded.weekdays,
              time_start=excluded.time_start,
              time_end=excluded.time_end,
              active=excluded.active,
              updated_at=datetime('now')
            WHERE access_rules.tenant_slug=excluded.tenant_slug
            """,
            (rule_id, tenant, people_group_id, door_group_id, name, weekdays, time_start, time_end, active),
        )
        row = c.execute("SELECT * FROM access_rules WHERE tenant_slug=? AND id=?", (tenant, rule_id)).fetchone()
    if row is None:
        raise ValueError("Regra nao encontrada neste cliente.")
    return dict(row)


def delete_rule(rule_id: str) -> bool:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    rid = _clean_text(rule_id, 80)
    if not rid:
        return False
    with db_store._conn() as c:
        cur = c.execute("DELETE FROM access_rules WHERE tenant_slug=? AND id=?", (tenant, rid))
        return int(cur.rowcount or 0) > 0


def _device_row_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    data.pop("password_enc", None)
    data["active"] = bool(int(data.get("active") or 0))
    return data


def list_devices(site: str = "") -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    where = ["tenant_slug = ?"]
    params: list[Any] = [tenant]
    if site:
        where.append("site = ?")
        params.append(_clean_text(site, 120))
    with db_store._conn() as c:
        rows = c.execute(
            f"SELECT * FROM access_devices WHERE {' AND '.join(where)} ORDER BY active DESC, name COLLATE NOCASE",
            tuple(params),
        ).fetchall()
    return [_device_row_dict(r) for r in rows]


def update_device_health(device_id: str, *, status: str, model: str = "", last_seen_at: str = "") -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(device_id, 80)
    clean_status = _clean_text(status, 20) or "unknown"
    clean_model = _clean_text(model, 60)
    clean_seen = _clean_text(last_seen_at, 40)
    with db_store._conn() as c:
        if clean_model:
            c.execute(
                """
                UPDATE access_devices
                SET status=?, model=?, last_seen_at=?, updated_at=datetime('now')
                WHERE tenant_slug=? AND id=?
                """,
                (clean_status, clean_model, clean_seen, tenant, did),
            )
        else:
            c.execute(
                """
                UPDATE access_devices
                SET status=?, last_seen_at=?, updated_at=datetime('now')
                WHERE tenant_slug=? AND id=?
                """,
                (clean_status, clean_seen, tenant, did),
            )
        row = c.execute("SELECT * FROM access_devices WHERE tenant_slug=? AND id=?", (tenant, did)).fetchone()
    if row is None:
        raise ValueError("Dispositivo nao encontrado neste cliente.")
    return _device_row_dict(row)


def update_device_event_cursor(device_id: str, last_event_id: str) -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(device_id, 80)
    cursor = _clean_text(last_event_id, 120)
    with db_store._conn() as c:
        c.execute(
            """
            UPDATE access_devices
            SET last_event_id=?, updated_at=datetime('now')
            WHERE tenant_slug=? AND id=?
            """,
            (cursor, tenant, did),
        )
        row = c.execute("SELECT * FROM access_devices WHERE tenant_slug=? AND id=?", (tenant, did)).fetchone()
    if row is None:
        raise ValueError("Dispositivo nao encontrado neste cliente.")
    return _device_row_dict(row)


def save_device(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    device_id = _clean_text(payload.get("id"), 80) or uuid.uuid4().hex
    name = _clean_text(payload.get("name"), 160)
    if not name:
        raise ValueError("Informe o nome do dispositivo.")
    site = _clean_text(payload.get("site"), 120)
    vendor = _clean_text(payload.get("vendor"), 60)
    model = _clean_text(payload.get("model"), 60)
    host = _clean_text(payload.get("host"), 120)
    connector_id = _clean_text(payload.get("connector_id"), 80)
    username = _clean_text(payload.get("username"), 80)
    access_direction = normalize_access_direction(payload.get("access_direction"))
    active = _bool_int(payload.get("active"), True)
    raw_password = payload.get("password")
    with db_store._conn() as c:
        if raw_password:
            password_enc = encrypt(str(raw_password))
            c.execute(
                """
                INSERT INTO access_devices(
                  id, tenant_slug, site, name, vendor, model, host, connector_id, username,
                  password_enc, access_direction, active, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(id) DO UPDATE SET
                  site=excluded.site, name=excluded.name, vendor=excluded.vendor, model=excluded.model,
                  host=excluded.host, connector_id=excluded.connector_id, username=excluded.username,
                  password_enc=excluded.password_enc, access_direction=excluded.access_direction,
                  active=excluded.active, updated_at=datetime('now')
                WHERE access_devices.tenant_slug=excluded.tenant_slug
                """,
                (
                    device_id,
                    tenant,
                    site,
                    name,
                    vendor,
                    model,
                    host,
                    connector_id,
                    username,
                    password_enc,
                    access_direction,
                    active,
                ),
            )
        else:
            c.execute(
                """
                INSERT INTO access_devices(
                  id, tenant_slug, site, name, vendor, model, host, connector_id, username,
                  access_direction, active, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(id) DO UPDATE SET
                  site=excluded.site, name=excluded.name, vendor=excluded.vendor, model=excluded.model,
                  host=excluded.host, connector_id=excluded.connector_id, username=excluded.username,
                  access_direction=excluded.access_direction, active=excluded.active, updated_at=datetime('now')
                WHERE access_devices.tenant_slug=excluded.tenant_slug
                """,
                (device_id, tenant, site, name, vendor, model, host, connector_id, username, access_direction, active),
            )
        row = c.execute("SELECT * FROM access_devices WHERE tenant_slug=? AND id=?", (tenant, device_id)).fetchone()
    if row is None:
        raise ValueError("Dispositivo nao encontrado neste cliente.")
    return _device_row_dict(row)


def delete_device(device_id: str) -> bool:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(device_id, 80)
    if not did:
        return False
    with db_store._conn() as c:
        cur = c.execute("DELETE FROM access_devices WHERE tenant_slug=? AND id=?", (tenant, did))
        c.execute("DELETE FROM access_door_group_members WHERE tenant_slug=? AND device_id=?", (tenant, did))
        c.execute("DELETE FROM access_provision_status WHERE tenant_slug=? AND device_id=?", (tenant, did))
        return int(cur.rowcount or 0) > 0


def get_device_with_password(device_id: str) -> Dict[str, Any] | None:
    """Uso interno (access_control_device.py) -- unica funcao que devolve a senha decifrada."""
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(device_id, 80)
    with db_store._conn() as c:
        row = c.execute("SELECT * FROM access_devices WHERE tenant_slug=? AND id=?", (tenant, did)).fetchone()
    if row is None:
        return None
    data = dict(row)
    enc = data.pop("password_enc", "")
    data["password"] = decrypt(enc) if enc else ""
    data["active"] = bool(int(data.get("active") or 0))
    return data


def record_event(event: Dict[str, Any]) -> str:
    """Grava um evento de acesso, ignorando duplicatas.

    O device do controle de acesso ainda nao filtra por since_id (ver
    poll_device_events em access_control_sync.py), entao cada poll do loop
    de fundo (Task 7) reenvia o indice de eventos inteiro do equipamento.
    Sem essa deduplicacao, access_events cresceria sem limite com o mesmo
    conjunto de eventos repetido a cada ciclo. Tratamos como duplicata
    qualquer evento com a mesma tupla (tenant_slug, device_id,
    person_name_raw, event_type, occurred_at) ja gravada -- se ja existir,
    devolve o id existente em vez de inserir de novo.
    """
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    device_id = _clean_text(event.get("device_id"), 80)
    person_name_raw = _clean_text(event.get("person_name_raw"), 160)
    event_type = normalize_access_event_type(event.get("event_type"))
    occurred_at = _clean_text(event.get("occurred_at"), 40)
    raw_event_id = _clean_text(event.get("raw_event_id"), 120)
    with db_store._conn() as c:
        if raw_event_id:
            existing = c.execute(
                """
                SELECT id FROM access_events
                WHERE tenant_slug = ? AND device_id = ? AND raw_event_id = ?
                """,
                (tenant, device_id, raw_event_id),
            ).fetchone()
        else:
            existing = c.execute(
                """
                SELECT id FROM access_events
                WHERE tenant_slug = ? AND device_id = ? AND person_name_raw = ?
                  AND event_type = ? AND occurred_at = ?
                """,
                (tenant, device_id, person_name_raw, event_type, occurred_at),
            ).fetchone()
        if existing:
            return str(existing["id"])
        # Debounce (janela de 5 min): a leitora facial re-reconhece a mesma
        # pessoa a cada ~3s enquanto ela fica parada na frente -> dezenas de
        # eventos identicos (e dezenas de WhatsApp). Tratamos como a MESMA
        # passagem qualquer marcacao da mesma pessoa + mesmo tipo + mesmo device
        # dentro de 5 min: nao insere de novo nem notifica. Saida manual/operador
        # (source manual) passa direto -- e uma acao deliberada.
        source_norm = _clean_text(event.get("source") or "device", 20)
        person_id_norm = _clean_text(event.get("person_id"), 80)
        if source_norm != "manual" and occurred_at:
            window_start = ""
            try:
                _base_dt = datetime.strptime(occurred_at[:19], "%Y-%m-%d %H:%M:%S")
                window_start = (_base_dt - timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                window_start = ""
            if window_start:
                recent = c.execute(
                    """
                    SELECT id FROM access_events
                    WHERE tenant_slug = ? AND device_id = ? AND event_type = ?
                      AND occurred_at >= ? AND occurred_at <= ?
                      AND (
                        (? <> '' AND person_id = ?)
                        OR (? = '' AND person_name_raw = ?)
                      )
                    ORDER BY occurred_at DESC LIMIT 1
                    """,
                    (
                        tenant, device_id, event_type, window_start, occurred_at,
                        person_id_norm, person_id_norm, person_id_norm, person_name_raw,
                    ),
                ).fetchone()
                if recent:
                    return str(recent["id"])
        event_id = uuid.uuid4().hex
        c.execute(
            """
            INSERT INTO access_events(
              id, tenant_slug, site, device_id, person_id, person_name_raw, event_type,
              source, device_name, device_role, operator_user, manual_reason,
              notification_status, raw_event_id, raw_payload, occurred_at, synced_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                event_id,
                tenant,
                _clean_text(event.get("site"), 120),
                device_id,
                _clean_text(event.get("person_id"), 80),
                person_name_raw,
                event_type,
                _clean_text(event.get("source") or "device", 20),
                _clean_text(event.get("device_name"), 160),
                normalize_access_direction(event.get("device_role")),
                _clean_text(event.get("operator_user"), 120),
                _clean_text(event.get("manual_reason"), 500),
                _clean_text(event.get("notification_status"), 40),
                raw_event_id,
                _clean_text(event.get("raw_payload"), 4000),
                occurred_at,
            ),
        )
    try:
        from app.services.access_control_notifications import notify_access_event

        result = notify_access_event(
            {
                "id": event_id,
                "site": _clean_text(event.get("site"), 120),
                "device_id": device_id,
                "person_id": _clean_text(event.get("person_id"), 80),
                "person_name_raw": person_name_raw,
                "event_type": event_type,
                "source": _clean_text(event.get("source") or "device", 20),
                "device_name": _clean_text(event.get("device_name"), 160),
                "device_role": normalize_access_direction(event.get("device_role")),
                "operator_user": _clean_text(event.get("operator_user"), 120),
                "manual_reason": _clean_text(event.get("manual_reason"), 500),
                "raw_event_id": raw_event_id,
                "occurred_at": occurred_at,
            }
        )
        notification_status = ",".join(
            _clean_text(status, 40) for status in result.get("statuses", []) if _clean_text(status, 40)
        )
    except Exception:
        notification_status = "notification_failed"
    if notification_status:
        with db_store._conn() as c:
            c.execute(
                """
                UPDATE access_events
                SET notification_status=?
                WHERE tenant_slug=? AND id=?
                """,
                (_clean_text(notification_status, 200), tenant, event_id),
            )
    return event_id


def latest_device_event_occurred_at(device_id: str) -> str:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    did = _clean_text(device_id, 80)
    if not did:
        return ""
    with db_store._conn() as c:
        row = c.execute(
            """
            SELECT occurred_at
            FROM access_events
            WHERE tenant_slug = ? AND device_id = ?
            ORDER BY occurred_at DESC
            LIMIT 1
            """,
            (tenant, did),
        ).fetchone()
    return _clean_text((row or {}).get("occurred_at") if row else "", 40)


def _event_row_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    data["event_type"] = normalize_access_event_type(data.get("event_type"))
    data["source"] = _clean_text(data.get("source") or "device", 20)
    data["device_role"] = normalize_access_direction(data.get("device_role"))
    if not _clean_text(data.get("site"), 120) and data.get("resolved_site"):
        data["site"] = _clean_text(data.get("resolved_site"), 120)
    if not _clean_text(data.get("device_name"), 160) and data.get("resolved_device_name"):
        data["device_name"] = _clean_text(data.get("resolved_device_name"), 160)
    if not _clean_text(data.get("person_document"), 80) and data.get("document_id"):
        data["person_document"] = _clean_text(data.get("document_id"), 80)
    if not _clean_text(data.get("person_enrollment"), 80) and data.get("enrollment_code"):
        data["person_enrollment"] = _clean_text(data.get("enrollment_code"), 80)
    return data


def list_events(person_id: str = "", site: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    where = ["tenant_slug = ?"]
    params: list[Any] = [tenant]
    if person_id:
        where.append("person_id = ?")
        params.append(_clean_text(person_id, 80))
    if site:
        where.append("site = ?")
        params.append(_clean_text(site, 120))
    with db_store._conn() as c:
        rows = c.execute(
            f"SELECT * FROM access_events WHERE {' AND '.join(where)} ORDER BY occurred_at DESC LIMIT ?",
            tuple(params) + (max(1, min(int(limit or 200), 1000)),),
        ).fetchall()
    return [_event_row_dict(r) for r in rows]


def _normalize_report_bound(value: str = "", *, end: bool = False) -> str:
    clean = _clean_text(value, 19).replace("T", " ").strip()
    if not clean:
        return ""
    if len(clean) == 16:
        return f"{clean}:59" if end else f"{clean}:00"
    if len(clean) >= 19:
        return clean[:19]
    return _clean_text(clean, 10)


def _period_bounds(period: str = "", start: str = "", end: str = "") -> tuple[str, str]:
    today = datetime.now().date()
    clean_period = _clean_text(period, 32).lower()
    if clean_period in {"all", "todos", "historico"}:
        return "", ""
    if clean_period == "yesterday":
        day = today.fromordinal(today.toordinal() - 1)
        return day.isoformat(), day.isoformat()
    if clean_period in {"last7", "7d"}:
        day = today.fromordinal(today.toordinal() - 6)
        return day.isoformat(), today.isoformat()
    if clean_period in {"last30", "30d"}:
        day = today.fromordinal(today.toordinal() - 29)
        return day.isoformat(), today.isoformat()
    if clean_period == "custom":
        return _normalize_report_bound(start), _normalize_report_bound(end, end=True)
    return today.isoformat(), today.isoformat()


def list_access_report_events(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    where = ["e.tenant_slug = ?"]
    params: list[Any] = [tenant]
    site = _clean_text(filters.get("site"), 120)
    event_type = _clean_text(filters.get("type"), 32).lower()
    search = _clean_text(filters.get("search"), 160).lower()
    device_id = _clean_text(filters.get("device_id"), 80)
    door_group_id = _clean_text(filters.get("door_group_id"), 80)
    start, end = _period_bounds(
        _clean_text(filters.get("period"), 32),
        _clean_text(filters.get("start"), 19),
        _clean_text(filters.get("end"), 19),
    )
    if start:
        where.append("e.occurred_at >= ?" if len(start) > 10 else "substr(e.occurred_at, 1, 10) >= ?")
        params.append(start)
    if end:
        where.append("e.occurred_at <= ?" if len(end) > 10 else "substr(e.occurred_at, 1, 10) <= ?")
        params.append(end)
    if site:
        where.append("COALESCE(NULLIF(e.site, ''), p.site, d.site, '') = ?")
        params.append(site)
    if event_type in {"entrada", "saida", "saida_manual"}:
        where.append("e.event_type = ?")
        params.append(event_type)
    if device_id:
        where.append("e.device_id = ?")
        params.append(device_id)
    elif door_group_id:
        device_ids = list_door_group_members(door_group_id)
        if not device_ids:
            return []
        placeholders = ",".join("?" for _ in device_ids)
        where.append(f"e.device_id IN ({placeholders})")
        params.extend(device_ids)
    if search:
        like = f"%{search}%"
        where.append(
            """
            (
              lower(COALESCE(NULLIF(p.full_name, ''), e.person_name_raw, '')) LIKE ?
              OR lower(COALESCE(p.class_name, '')) LIKE ?
              OR lower(COALESCE(NULLIF(d.name, ''), e.device_name, '')) LIKE ?
            )
            """
        )
        params.extend([like, like, like])
    limit = max(1, min(int(filters.get("limit") or 300), 1000))
    with db_store._conn() as c:
        rows = c.execute(
            f"""
            SELECT
              e.*,
              COALESCE(NULLIF(p.full_name, ''), e.person_name_raw, '') AS person_name,
              COALESCE(p.person_type, '') AS person_type,
              COALESCE(p.class_name, '') AS class_name,
              COALESCE(p.document_id, '') AS person_document,
              COALESCE(p.enrollment_code, '') AS person_enrollment,
              COALESCE(NULLIF(p.site, ''), e.site, d.site, '') AS resolved_site,
              COALESCE(NULLIF(d.name, ''), e.device_name, '') AS resolved_device_name
            FROM access_events e
            LEFT JOIN access_people p ON p.tenant_slug=e.tenant_slug AND p.id=e.person_id
            LEFT JOIN access_devices d ON d.tenant_slug=e.tenant_slug AND d.id=e.device_id
            WHERE {' AND '.join(where)}
            ORDER BY e.occurred_at DESC, e.synced_at DESC
            LIMIT ?
            """,
            tuple(params) + (limit,),
        ).fetchall()
    return [_event_row_dict(r) for r in rows]


def access_presence_summary(site: str = "", device_id: str = "", door_group_id: str = "") -> Dict[str, int]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    where = ["e.tenant_slug = ?", "e.person_id <> ''"]
    params: list[Any] = [tenant]
    clean_site = _clean_text(site, 120)
    clean_device_id = _clean_text(device_id, 80)
    clean_door_group_id = _clean_text(door_group_id, 80)
    if clean_site:
        where.append("COALESCE(NULLIF(e.site, ''), p.site, d.site, '') = ?")
        params.append(clean_site)
    if clean_device_id:
        where.append("e.device_id = ?")
        params.append(clean_device_id)
    elif clean_door_group_id:
        device_ids = list_door_group_members(clean_door_group_id)
        if not device_ids:
            return {"people_with_events": 0, "inside_now": 0, "outside_now": 0}
        placeholders = ",".join("?" for _ in device_ids)
        where.append(f"e.device_id IN ({placeholders})")
        params.extend(device_ids)
    with db_store._conn() as c:
        rows = c.execute(
            f"""
            SELECT e.person_id, e.event_type
            FROM access_events e
            LEFT JOIN access_people p ON p.tenant_slug=e.tenant_slug AND p.id=e.person_id
            LEFT JOIN access_devices d ON d.tenant_slug=e.tenant_slug AND d.id=e.device_id
            WHERE {' AND '.join(where)}
            ORDER BY e.person_id, e.occurred_at DESC, e.synced_at DESC
            """,
            tuple(params),
        ).fetchall()
    latest: Dict[str, str] = {}
    for row in rows:
        person_id = str(row["person_id"] or "")
        if person_id and person_id not in latest:
            latest[person_id] = normalize_access_event_type(row["event_type"])
    inside = sum(1 for event_type in latest.values() if event_type == "entrada")
    outside = sum(1 for event_type in latest.values() if event_type in {"saida", "saida_manual"})
    return {"people_with_events": len(latest), "inside_now": inside, "outside_now": outside}


def access_present_people(site: str = "", device_id: str = "", door_group_id: str = "") -> List[Dict[str, Any]]:
    """Lista as pessoas PRESENTES agora (mesma regra do inside_now: a ultima
    marcacao de cada pessoa e uma entrada). Devolve nome/site/horario da entrada
    para a lista lateral do Acesso ao Vivo -- sem isso o front derivava a lista
    dos eventos carregados (limitados) e nunca batia com o numero do card."""
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    where = ["e.tenant_slug = ?", "e.person_id <> ''"]
    params: list[Any] = [tenant]
    clean_site = _clean_text(site, 120)
    clean_device_id = _clean_text(device_id, 80)
    clean_door_group_id = _clean_text(door_group_id, 80)
    if clean_site:
        where.append("COALESCE(NULLIF(e.site, ''), p.site, d.site, '') = ?")
        params.append(clean_site)
    if clean_device_id:
        where.append("e.device_id = ?")
        params.append(clean_device_id)
    elif clean_door_group_id:
        device_ids = list_door_group_members(clean_door_group_id)
        if not device_ids:
            return []
        placeholders = ",".join("?" for _ in device_ids)
        where.append(f"e.device_id IN ({placeholders})")
        params.extend(device_ids)
    with db_store._conn() as c:
        rows = c.execute(
            f"""
            SELECT e.person_id, e.event_type, e.occurred_at,
                   COALESCE(NULLIF(e.site, ''), p.site, d.site, '') AS site,
                   p.full_name AS full_name, p.document_id AS document_id,
                   p.enrollment_code AS enrollment_code
            FROM access_events e
            LEFT JOIN access_people p ON p.tenant_slug=e.tenant_slug AND p.id=e.person_id
            LEFT JOIN access_devices d ON d.tenant_slug=e.tenant_slug AND d.id=e.device_id
            WHERE {' AND '.join(where)}
            ORDER BY e.person_id, e.occurred_at DESC, e.synced_at DESC
            """,
            tuple(params),
        ).fetchall()
    latest: Dict[str, Any] = {}
    for row in rows:
        person_id = str(row["person_id"] or "")
        if person_id and person_id not in latest:
            latest[person_id] = row
    present: List[Dict[str, Any]] = []
    for person_id, row in latest.items():
        if normalize_access_event_type(row["event_type"]) != "entrada":
            continue
        present.append({
            "person_id": person_id,
            "name": (str(row["full_name"] or "").strip() or "Pessoa nao identificada"),
            "site": str(row["site"] or "").strip(),
            "document": str(row["document_id"] or "").strip(),
            "enrollment": str(row["enrollment_code"] or "").strip(),
            "since": str(row["occurred_at"] or "").strip(),
        })
    present.sort(key=lambda item: item.get("since") or "", reverse=True)
    return present


def access_report_summary(filters: Dict[str, Any]) -> Dict[str, int]:
    events = list_access_report_events({**filters, "limit": 1000})
    entries = sum(1 for event in events if event.get("event_type") == "entrada")
    exits = sum(1 for event in events if event.get("event_type") == "saida")
    manual_exits = sum(1 for event in events if event.get("event_type") == "saida_manual")
    without_person = sum(1 for event in events if not str(event.get("person_id") or "").strip())
    presence = access_presence_summary(
        site=_clean_text(filters.get("site"), 120),
        device_id=_clean_text(filters.get("device_id"), 80),
        door_group_id=_clean_text(filters.get("door_group_id"), 80),
    )
    return {
        "total": len(events),
        "entries": entries,
        "exits": exits,
        "manual_exits": manual_exits,
        "inside_now": int(presence.get("inside_now") or 0),
        "without_person": without_person,
    }


def record_manual_exit(person_id: str, site: str = "", reason: str = "", operator_user: str = "") -> Dict[str, Any]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    pid = _clean_text(person_id, 80)
    if not pid:
        raise ValueError("Informe a pessoa para registrar saida.")
    with db_store._conn() as c:
        person = c.execute(
            "SELECT * FROM access_people WHERE tenant_slug=? AND id=?",
            (tenant, pid),
        ).fetchone()
    if person is None:
        raise ValueError("Pessoa nao encontrada neste cliente.")
    person_data = dict(person)
    resolved_site = _clean_text(site or person_data.get("site"), 120)
    occurred_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    event_id = record_event(
        {
            "site": resolved_site,
            "device_id": "",
            "person_id": pid,
            "person_name_raw": person_data.get("full_name", ""),
            "event_type": "saida_manual",
            "source": "manual",
            "operator_user": operator_user,
            "manual_reason": reason,
            "occurred_at": occurred_at,
        }
    )
    events = list_access_report_events({"limit": 1, "type": "saida_manual", "site": resolved_site})
    event = next((item for item in events if item.get("id") == event_id), None)
    return event or {"id": event_id, "person_id": pid, "event_type": "saida_manual", "occurred_at": occurred_at}


def upsert_provision_status(person_id: str, device_id: str, status: str, last_error: str = "") -> None:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    pid = _clean_text(person_id, 80)
    did = _clean_text(device_id, 80)
    with db_store._conn() as c:
        c.execute(
            """
            INSERT INTO access_provision_status(tenant_slug, person_id, device_id, status, last_error, updated_at)
            VALUES(?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(tenant_slug, person_id, device_id) DO UPDATE SET
              status=excluded.status, last_error=excluded.last_error, updated_at=datetime('now')
            """,
            (tenant, pid, did, _clean_text(status, 20) or "pending", _clean_text(last_error, 500)),
        )


def list_pending_provisions() -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    with db_store._conn() as c:
        rows = c.execute(
            "SELECT * FROM access_provision_status WHERE tenant_slug=? AND status IN ('pending','failed')",
            (tenant,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_provision_status_for_person(person_id: str) -> List[Dict[str, Any]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    pid = _clean_text(person_id, 80)
    with db_store._conn() as c:
        rows = c.execute(
            "SELECT * FROM access_provision_status WHERE tenant_slug=? AND person_id=?", (tenant, pid)
        ).fetchall()
    return [dict(r) for r in rows]


def list_provision_status_for_people(person_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    ensure_access_control_schema()
    tenant = db_store._current_tenant_slug()
    clean_ids = []
    seen = set()
    for person_id in person_ids:
        pid = _clean_text(person_id, 80)
        if pid and pid not in seen:
            seen.add(pid)
            clean_ids.append(pid)
    grouped: Dict[str, List[Dict[str, Any]]] = {pid: [] for pid in clean_ids}
    if not clean_ids:
        return grouped

    with db_store._conn() as c:
        for start in range(0, len(clean_ids), 500):
            batch = clean_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = c.execute(
                f"""
                SELECT * FROM access_provision_status
                WHERE tenant_slug=? AND person_id IN ({placeholders})
                """,
                (tenant, *batch),
            ).fetchall()
            for row in rows:
                data = dict(row)
                grouped.setdefault(str(data.get("person_id") or ""), []).append(data)
    return grouped
