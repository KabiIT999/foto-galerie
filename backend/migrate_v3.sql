-- ─────────────────────────────────────────────────────────────────────────────
-- migrate_v3.sql – Migration für bestehende Datenbanken (KVPCinematic v3)
--
-- NEU: is_favorite Spalte (Favoriten-Funktion per Herz-Button)
--
-- AUSFÜHREN:
--   Supabase: SQL Editor → Dieses Script einfügen → Run
--   Azure:    PostgreSQL → Query Editor → Script einfügen
--
-- Sicher: IF NOT EXISTS → kann mehrfach ausgeführt werden
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE photos
  ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE;

-- Optional: Index für schnelle Favoriten-Abfragen
CREATE INDEX IF NOT EXISTS idx_photos_favorite ON photos(is_favorite);

-- Bestätigung anzeigen
SELECT 'Migration v3 erfolgreich: is_favorite Spalte vorhanden' AS status;
