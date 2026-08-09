-- Identidade do operador resolvida contra a lista de colaboradores do SAP.
--
-- `operator_no` (o número escrito no kanban) já existia; falta o número de
-- pessoal SAP e, sobretudo, COMO é que a identidade foi decidida. Guardar a
-- regra é o que distingue um lookup de um trilho de auditoria: daqui a um ano
-- é possível saber se aquele nome foi lido, corrigido por número, ou aceite
-- sem referência.
--
--   docker exec -i postgres psql -U postgres -d dataresearchmtg < sql/013_operador.sql

BEGIN;

ALTER TABLE mes_kanban.validated_sheets
    ADD COLUMN IF NOT EXISTS operator_pernr      char(8),
    ADD COLUMN IF NOT EXISTS operator_match_rule text;

ALTER TABLE mes_kanban.production_records
    ADD COLUMN IF NOT EXISTS operator_pernr char(8);

ALTER TABLE mes_kanban.stoppage_records
    ADD COLUMN IF NOT EXISTS operator_pernr char(8);

COMMENT ON COLUMN mes_kanban.validated_sheets.operator_match_rule IS
    'Como a identidade foi decidida: exact|token|so_numero|corrigido|sem_ref|sem_numero';

CREATE INDEX IF NOT EXISTS production_records_pernr_idx
    ON mes_kanban.production_records (operator_pernr);

COMMIT;
