-- Registo das migrações aplicadas ao schema mes_kanban.
--
-- Até aqui não havia forma de saber, olhando para a base, que migrações
-- estavam aplicadas — e a 017 existe em três versões (este repo, o dos perfis
-- e o DATARESEARCHMTG/sql/008), todas idempotentes e com os mesmos objetos.
-- A partir desta: este repo (cantoneiras) é o único dono do DDL mes_kanban,
-- como já dizia a 015, e cada migração nova termina com o seu INSERT abaixo.
--
-- O registo inicial só marca uma versão se os objetos que ela cria existirem
-- de facto: nunca se declara aplicada uma migração que não está.
-- Aplicar como dono do schema (não com mes_kanban_app, que só faz INSERT).
BEGIN;

CREATE TABLE IF NOT EXISTS mes_kanban.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    note       text
);

WITH evidence(version, present) AS (
    VALUES
    ('010_mes_kanban', to_regclass('mes_kanban.production_records') IS NOT NULL),
    ('011_mes_paragens', to_regclass('mes_kanban.stoppage_records') IS NOT NULL),
    ('012_perfil_e_marca', EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'mes_kanban' AND table_name = 'production_records'
          AND column_name = 'full_profile')),
    ('013_operador', EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'mes_kanban' AND table_name = 'validated_sheets'
          AND column_name = 'operator_match_rule')),
    ('014_metros', EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'mes_kanban' AND table_name = 'production_records'
          AND column_name = 'meters_produced')),
    ('015_family_perfis', EXISTS (SELECT 1 FROM pg_constraint
        WHERE conname = 'validated_sheets_family_check'
          AND pg_get_constraintdef(oid) LIKE '%perfis%')),
    ('016_mtg2_schema_v2', EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'mes_kanban' AND table_name = 'validated_sheets'
          AND column_name = 'source_page')),
    ('017_plan_binding_sheet_numbers',
        to_regclass('mes_kanban.validated_sheets_source_app_sheet_no_uidx') IS NOT NULL
        AND to_regclass('mes_kanban.production_record_plan_refs') IS NOT NULL)
)
INSERT INTO mes_kanban.schema_migrations(version, note)
SELECT version, 'aplicada antes do registo (verificada pelos objetos do schema)'
FROM evidence WHERE present
ON CONFLICT (version) DO NOTHING;

INSERT INTO mes_kanban.schema_migrations(version, note)
VALUES ('018_schema_migrations', NULL)
ON CONFLICT (version) DO NOTHING;

GRANT SELECT ON mes_kanban.schema_migrations TO mes_kanban_app;

COMMIT;
