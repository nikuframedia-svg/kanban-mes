-- Colunas que faltavam para registar o que a folha TPL102 diz de facto.
--
-- 1) `full_profile` — a coluna PERF. COMP. da folha. Quando o operador põe um
--    visto, aquela linha vale por TODAS as referências daquele perfil na OF, e
--    é por isso que nessas linhas não há modelo nem quantidade. Estava a ser
--    gravada em `length_mm` (um comprimento em milímetros), o que a perdia:
--    parse_number('x') é NULL.
--
-- 2) `profile_type` — o perfil (L60X60X5). É o campo mais discriminante das
--    cantoneiras e ia enterrado no jsonb `extra`, fora de qualquer consulta.
--
-- 3) `plan_quantity` — a quantidade planeada para a linha do plano que casou,
--    guardada ao lado da produzida para se poder ver depois porque é que uma
--    quantidade foi assinalada como acima do previsto.
--
-- Idempotente: pode voltar a correr sem estragar nada.
--   docker exec -i postgres psql -U postgres -d dataresearchmtg < sql/012_perfil_e_marca.sql

BEGIN;

ALTER TABLE mes_kanban.production_records
    ADD COLUMN IF NOT EXISTS full_profile  boolean,
    ADD COLUMN IF NOT EXISTS profile_type  text,
    ADD COLUMN IF NOT EXISTS plan_quantity double precision;

COMMENT ON COLUMN mes_kanban.production_records.full_profile IS
    'PERF. COMP. na folha: a linha cobre todas as referências do perfil nesta OF';
COMMENT ON COLUMN mes_kanban.production_records.profile_type IS
    'Perfil como o operador o escreveu, normalizado para a forma do plano';
COMMENT ON COLUMN mes_kanban.production_records.plan_quantity IS
    'Quantidade planeada da linha do plano que casou, no momento da validação';

CREATE INDEX IF NOT EXISTS production_records_profile_idx
    ON mes_kanban.production_records (profile_type);

COMMIT;
