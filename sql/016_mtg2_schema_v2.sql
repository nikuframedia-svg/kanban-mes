-- Kanban MTG2 schema v2: total Vanguard, duração e proveniência documental.
-- Idempotente; a aplicação sonda estas colunas durante um rollout faseado.
BEGIN;

ALTER TABLE mes_kanban.production_records
    ADD COLUMN IF NOT EXISTS quantity_total_mm numeric,
    ADD COLUMN IF NOT EXISTS duration_text text;

ALTER TABLE mes_kanban.validated_sheets
    ADD COLUMN IF NOT EXISTS source_filename text,
    ADD COLUMN IF NOT EXISTS source_page integer;

COMMENT ON COLUMN mes_kanban.production_records.quantity_total_mm IS
    'Comprimento total real da linha Vanguard, em milímetros';
COMMENT ON COLUMN mes_kanban.production_records.duration_text IS
    'Duração manuscrita; nunca contém o número de corte/lote';
COMMENT ON COLUMN mes_kanban.validated_sheets.source_filename IS
    'Nome do PDF/fotografia de origem; proveniência, não data de produção';
COMMENT ON COLUMN mes_kanban.validated_sheets.source_page IS
    'Página 1-based no PDF de origem';

COMMIT;
