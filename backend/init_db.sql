-- ─────────────────────────────────────────────────────────────────────────────
-- init_db.sql – KVPCinematic Portfolio Datenbankschema v2
--
-- AUSFÜHREN: Azure Portal → PostgreSQL → Query Editor → dieses Script einfügen
--
-- ACHTUNG: Löscht bestehende Tabellen (inkl. Daten)!
-- Für bestehende Installationen: migrate_v2.sql verwenden (nur ALTER TABLE).
-- ─────────────────────────────────────────────────────────────────────────────

-- Reihenfolge wichtig: zuerst Tabelle mit Fremdschlüssel löschen
DROP TABLE IF EXISTS photos CASCADE;
DROP TABLE IF EXISTS users  CASCADE;

-- ─────────────────────────────────────────────────────────────────────────────
-- TABELLE: users
--
-- Rollen:
--   admin  → Alle Fotos sehen/hochladen/löschen + Benutzer verwalten
--   user   → Nur eigene Fotos hochladen und sehen
--   viewer → Alle Fotos ansehen und filtern (kein Upload, kein Löschen)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE users (
    id            SERIAL        PRIMARY KEY,
    username      VARCHAR(100)  UNIQUE NOT NULL,
    password_hash VARCHAR(255)  NOT NULL,                    -- pbkdf2/bcrypt via werkzeug
    role          VARCHAR(20)   NOT NULL DEFAULT 'viewer'
                  CHECK (role IN ('admin', 'user', 'viewer')),
    display_name  VARCHAR(200),                              -- Anzeigename (optional)
    created_at    TIMESTAMP     DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- TABELLE: photos
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE photos (
    id           SERIAL           PRIMARY KEY,
    filename     VARCHAR(500)     NOT NULL,
    url          TEXT             NOT NULL,
    lat          DOUBLE PRECISION,                           -- GPS Breitengrad
    lng          DOUBLE PRECISION,                           -- GPS Längengrad
    city         VARCHAR(200),
    country      VARCHAR(200),
    device       VARCHAR(200),                               -- "Apple iPhone 16 Pro Max"
    camera_make  VARCHAR(100),                               -- EXIF Make
    camera_model VARCHAR(100),                               -- EXIF Model
    uploaded_by  INTEGER REFERENCES users(id)                -- Wer hat hochgeladen?
                 ON DELETE SET NULL,
    created_at   TIMESTAMP DEFAULT NOW()
);

-- Indizes für schnellere Filter-Abfragen
CREATE INDEX idx_photos_city        ON photos(city);
CREATE INDEX idx_photos_country     ON photos(country);
CREATE INDEX idx_photos_device      ON photos(device);
CREATE INDEX idx_photos_uploaded_by ON photos(uploaded_by);
CREATE INDEX idx_users_username     ON users(username);

-- ─────────────────────────────────────────────────────────────────────────────
-- ERSTER ADMIN-BENUTZER
-- Die App legt den Admin automatisch an, wenn:
--   - Die users-Tabelle leer ist, UND
--   - FIRST_ADMIN_PASSWORD als Azure App Setting gesetzt ist
--
-- Azure Portal → App Service → Konfiguration → Anwendungseinstellungen:
--   FIRST_ADMIN_USERNAME = admin          (oder wähle einen anderen Namen)
--   FIRST_ADMIN_PASSWORD = <sicheres-passwort>
--
-- Der Admin wird beim nächsten /health Aufruf automatisch angelegt.
-- Danach solltest du FIRST_ADMIN_PASSWORD aus den Einstellungen entfernen.
-- ─────────────────────────────────────────────────────────────────────────────
