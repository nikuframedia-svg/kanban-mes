-- A família «perfis» (setor MTG2: serrote + Vanguard) passa a poder validar
-- folhas no mes_kanban. O schema é partilhado pelas duas apps kanban; este
-- repo (cantoneiras) é o dono das migrações.
BEGIN;

ALTER TABLE mes_kanban.validated_sheets
    DROP CONSTRAINT validated_sheets_family_check;
ALTER TABLE mes_kanban.validated_sheets
    ADD CONSTRAINT validated_sheets_family_check
    CHECK (family IN ('chapa', 'cantoneiras', 'perfis'));

COMMIT;
