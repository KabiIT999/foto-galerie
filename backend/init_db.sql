CREATE TABLE IF NOT EXISTS photos (
    id           SERIAL PRIMARY KEY,
    filename     VARCHAR(255) NOT NULL,
    url          TEXT NOT NULL,
    lat          FLOAT,
    lng          FLOAT,
    city         VARCHAR(100),
    country      VARCHAR(100),
    device       VARCHAR(200),
    camera_make  VARCHAR(100),
    camera_model VARCHAR(100),
    created_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_photos_city    ON photos(city);
CREATE INDEX IF NOT EXISTS idx_photos_country ON photos(country);
CREATE INDEX IF NOT EXISTS idx_photos_device  ON photos(device);