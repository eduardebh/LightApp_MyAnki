-- 001_fix_schema.sql
-- Ejemplo de migración segura para Postgres.
-- AÑADE una columna `example_col` en `words` si no existe.

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='words' AND column_name='example_col'
    ) THEN
        ALTER TABLE words ADD COLUMN example_col TEXT;
    END IF;
END $$;
