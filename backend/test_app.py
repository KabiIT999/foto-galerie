# test_app.py – pytest-Tests fuer das Foto-Galerie Backend
# Testet alle API-Endpunkte mit gemockten Azure/DB-Abhaengigkeiten
# Ausfuehren: cd backend && python -m pytest -v

import pytest
import io
import json
from unittest.mock import patch, MagicMock

# Umgebungsvariablen setzen BEVOR app.py importiert wird
import os
os.environ.setdefault("AZURE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=dGVzdA==;EndpointSuffix=core.windows.net")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("APP_USERNAME", "testuser")
os.environ.setdefault("APP_PASSWORD", "testpass")

from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def auth_header():
    import base64
    token = base64.b64encode(b"testuser:testpass").decode()
    return {"Authorization": f"Basic {token}"}


# ── /health ────────────────────────────────────────────────────────────────────

def test_health_no_auth_required(client):
    """Health-Check braucht keine Authentifizierung."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"


# ── Authentifizierung ──────────────────────────────────────────────────────────

def test_photos_requires_auth(client):
    """GET /api/photos ohne Auth muss 401 zurueckgeben."""
    resp = client.get("/api/photos")
    assert resp.status_code == 401


def test_locations_requires_auth(client):
    """GET /api/locations ohne Auth muss 401 zurueckgeben."""
    resp = client.get("/api/locations")
    assert resp.status_code == 401


def test_devices_requires_auth(client):
    """GET /api/devices ohne Auth muss 401 zurueckgeben."""
    resp = client.get("/api/devices")
    assert resp.status_code == 401


def test_wrong_credentials(client):
    """Falsche Credentials muessen 401 liefern."""
    import base64
    token = base64.b64encode(b"wrong:credentials").decode()
    resp = client.get("/api/photos", headers={"Authorization": f"Basic {token}"})
    assert resp.status_code == 401


# ── GET /api/photos ────────────────────────────────────────────────────────────

@patch("app.get_db")
def test_list_photos_empty(mock_db, client):
    """GET /api/photos gibt leere Liste zurueck wenn keine Fotos vorhanden."""
    mock_conn = MagicMock()
    mock_cur  = MagicMock()
    mock_cur.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cur
    mock_db.return_value = mock_conn

    resp = client.get("/api/photos", headers=auth_header())
    assert resp.status_code == 200
    assert resp.get_json() == []


@patch("app.get_db")
def test_list_photos_with_data(mock_db, client):
    """GET /api/photos gibt Fotos korrekt zurueck."""
    mock_conn = MagicMock()
    mock_cur  = MagicMock()
    mock_cur.fetchall.return_value = [
        (1, "test.jpg", "https://example.com/test.jpg", "Zurich", "Switzerland", 47.3769, 8.5417, "iPhone 14", "Apple", "iPhone 14")
    ]
    mock_conn.cursor.return_value = mock_cur
    mock_db.return_value = mock_conn

    resp = client.get("/api/photos", headers=auth_header())
    assert resp.status_code == 200
    photos = resp.get_json()
    assert len(photos) == 1
    assert photos[0]["filename"] == "test.jpg"
    assert photos[0]["city"] == "Zurich"
    assert photos[0]["device"] == "iPhone 14"


@patch("app.get_db")
def test_list_photos_filter_city(mock_db, client):
    """GET /api/photos?city=... filtert korrekt (DB-Query wird mit Param aufgerufen)."""
    mock_conn = MagicMock()
    mock_cur  = MagicMock()
    mock_cur.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cur
    mock_db.return_value = mock_conn

    resp = client.get("/api/photos?city=Zurich", headers=auth_header())
    assert resp.status_code == 200
    # Pruefe dass execute mit city-Parameter aufgerufen wurde
    call_args = mock_cur.execute.call_args
    assert "Zurich" in call_args[0][1]


# ── GET /api/locations ─────────────────────────────────────────────────────────

@patch("app.get_db")
def test_get_locations(mock_db, client):
    """GET /api/locations gibt Orte korrekt zurueck."""
    mock_conn = MagicMock()
    mock_cur  = MagicMock()
    mock_cur.fetchall.return_value = [
        ("Zurich", "Switzerland"),
        ("Berlin", "Germany"),
    ]
    mock_conn.cursor.return_value = mock_cur
    mock_db.return_value = mock_conn

    resp = client.get("/api/locations", headers=auth_header())
    assert resp.status_code == 200
    locs = resp.get_json()
    assert len(locs) == 2
    assert locs[0]["city"] == "Zurich"
    assert locs[1]["country"] == "Germany"


# ── GET /api/devices ───────────────────────────────────────────────────────────

@patch("app.get_db")
def test_get_devices(mock_db, client):
    """GET /api/devices gibt Geraete korrekt zurueck."""
    mock_conn = MagicMock()
    mock_cur  = MagicMock()
    mock_cur.fetchall.return_value = [("Apple iPhone 14",), ("Samsung SM-S901",)]
    mock_conn.cursor.return_value = mock_cur
    mock_db.return_value = mock_conn

    resp = client.get("/api/devices", headers=auth_header())
    assert resp.status_code == 200
    devs = resp.get_json()
    assert len(devs) == 2
    assert devs[0]["device"] == "Apple iPhone 14"


# ── POST /api/photos ───────────────────────────────────────────────────────────

def test_upload_no_file(client):
    """POST /api/photos ohne Datei muss 400 zurueckgeben."""
    resp = client.post("/api/photos", headers=auth_header())
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error_code"] == "E001"


@patch("app.get_db")
@patch("app.BlobServiceClient")
def test_upload_photo(mock_blob_cls, mock_db, client):
    """POST /api/photos laedt Foto hoch und gibt 201 zurueck."""
    # DB-Mock
    mock_conn = MagicMock()
    mock_cur  = MagicMock()
    mock_cur.fetchone.return_value = (42,)
    mock_conn.cursor.return_value = mock_cur
    mock_db.return_value = mock_conn

    # Blob-Mock
    mock_blob_svc    = MagicMock()
    mock_blob_client = MagicMock()
    mock_blob_svc.get_blob_client.return_value = mock_blob_client
    mock_blob_svc.account_name = "testaccount"
    mock_blob_cls.from_connection_string.return_value = mock_blob_svc

    # Minimales JPEG (kein EXIF) als Bytes
    fake_img = io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    fake_img.name = "test.jpg"

    resp = client.post(
        "/api/photos",
        headers=auth_header(),
        data={"photo": (fake_img, "test.jpg")},
        content_type="multipart/form-data"
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["id"] == 42


# ── POST /api/download ─────────────────────────────────────────────────────────

def test_download_no_ids(client):
    """POST /api/download ohne IDs muss 400 zurueckgeben."""
    resp = client.post(
        "/api/download",
        headers={**auth_header(), "Content-Type": "application/json"},
        data=json.dumps({"ids": []})
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error_code"] == "E008"


@patch("app.get_db")
@patch("app.BlobServiceClient")
def test_download_zip(mock_blob_cls, mock_db, client):
    """POST /api/download erstellt ZIP und gibt es zurueck."""
    mock_conn = MagicMock()
    mock_cur  = MagicMock()
    mock_cur.fetchone.return_value = ("test.jpg",)
    mock_conn.cursor.return_value = mock_cur
    mock_db.return_value = mock_conn

    mock_blob_svc    = MagicMock()
    mock_blob_client = MagicMock()
    mock_blob_client.download_blob.return_value.readall.return_value = b"fake image data"
    mock_blob_svc.get_blob_client.return_value = mock_blob_client
    mock_blob_cls.from_connection_string.return_value = mock_blob_svc

    resp = client.post(
        "/api/download",
        headers={**auth_header(), "Content-Type": "application/json"},
        data=json.dumps({"ids": [1]})
    )
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
