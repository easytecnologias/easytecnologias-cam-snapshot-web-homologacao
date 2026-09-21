-- Lotacao da pessoa ligada a um conector (o local monitorado): no alerta, a
-- central ve TODAS as cameras daquele conector, mesmo sem coordenada.
ALTER TABLE alert_members ADD COLUMN IF NOT EXISTS unit_connector_id TEXT NOT NULL DEFAULT '';
