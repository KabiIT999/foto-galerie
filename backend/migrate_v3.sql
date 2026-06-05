-- ─────────────────────────────────────────────────────────────────────────────
-- migrate_v3.sql – Migration für bestehende Datenbanken (KVPCinematic v3)
--
-- NEU in v3:
--   1. is_favorite – Favoriten-Markierung per Herz-Button
--   2. tags        – KI-erkannte Tags (Azure Computer Vision)
--                    z.B. ARRAY['person', 'outdoor', 'tree']
--
-- AUSFÜHREN:
--   Supabase: SQL Editor → Dieses Script einfügen → Run
--   Azure:    PostgreSQL → Query Editor → Script einfügen
--
-- SICHER: IF NOT EXISTS → kann mehrfach ausgeführt werden ohne Datenverlust
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Favoriten-Spalte
ALTER TABLE photos
  ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE;

-- 2. KI-Tags-Spalte (PostgreSQL Text-Array)
ALTER TABLE photos
  ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT ARRAY[]::TEXT[];

-- Indizes für Performance
CREATE INDEX IF NOT EXISTS idx_photos_favorite ON photos(is_favorite);
CREATE INDEX IF NOT EXISTS idx_photos_tags     ON photos USING GIN(tags);

-- Bestätigung
SELECT
  'Migration v3 OK' AS status,
  COUNT(*) AS fotos_gesamt,
  COUNT(*) FILTER (WHERE is_favorite)           AS favoriten,
  COUNT(*) FILTER (WHERE tags IS NOT NULL AND tags <> '{}') AS mit_ki_tags
FROM photos;
