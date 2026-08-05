-- Kanban MES — schema de dados VALIDADOS + role da aplicação.
-- Regra de ouro: o Postgres só recebe dados depois de validação humana.
-- A app trabalha em staging local (SQLite); este schema é a única superfície de escrita.
--
-- Aplicar como superuser na BD dataresearchmtg:
--   docker exec -i postgres psql -U postgres -d dataresearchmtg < sql/010_mes_kanban.sql
-- Antes de aplicar: definir a password do role (ver nota no fim).

BEGIN;

CREATE SCHEMA IF NOT EXISTS mes_kanban;

-- Folha validada: uma linha por folha kanban aprovada por um humano. Append-only.
CREATE TABLE IF NOT EXISTS mes_kanban.validated_sheets (
    sheet_uid       text PRIMARY KEY,           -- uid gerado pela app (uuid4)
    sheet_date      date NOT NULL,              -- data escrita na folha
    template_name   text NOT NULL,              -- ex.: chapa_kanban | cantoneiras_kanban
    family          text NOT NULL CHECK (family IN ('chapa', 'cantoneiras')),
    operator_name   text NOT NULL,
    operator_no     text,
    sector_machine  text,
    shift           text,
    image_sha256    text NOT NULL,              -- hash da foto original (a foto fica na app)
    raw_extraction  jsonb NOT NULL,             -- OCR bruto, imutável — trilho de auditoria
    sheet_data      jsonb NOT NULL,             -- versão final (pós-cross + edições humanas)
    cross_check     jsonb,                      -- resultado do motor (bits, margens, estados)
    edit_count      integer NOT NULL DEFAULT 0, -- nº de células corrigidas por humanos
    validated_by    text NOT NULL,
    validated_at    timestamptz NOT NULL DEFAULT now(),
    app_version     text
);

-- Linhas de produção desnormalizadas da folha validada — alimenta KPIs.
CREATE TABLE IF NOT EXISTS mes_kanban.production_records (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sheet_uid       text NOT NULL REFERENCES mes_kanban.validated_sheets(sheet_uid),
    row_index       integer NOT NULL,
    sheet_date      date NOT NULL,
    family          text NOT NULL,
    operator_name   text NOT NULL,
    machine         text,
    -- identidade cruzada contra o plano
    production_order  text,                     -- OF
    sales_order       text,                     -- OV
    customer_name     text,
    model_ref         text,                     -- modelo / referência / nesting
    matched_plan_key  text,                     -- chave da linha do plano vencedora (se houver)
    match_confidence  double precision,         -- P(certo) do motor no momento da validação
    -- quantidades e medidas (o que se aplicar à família)
    quantity          double precision,
    length_mm         double precision,
    width_mm          double precision,
    thickness_mm      double precision,
    lot_ref           text,
    scrap             text,
    hours_worked      double precision,
    extra             jsonb,                    -- campos específicos do template
    validated_at    timestamptz NOT NULL,
    UNIQUE (sheet_uid, row_index)
);

CREATE INDEX IF NOT EXISTS production_records_date_idx
    ON mes_kanban.production_records (sheet_date);
CREATE INDEX IF NOT EXISTS production_records_of_idx
    ON mes_kanban.production_records (production_order);
CREATE INDEX IF NOT EXISTS production_records_machine_idx
    ON mes_kanban.production_records (machine);

-- Role da aplicação: lê o plano, escreve APENAS em mes_kanban, e mesmo aí sem UPDATE/DELETE
-- nas tabelas validadas — a imutabilidade é imposta pela base de dados, não pelo código.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mes_kanban_app') THEN
        CREATE ROLE mes_kanban_app LOGIN;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE dataresearchmtg TO mes_kanban_app;
GRANT USAGE ON SCHEMA core_mtg, analytics_mtg, raw_mtg, audit_mtg TO mes_kanban_app;
GRANT SELECT ON ALL TABLES IN SCHEMA core_mtg, analytics_mtg, raw_mtg, audit_mtg TO mes_kanban_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA core_mtg, analytics_mtg, raw_mtg, audit_mtg
    GRANT SELECT ON TABLES TO mes_kanban_app;

GRANT USAGE ON SCHEMA mes_kanban TO mes_kanban_app;
GRANT SELECT, INSERT ON mes_kanban.validated_sheets TO mes_kanban_app;
GRANT SELECT, INSERT ON mes_kanban.production_records TO mes_kanban_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mes_kanban TO mes_kanban_app;

COMMIT;

-- Depois de aplicar, definir a password (gerar uma forte e guardá-la no .env da app):
--   ALTER ROLE mes_kanban_app PASSWORD '<password-gerada>';
