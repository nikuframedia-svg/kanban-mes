-- Metros teóricos por linha e controlo de desperdício (19/08/2026).
--
-- plan_length_mm  = comprimento da peça na linha do plano que casou (mm)
-- line_meters     = qtd × plan_length_mm / 1000 — metros teóricos da linha
-- meters_produced = METROS PRODUZIDOS manuscritos no rodapé, desnormalizados
--                   por linha (como hours_worked); a diferença entre o Σ dos
--                   line_meters e este valor é o desperdício/excedente.
--
-- Colunas opcionais: a app sonda-as via information_schema (pg_store.py) e
-- funciona com ou sem elas — aplicar este ficheiro quando der jeito.

ALTER TABLE mes_kanban.production_records
    ADD COLUMN IF NOT EXISTS plan_length_mm  numeric,
    ADD COLUMN IF NOT EXISTS line_meters     numeric,
    ADD COLUMN IF NOT EXISTS meters_produced numeric;

COMMENT ON COLUMN mes_kanban.production_records.plan_length_mm  IS 'Comprimento da peça no plano (mm), da linha que casou';
COMMENT ON COLUMN mes_kanban.production_records.line_meters     IS 'Metros teóricos da linha: qtd × plan_length_mm / 1000';
COMMENT ON COLUMN mes_kanban.production_records.meters_produced IS 'METROS PRODUZIDOS do rodapé da folha (desnormalizado por linha)';
