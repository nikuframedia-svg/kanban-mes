-- Kanban MES — paragens de máquina (verso da folha TPL102).
-- Mesma regra de ouro: append-only, escrita só no ato de validação humana.
--
-- Aplicar como superuser na BD dataresearchmtg:
--   docker exec -i postgres psql -U postgres -d dataresearchmtg < sql/011_mes_paragens.sql

BEGIN;

CREATE TABLE IF NOT EXISTS mes_kanban.stoppage_records (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sheet_uid       text NOT NULL REFERENCES mes_kanban.validated_sheets(sheet_uid),
    row_index       integer NOT NULL,
    sheet_date      date NOT NULL,
    machine         text,
    operator_name   text NOT NULL,
    motivo          text,
    inicio          text,                       -- tal como escrito ("11:30h")
    fim             text,
    duracao_horas   double precision,           -- parse numérico de "1H"/"1.5" (se possível)
    resolvido       text,                       -- tal como escrito ("Sim!")
    extra           jsonb,
    validated_at    timestamptz NOT NULL,
    UNIQUE (sheet_uid, row_index)
);

CREATE INDEX IF NOT EXISTS stoppage_records_date_idx
    ON mes_kanban.stoppage_records (sheet_date);
CREATE INDEX IF NOT EXISTS stoppage_records_machine_idx
    ON mes_kanban.stoppage_records (machine);

-- imutabilidade imposta pela base de dados: sem UPDATE/DELETE para a app
GRANT SELECT, INSERT ON mes_kanban.stoppage_records TO mes_kanban_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mes_kanban TO mes_kanban_app;

COMMIT;
