-- Contrato aditivo partilhado pelos Kanban MES: número público, snapshot e
-- decomposição auditável de linhas Perf. Comp. Aplicar depois de sql/016.
BEGIN;

ALTER TABLE IF EXISTS mes_kanban.validated_sheets
    ADD COLUMN IF NOT EXISTS source_app text,
    ADD COLUMN IF NOT EXISTS sheet_no bigint,
    ADD COLUMN IF NOT EXISTS plan_snapshot_id text;

UPDATE mes_kanban.validated_sheets
SET source_app = CASE
    WHEN family = 'perfis' THEN 'kanban-mes-mtg2'
    ELSE 'kanban-mes'
END
WHERE source_app IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS validated_sheets_source_app_sheet_no_uidx
    ON mes_kanban.validated_sheets(source_app, sheet_no)
    WHERE source_app IS NOT NULL AND sheet_no IS NOT NULL;

ALTER TABLE IF EXISTS mes_kanban.production_records
    ADD COLUMN IF NOT EXISTS plan_snapshot_id text;

CREATE TABLE IF NOT EXISTS mes_kanban.production_record_plan_refs (
    id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    production_record_id     bigint NOT NULL
        REFERENCES mes_kanban.production_records(id),
    sheet_uid                text NOT NULL
        REFERENCES mes_kanban.validated_sheets(sheet_uid),
    row_index                integer NOT NULL,
    plan_snapshot_id         text NOT NULL,
    plan_key                 text NOT NULL,
    component_ref            text,
    profile_type             text,
    length_mm                numeric,
    quantity_planned         numeric,
    quantity_made_before     numeric,
    remaining_before         numeric NOT NULL,
    overproduction_before    numeric,
    assumed_quantity         numeric NOT NULL CHECK (assumed_quantity >= 0),
    remaining_rule           text NOT NULL,
    extra                    jsonb,
    created_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sheet_uid, row_index, plan_key)
);

CREATE INDEX IF NOT EXISTS production_record_plan_refs_parent_idx
    ON mes_kanban.production_record_plan_refs(production_record_id);
CREATE INDEX IF NOT EXISTS production_record_plan_refs_plan_idx
    ON mes_kanban.production_record_plan_refs(plan_snapshot_id, plan_key);

GRANT SELECT, INSERT ON mes_kanban.production_record_plan_refs TO mes_kanban_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mes_kanban TO mes_kanban_app;

COMMIT;
