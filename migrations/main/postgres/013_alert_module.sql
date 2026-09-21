-- Modulo Alerta: botao de panico/coacao no celular + painel ao vivo da central.
-- Datas sao TEXT ISO-8601 UTC ("2026-09-18T13:14:00Z") gravadas pelo Python,
-- iguais nos dois backends, pra comparacao de string funcionar no loop de
-- escalonamento sem depender de fuso do banco.

CREATE TABLE IF NOT EXISTS alert_members (
    id TEXT PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    full_name TEXT NOT NULL,
    document_id TEXT NOT NULL DEFAULT '',
    role_title TEXT NOT NULL DEFAULT '',
    unit_name TEXT NOT NULL DEFAULT '',
    unit_lat DOUBLE PRECISION,
    unit_lon DOUBLE PRECISION,
    phone TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    pin_hash TEXT NOT NULL DEFAULT '',
    duress_pin_hash TEXT NOT NULL DEFAULT '',
    activation_code_hash TEXT NOT NULL DEFAULT '',
    activation_expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS alert_members_tenant_document_uidx
    ON alert_members(tenant_slug, document_id) WHERE document_id <> '';
CREATE INDEX IF NOT EXISTS alert_members_tenant_name_idx ON alert_members(tenant_slug, full_name);
CREATE INDEX IF NOT EXISTS alert_members_activation_idx ON alert_members(activation_code_hash);

CREATE TABLE IF NOT EXISTS alert_app_sessions (
    id TEXT PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    member_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    device_label TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '',
    revoked_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS alert_app_sessions_token_uidx ON alert_app_sessions(token_hash);
CREATE INDEX IF NOT EXISTS alert_app_sessions_member_idx ON alert_app_sessions(tenant_slug, member_id);

CREATE TABLE IF NOT EXISTS alert_incidents (
    id TEXT PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    member_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'panic',
    status TEXT NOT NULL DEFAULT 'new',
    source TEXT NOT NULL DEFAULT 'app',
    opened_at TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL DEFAULT '',
    acknowledged_by TEXT NOT NULL DEFAULT '',
    dispatched_at TEXT NOT NULL DEFAULT '',
    dispatched_by TEXT NOT NULL DEFAULT '',
    closed_at TEXT NOT NULL DEFAULT '',
    closed_by TEXT NOT NULL DEFAULT '',
    close_note TEXT NOT NULL DEFAULT '',
    duress_at TEXT NOT NULL DEFAULT '',
    escalated_at TEXT NOT NULL DEFAULT '',
    last_lat DOUBLE PRECISION,
    last_lon DOUBLE PRECISION,
    last_accuracy DOUBLE PRECISION,
    last_battery INTEGER,
    last_position_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS alert_incidents_tenant_status_idx ON alert_incidents(tenant_slug, status, opened_at);
CREATE INDEX IF NOT EXISTS alert_incidents_member_idx ON alert_incidents(tenant_slug, member_id, status);
CREATE INDEX IF NOT EXISTS alert_incidents_escalation_idx ON alert_incidents(status, escalated_at, opened_at);

CREATE TABLE IF NOT EXISTS alert_positions (
    id BIGSERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    accuracy DOUBLE PRECISION,
    battery INTEGER,
    recorded_at TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS alert_positions_incident_idx ON alert_positions(tenant_slug, incident_id, recorded_at);

CREATE TABLE IF NOT EXISTS alert_incident_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_incident_log_incident_idx ON alert_incident_log(tenant_slug, incident_id, created_at);
