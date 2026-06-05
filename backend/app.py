# ─────────────────────────────────────────────────────────────────────────────
# app.py – Backend API des KVPCinematic Portfolios
#
# Framework  : Flask (Python)
# Laufzeit   : Docker Container auf Azure App Service (linux/amd64)
# Datenbank  : PostgreSQL (Supabase / Azure Flexible Server)
# Speicher   : Azure Blob Storage (privat, Bilder via Proxy geliefert)
#
# ROLLEN-SYSTEM:
#   öffentlich  → Galerie ansehen, filtern (kein Login nötig)
#   admin  → Alles: hochladen, löschen, Favoriten, Benutzer verwalten
#   user   → Eigene Fotos hochladen und sehen
#   viewer → Alle Fotos sehen (Login optional)
#
# ÖFFENTLICHE ENDPUNKTE (kein Auth-Header nötig):
#   GET /api/photos, /api/photos/<id>/image, /api/locations, /api/devices, /api/tags
#
# NEUE FEATURE: Azure Computer Vision (optional)
#   Env-Vars: AZURE_CV_ENDPOINT, AZURE_CV_KEY
#   Bei Upload: Bild wird automatisch analysiert → Tags (Personen, Objekte, Szenen)
#   Ohne CV-Keys: Upload funktioniert normal, nur ohne automatische Tags
#
# SETUP Azure Computer Vision:
#   Azure Portal → Ressource erstellen → "Computer Vision" (oder "Azure AI Services")
#   Dann Endpoint + Key unter App Service → Konfiguration → Anwendungseinstellungen eintragen
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, jsonify, request, send_file, Response, g
from azure.storage.blob import BlobServiceClient
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from werkzeug.security import check_password_hash, generate_password_hash
import psycopg2, psycopg2.errors, psycopg2.extras
import os, zipfile, io, logging, traceback
import requests as req
from functools import wraps
from user_agents import parse as ua_parse
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# ── CORS ─────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return "", 200

# ── Rate Limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(
    get_remote_address, app=app,
    default_limits=["500 per day", "100 per hour"],
    storage_uri="memory://",
)

# ── Bot-Blocking ──────────────────────────────────────────────────────────────
BLOCKED_BOTS = [
    "gptbot", "chatgpt-user", "claude-web", "anthropic", "ccbot",
    "cohere-ai", "google-extended", "amazonbot", "bytespider",
    "semrushbot", "ahrefsbot", "dotbot", "mj12bot", "yandexbot", "petalbot",
]

@app.before_request
def block_bots():
    if request.path in ("/health", "/robots.txt"):
        return None
    raw_ua = request.headers.get("User-Agent", "").lower()
    for bot in BLOCKED_BOTS:
        if bot in raw_ua:
            ip = request.headers.get("X-Forwarded-For","").split(",")[0].strip() or request.remote_addr
            log.warning(f"[BLOCKED] Bot | pattern='{bot}' | ip={ip}")
            return jsonify({"error": "Automatisierter Zugriff nicht erlaubt"}), 403

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

# ── Umgebungsvariablen ────────────────────────────────────────────────────────
CONN_STR             = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
CONTAINER            = "photos"
DB_URL               = os.environ["DATABASE_URL"]
CDN_URL              = os.environ.get("CDN_URL", "")
FIRST_ADMIN_USERNAME = os.environ.get("FIRST_ADMIN_USERNAME", "admin")
FIRST_ADMIN_PASSWORD = os.environ.get("FIRST_ADMIN_PASSWORD", "")
# Azure Computer Vision (optional) – für automatische Personen/Objekt-Erkennung
AZURE_CV_ENDPOINT    = os.environ.get("AZURE_CV_ENDPOINT", "").rstrip("/")
AZURE_CV_KEY         = os.environ.get("AZURE_CV_KEY", "")

# ── Fehlercodes ──────────────────────────────────────────────────────────────
ERROR_CODES = {
    "E001": "Kein Foto im Request",
    "E002": "EXIF-Daten konnten nicht gelesen werden",
    "E003": "GPS-Koordinaten ungültig",
    "E004": "Reverse Geocoding fehlgeschlagen",
    "E005": "Azure Blob Storage Upload fehlgeschlagen",
    "E006": "Datenbankverbindung fehlgeschlagen",
    "E007": "Datenbankabfrage fehlgeschlagen",
    "E008": "Keine Foto-IDs angegeben",
    "E009": "Blob nicht gefunden",
    "E010": "Authentifizierung fehlgeschlagen",
    "E011": "ZIP-Erstellung fehlgeschlagen",
    "E013": "Unzureichende Berechtigungen",
}


# ─────────────────────────────────────────────────────────────────────────────
# HILFSFUNKTIONEN
# ─────────────────────────────────────────────────────────────────────────────

def get_client_info():
    ip = (request.headers.get("X-Forwarded-For","").split(",")[0].strip()
          or request.headers.get("X-Real-IP","")
          or request.remote_addr or "unbekannt")
    raw_ua = request.headers.get("User-Agent", "")
    try:
        ua      = ua_parse(raw_ua)
        kind    = "📱 Mobil" if ua.is_mobile else ("💻 Desktop" if ua.is_pc else "🤖 Bot")
        device  = ua.device.family
        os_str  = f"{ua.os.family} {ua.os.version_string}".strip()
        browser = ua.browser.family
        return ip, f"IP:{ip} | {kind} | {device} | {os_str} | {browser}"
    except Exception:
        return ip, f"IP:{ip} | UA:{raw_ua[:80]}"

def log_error(code, detail="", exception=None):
    _, client = get_client_info()
    desc = ERROR_CODES.get(code, "Unbekannter Fehler")
    msg  = f"[{code}] {desc} | {client}"
    if detail:    msg += f" | {detail}"
    if exception: msg += f" | {type(exception).__name__}: {str(exception)}"
    log.error(msg)
    return {"error_code": code, "error": desc, "detail": detail}

def log_info(action, detail=""):
    _, client = get_client_info()
    msg = f"[OK] {action} | {client}"
    if detail: msg += f" | {detail}"
    log.info(msg)

def log_warning(action, detail=""):
    _, client = get_client_info()
    log.warning(f"[WARN] {action} | {client}{(' | '+detail) if detail else ''}")

def get_db():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        raise ConnectionError(log_error("E006", str(e), e)["error"])


# ─────────────────────────────────────────────────────────────────────────────
# AZURE COMPUTER VISION – KI-Analyse (Personen & Objekte)
# ─────────────────────────────────────────────────────────────────────────────

def analyze_with_cv(image_bytes):
    """
    Azure Computer Vision v3.2: Bild analysieren → Tags zurückgeben.

    Erkennt: Personen, Objekte, Szenen, Tiere, Gebäude, Natur, etc.
    Gibt eine Liste von Tags (Strings) zurück, z.B. ["person", "outdoor", "tree"].

    Gibt leere Liste zurück wenn:
    - AZURE_CV_ENDPOINT oder AZURE_CV_KEY nicht gesetzt
    - Azure CV nicht erreichbar (Fehler wird nur geloggt, nicht geworfen)

    Konfidenzschwelle: 65% (nur zuverlässige Erkennungen)
    """
    if not AZURE_CV_ENDPOINT or not AZURE_CV_KEY:
        log.debug("[CV] Keine Azure CV Konfiguration → Analyse übersprungen")
        return []

    try:
        url = (f"{AZURE_CV_ENDPOINT}/vision/v3.2/analyze"
               f"?visualFeatures=Tags,Objects,Faces&language=en&model-version=latest")
        headers = {
            "Ocp-Apim-Subscription-Key": AZURE_CV_KEY,
            "Content-Type": "application/octet-stream"
        }
        resp = req.post(url, headers=headers, data=image_bytes, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        tags = []

        # 1) Visual Tags (Szenen, Objekte, Stimmungen)
        for t in data.get("tags", []):
            name       = t.get("name", "").strip().lower()
            confidence = t.get("confidence", 0)
            if name and confidence >= 0.65 and name not in tags:
                tags.append(name)

        # 2) Erkannte Objekte (häufig spezifischer als Tags)
        for obj in data.get("objects", []):
            name       = obj.get("object", "").strip().lower()
            confidence = obj.get("confidence", 0)
            if name and confidence >= 0.65 and name not in tags:
                tags.append(name)

        # 3) Gesichter erkannt → explizites "person"-Tag hinzufügen
        faces = data.get("faces", [])
        if len(faces) > 0 and "person" not in tags:
            tags.insert(0, "person")  # Person nach vorne setzen

        # Max 25 Tags, dedupliziert
        seen, result = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t)
                result.append(t)
            if len(result) >= 25:
                break

        log.info(f"[CV] Analyse: {len(faces)} Gesicht(er), {len(result)} Tags: {result[:8]}…")
        return result

    except req.exceptions.Timeout:
        log.warning("[CV] Timeout (>15s) – Azure CV nicht erreichbar")
        return []
    except Exception as e:
        log.warning(f"[CV] Analyse fehlgeschlagen: {type(e).__name__}: {e}")
        return []


def reanalyze_photo(photo_id, filename):
    """
    Existierendes Foto aus Blob Storage laden und CV-Analyse neu ausführen.
    Gibt Liste der neuen Tags zurück oder leere Liste bei Fehler.
    """
    try:
        blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
        blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
        image_bytes = blob_client.download_blob().readall()
        return analyze_with_cv(image_bytes)
    except Exception as e:
        log.warning(f"[CV] Reanalyse fehlgeschlagen id={photo_id}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# AUTHENTIFIZIERUNG
# ─────────────────────────────────────────────────────────────────────────────

def get_current_user():
    """HTTP Basic Auth prüfen. Gibt User-Dict zurück oder None."""
    auth = request.authorization
    if not auth:
        return None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, username, role, display_name, password_hash FROM users WHERE username = %s",
            (auth.username,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        uid, username, role, display_name, pw_hash = row
        if not check_password_hash(pw_hash, auth.password):
            return None
        return {"id": uid, "username": username, "role": role,
                "display_name": display_name or username}
    except Exception as e:
        log.error(f"[Auth] DB-Fehler: {e}")
        return None

def requires_auth(f):
    """Decorator: Route benötigt gültige Authentifizierung (setzt g.user)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            tried = getattr(request.authorization, "username", "–")
            log_warning("Auth fehlgeschlagen", f"tried='{tried}'")
            return jsonify({"error_code": "E010", "error": ERROR_CODES["E010"]}), 401, {
                "WWW-Authenticate": 'Basic realm="KVPCinematic Portfolio"'
            }
        g.user = user
        return f(*args, **kwargs)
    return decorated

def create_first_admin_if_needed():
    if not FIRST_ADMIN_PASSWORD:
        return
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] == 0:
            hashed = generate_password_hash(FIRST_ADMIN_PASSWORD)
            cur.execute(
                "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,'admin',%s)",
                (FIRST_ADMIN_USERNAME, hashed, "Administrator")
            )
            conn.commit()
            log.info(f"[SETUP] Erster Admin angelegt: '{FIRST_ADMIN_USERNAME}'")
        conn.close()
    except Exception as e:
        log.warning(f"[SETUP] Ersteinrichtung übersprungen: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# EXIF & GEOCODING
# ─────────────────────────────────────────────────────────────────────────────

def get_exif_data(file):
    """GPS + Kamera-Infos aus EXIF lesen."""
    try:
        img      = Image.open(file)
        exif_raw = img._getexif()
        if not exif_raw:
            return None, None, None, None, None
        exif = {TAGS.get(tag, tag): value for tag, value in exif_raw.items()}
        lat, lng = None, None
        if "GPSInfo" in exif:
            gps = {GPSTAGS.get(t, t): v for t, v in exif["GPSInfo"].items()}
            if "GPSLatitude" in gps and "GPSLongitude" in gps:
                try:
                    la  = gps["GPSLatitude"]
                    lo  = gps["GPSLongitude"]
                    lat = float(la[0]) + float(la[1])/60 + float(la[2])/3600
                    lng = float(lo[0]) + float(lo[1])/60 + float(lo[2])/3600
                    if gps.get("GPSLatitudeRef")  == "S": lat = -lat
                    if gps.get("GPSLongitudeRef") == "W": lng = -lng
                    lat, lng = round(lat, 6), round(lng, 6)
                except Exception:
                    lat, lng = None, None
        make   = str(exif.get("Make",  "")).strip()
        model  = str(exif.get("Model", "")).strip()
        device = f"{make} {model}".strip() if (make or model) else None
        return lat, lng, make or None, model or None, device
    except Exception as e:
        log_error("E002", f"{getattr(file,'filename','?')}", e)
        return None, None, None, None, None

def reverse_geocode(lat, lng):
    """GPS → Ortsname via OpenStreetMap Nominatim."""
    try:
        r = req.get(
            f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json",
            headers={"User-Agent": "kvpcinematic/1.0"}, timeout=5
        )
        r.raise_for_status()
        addr    = r.json().get("address", {})
        city    = addr.get("city") or addr.get("town") or addr.get("village", "")
        country = addr.get("country", "")
        return city, country
    except Exception as e:
        log_error("E004", f"lat={lat}, lng={lng}", e)
        return "", ""


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES – ALLGEMEIN
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/robots.txt")
def robots_txt():
    return Response(
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: GPTBot\nDisallow: /\n\n"
        "User-agent: Claude-Web\nDisallow: /\n\n"
        "User-agent: CCBot\nDisallow: /\n",
        mimetype="text/plain"
    )

@app.route("/health")
def health():
    create_first_admin_if_needed()
    cv_configured = bool(AZURE_CV_ENDPOINT and AZURE_CV_KEY)
    log.info(f"[OK] Health | cv_configured={cv_configured}")
    return jsonify({"status": "ok", "cv_enabled": cv_configured}), 200

@app.route("/api/me")
@requires_auth
def get_me():
    log_info("GET /api/me", f"user={g.user['username']} role={g.user['role']}")
    return jsonify(g.user)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES – FOTOS (öffentliche GET-Endpunkte)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/photos", methods=["GET"])
@limiter.limit("120 per minute")
def list_photos():
    """
    Öffentlicher Endpunkt: Gibt Fotos zurück ohne Login.
    Query-Params: city, country, device, tag, favorites_only=true
    """
    user       = get_current_user()
    city       = request.args.get("city")
    country    = request.args.get("country")
    device     = request.args.get("device")
    tag        = request.args.get("tag")          # KI-Tag Filter (z.B. "person")
    fav_only   = request.args.get("favorites_only") == "true"

    try:
        conn = get_db()
        cur  = conn.cursor()
        query = """
            SELECT p.id, p.filename, p.url, p.city, p.country, p.lat, p.lng,
                   p.device, p.camera_make, p.camera_model,
                   p.uploaded_by, u.username AS uploader,
                   COALESCE(p.is_favorite, false) AS is_favorite,
                   COALESCE(p.tags, ARRAY[]::TEXT[]) AS tags
            FROM   photos p
            LEFT JOIN users u ON p.uploaded_by = u.id
        """
        params, conditions = [], []

        # user-Rolle: nur eigene Fotos
        if user and user["role"] == "user":
            conditions.append("p.uploaded_by = %s")
            params.append(user["id"])

        if fav_only: conditions.append("COALESCE(p.is_favorite, false) = true")
        if city:     conditions.append("p.city = %s");    params.append(city)
        if country:  conditions.append("p.country = %s"); params.append(country)
        if device:   conditions.append("p.device = %s");  params.append(device)

        # KI-Tag-Filter: 'person' = ANY(tags)
        if tag:
            conditions.append("%s = ANY(COALESCE(p.tags, ARRAY[]::TEXT[]))")
            params.append(tag.lower())

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY p.created_at DESC"

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        return jsonify([{
            "id": r[0], "filename": r[1], "url": r[2],
            "city": r[3], "country": r[4], "lat": r[5], "lng": r[6],
            "device": r[7], "camera_make": r[8], "camera_model": r[9],
            "uploaded_by": r[10], "uploader": r[11],
            "is_favorite": r[12], "tags": list(r[13]) if r[13] else []
        } for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "list_photos", e)), 500


@app.route("/api/locations")
@limiter.limit("60 per minute")
def get_locations():
    """Öffentlich: Alle Orte für Filter-Dropdown."""
    user = get_current_user()
    try:
        conn = get_db()
        cur  = conn.cursor()
        base = ("SELECT DISTINCT p.city, p.country FROM photos p "
                "WHERE p.city IS NOT NULL AND p.city != ''")
        if user and user["role"] == "user":
            cur.execute(base + " AND p.uploaded_by = %s ORDER BY p.country, p.city", (user["id"],))
        else:
            cur.execute(base + " ORDER BY p.country, p.city")
        rows = cur.fetchall()
        conn.close()
        return jsonify([{"city": r[0], "country": r[1]} for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "get_locations", e)), 500


@app.route("/api/devices")
@limiter.limit("60 per minute")
def get_devices():
    """Öffentlich: Alle Geräte für Filter-Dropdown."""
    user = get_current_user()
    try:
        conn = get_db()
        cur  = conn.cursor()
        base = "SELECT DISTINCT p.device FROM photos p WHERE p.device IS NOT NULL AND p.device != ''"
        if user and user["role"] == "user":
            cur.execute(base + " AND p.uploaded_by = %s ORDER BY p.device", (user["id"],))
        else:
            cur.execute(base + " ORDER BY p.device")
        rows = cur.fetchall()
        conn.close()
        return jsonify([{"device": r[0]} for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "get_devices", e)), 500


@app.route("/api/tags")
@limiter.limit("60 per minute")
def get_tags():
    """
    Öffentlich: Alle vorhandenen KI-Tags mit Anzahl der Fotos.
    Wird für die Tag-Filter-Pills im Frontend verwendet.
    Rückgabe: [{tag: "person", count: 5}, ...]
    """
    try:
        conn = get_db()
        cur  = conn.cursor()
        # PostgreSQL: unnest(tags) expandiert das Array → GROUP BY zählt Vorkommen
        cur.execute("""
            SELECT tag, COUNT(*) as cnt
            FROM   photos, unnest(COALESCE(tags, ARRAY[]::TEXT[])) AS tag
            WHERE  tag != ''
            GROUP  BY tag
            ORDER  BY cnt DESC, tag ASC
            LIMIT  50
        """)
        rows = cur.fetchall()
        conn.close()
        return jsonify([{"tag": r[0], "count": int(r[1])} for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "get_tags", e)), 500


@app.route("/api/photos/<int:photo_id>/image")
@limiter.limit("2000 per hour")
def serve_photo(photo_id):
    """Öffentlicher Bild-Proxy: Liefert Bild aus privatem Azure Blob Storage."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT filename FROM photos WHERE id = %s", (photo_id,))
        row  = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Foto nicht gefunden"}), 404
        filename    = row[0]
        blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
        blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
        data        = blob_client.download_blob().readall()
        fn = filename.lower()
        if   fn.endswith(".png"):  mime = "image/png"
        elif fn.endswith(".gif"):  mime = "image/gif"
        elif fn.endswith(".webp"): mime = "image/webp"
        else:                      mime = "image/jpeg"
        response = send_file(io.BytesIO(data), mimetype=mime)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response
    except Exception as e:
        return jsonify(log_error("E009", f"id={photo_id}", e)), 404


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES – FOTOS (schreibende Operationen)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/photos", methods=["POST"])
@requires_auth
@limiter.limit("30 per hour")
def upload_photo():
    """
    Foto hochladen → EXIF → Blob Storage → Azure CV Analyse → DB.
    KI-Analyse läuft automatisch wenn AZURE_CV_ENDPOINT + AZURE_CV_KEY gesetzt.
    """
    user = g.user
    if user["role"] == "viewer":
        return jsonify({"error": "Viewer dürfen keine Fotos hochladen"}), 403

    file = request.files.get("photo")
    if not file:
        return jsonify(log_error("E001")), 400

    log_info("Upload", f"file={file.filename} user={user['username']}")

    # EXIF lesen
    lat, lng, camera_make, camera_model, device = get_exif_data(file)
    file.seek(0)

    # GPS → Ortsname
    city, country = "", ""
    if lat and lng:
        city, country = reverse_geocode(lat, lng)

    # Bild-Bytes für Azure Blob + CV Analyse lesen
    image_bytes = file.read()
    file.seek(0)

    # 1) Azure Blob Storage Upload
    try:
        blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
        blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=file.filename)
        blob_client.upload_blob(io.BytesIO(image_bytes), overwrite=True)
    except Exception as e:
        return jsonify(log_error("E005", file.filename, e)), 500

    # 2) Azure Computer Vision Analyse (KI-Tags, Personen, Objekte)
    tags = analyze_with_cv(image_bytes)

    url = (f"{CDN_URL}/{file.filename}" if CDN_URL
           else f"https://{blob_svc.account_name}.blob.core.windows.net/{CONTAINER}/{file.filename}")

    # 3) Datenbank-Eintrag
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO photos
               (filename, url, lat, lng, city, country, device, camera_make, camera_model, uploaded_by, tags)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (file.filename, url, lat, lng, city, country,
             device, camera_make, camera_model, user["id"], tags if tags else None)
        )
        photo_id = cur.fetchone()[0]
        conn.commit(); conn.close()
        log_info("Foto gespeichert", f"id={photo_id} city={city or '–'} tags={len(tags)}")
    except Exception as e:
        return jsonify(log_error("E007", "upload INSERT", e)), 500

    return jsonify({
        "id": photo_id, "url": url, "city": city,
        "country": country, "device": device, "tags": tags
    }), 201


@app.route("/api/photos/<int:photo_id>/analyze", methods=["POST"])
@requires_auth
def reanalyze(photo_id):
    """
    KI-Analyse für ein bestehendes Foto erneut ausführen (admin only).
    Nützlich für Fotos die vor der CV-Integration hochgeladen wurden.
    """
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins"}), 403
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT filename FROM photos WHERE id = %s", (photo_id,))
        row  = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Foto nicht gefunden"}), 404
        tags = reanalyze_photo(photo_id, row[0])
        cur.execute("UPDATE photos SET tags = %s WHERE id = %s", (tags or None, photo_id))
        conn.commit(); conn.close()
        log_info("Reanalyse", f"id={photo_id} tags={tags}")
        return jsonify({"id": photo_id, "tags": tags})
    except Exception as e:
        return jsonify(log_error("E007", f"reanalyze id={photo_id}", e)), 500


@app.route("/api/photos/reanalyze-all", methods=["POST"])
@requires_auth
def reanalyze_all():
    """
    Alle Fotos ohne Tags erneut mit Azure CV analysieren (admin only).
    Läuft synchron – kann bei vielen Fotos lange dauern.
    """
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins"}), 403
    if not AZURE_CV_ENDPOINT or not AZURE_CV_KEY:
        return jsonify({"error": "Azure Computer Vision nicht konfiguriert."}), 400
    try:
        conn = get_db()
        cur  = conn.cursor()
        # Nur Fotos ohne Tags analysieren
        cur.execute("SELECT id, filename FROM photos WHERE tags IS NULL OR tags = '{}' ORDER BY id DESC LIMIT 50")
        rows    = cur.fetchall()
        updated = 0
        for photo_id, filename in rows:
            tags = reanalyze_photo(photo_id, filename)
            if tags:
                cur.execute("UPDATE photos SET tags = %s WHERE id = %s", (tags, photo_id))
                updated += 1
        conn.commit(); conn.close()
        log_info("Reanalyse-All", f"{updated}/{len(rows)} Fotos analysiert")
        return jsonify({"analyzed": updated, "total": len(rows)})
    except Exception as e:
        return jsonify(log_error("E007", "reanalyze_all", e)), 500


@app.route("/api/photos/<int:photo_id>", methods=["DELETE"])
@requires_auth
def delete_photo(photo_id):
    """Einzelnes Foto löschen. Nur admin."""
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins können Fotos löschen"}), 403
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT filename FROM photos WHERE id = %s", (photo_id,))
        row  = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Foto nicht gefunden"}), 404
        filename = row[0]
        try:
            blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
            blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
            blob_client.delete_blob()
        except Exception as e:
            log_warning("Blob-Delete fehlgeschlagen", f"id={photo_id} | {e}")
        cur.execute("DELETE FROM photos WHERE id = %s", (photo_id,))
        conn.commit(); conn.close()
        log_info("Foto gelöscht", f"id={photo_id}")
        return jsonify({"success": True, "deleted_id": photo_id})
    except Exception as e:
        return jsonify(log_error("E007", f"delete id={photo_id}", e)), 500


@app.route("/api/photos/bulk-delete", methods=["POST"])
@requires_auth
def bulk_delete_photos():
    """Mehrere Fotos auf einmal löschen. Nur admin."""
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins"}), 403
    ids = (request.json or {}).get("ids", [])
    if not ids:
        return jsonify({"error": "Keine IDs angegeben"}), 400
    deleted, errors = [], []
    for photo_id in ids:
        try:
            conn = get_db()
            cur  = conn.cursor()
            cur.execute("SELECT filename FROM photos WHERE id = %s", (photo_id,))
            row  = cur.fetchone()
            if not row:
                errors.append(photo_id); conn.close(); continue
            try:
                blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
                blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=row[0])
                blob_client.delete_blob()
            except Exception:
                pass
            cur.execute("DELETE FROM photos WHERE id = %s", (photo_id,))
            conn.commit(); conn.close()
            deleted.append(photo_id)
        except Exception as e:
            errors.append(photo_id)
            log_error("E007", f"bulk-delete id={photo_id}", e)
    log_info("Bulk-Delete", f"{len(deleted)} gelöscht | {len(errors)} Fehler")
    return jsonify({"deleted": deleted, "errors": errors})


@app.route("/api/photos/<int:photo_id>/favorite", methods=["PATCH"])
@requires_auth
def toggle_favorite(photo_id):
    """Favoriten-Status umschalten."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT COALESCE(is_favorite, false) FROM photos WHERE id = %s", (photo_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Foto nicht gefunden"}), 404
        new_val = not row[0]
        cur.execute("UPDATE photos SET is_favorite = %s WHERE id = %s", (new_val, photo_id))
        conn.commit(); conn.close()
        return jsonify({"id": photo_id, "is_favorite": new_val})
    except Exception as e:
        return jsonify(log_error("E007", f"toggle_favorite id={photo_id}", e)), 500


@app.route("/api/download", methods=["POST"])
@requires_auth
@limiter.limit("10 per hour")
def download_photos():
    """Mehrere Fotos als ZIP herunterladen."""
    ids = (request.json or {}).get("ids", [])
    if not ids:
        return jsonify(log_error("E008")), 400
    try:
        conn       = get_db()
        cur        = conn.cursor()
        zip_buffer = io.BytesIO()
        found      = 0
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for photo_id in ids:
                cur.execute("SELECT filename, uploaded_by FROM photos WHERE id = %s", (photo_id,))
                row = cur.fetchone()
                if not row:
                    continue
                filename, uploaded_by = row
                if g.user["role"] == "user" and uploaded_by != g.user["id"]:
                    continue
                try:
                    blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
                    blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
                    zf.writestr(filename, blob_client.download_blob().readall())
                    found += 1
                except Exception as e:
                    log_error("E009", f"id={photo_id}", e)
        conn.close()
        zip_buffer.seek(0)
        return send_file(zip_buffer, mimetype="application/zip",
                         download_name="kvp-portfolio.zip", as_attachment=True)
    except Exception as e:
        return jsonify(log_error("E011", str(e), e)), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES – BENUTZERVERWALTUNG (nur admin)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/users")
@requires_auth
def list_users():
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins"}), 403
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT id, username, role, display_name, created_at FROM users ORDER BY created_at")
        rows = cur.fetchall()
        conn.close()
        return jsonify([{"id": r[0], "username": r[1], "role": r[2],
                         "display_name": r[3], "created_at": str(r[4])} for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "list_users", e)), 500


@app.route("/api/users", methods=["POST"])
@requires_auth
@limiter.limit("20 per hour")
def create_user():
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins"}), 403
    data         = request.json or {}
    username     = data.get("username", "").strip()
    password     = data.get("password", "")
    role         = data.get("role", "viewer")
    display_name = data.get("display_name", username)
    if not username or not password:
        return jsonify({"error": "username und password erforderlich"}), 400
    if role not in ("admin", "user", "viewer"):
        return jsonify({"error": "Ungültige Rolle"}), 400
    if len(password) < 6:
        return jsonify({"error": "Passwort mind. 6 Zeichen"}), 400
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,%s,%s) RETURNING id",
            (username, generate_password_hash(password), role, display_name)
        )
        new_id = cur.fetchone()[0]
        conn.commit(); conn.close()
        log_info("Benutzer angelegt", f"id={new_id} '{username}' {role}")
        return jsonify({"id": new_id, "username": username, "role": role}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": f"Benutzername '{username}' existiert bereits"}), 409
    except Exception as e:
        return jsonify(log_error("E007", "create_user", e)), 500


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@requires_auth
def delete_user(user_id):
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins"}), 403
    if g.user["id"] == user_id:
        return jsonify({"error": "Kann sich nicht selbst löschen"}), 400
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
        row  = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Benutzer nicht gefunden"}), 404
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit(); conn.close()
        log_info("Benutzer gelöscht", f"id={user_id} '{row[0]}'")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify(log_error("E007", f"delete_user id={user_id}", e)), 500


@app.route("/api/users/<int:user_id>/password", methods=["PATCH"])
@requires_auth
@limiter.limit("10 per hour")
def change_password(user_id):
    current = g.user
    if current["role"] != "admin" and current["id"] != user_id:
        return jsonify({"error": "Keine Berechtigung"}), 403
    new_pw = (request.json or {}).get("password", "")
    if len(new_pw) < 6:
        return jsonify({"error": "Passwort mind. 6 Zeichen"}), 400
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (generate_password_hash(new_pw), user_id))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify(log_error("E007", "change_password", e)), 500


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("[OK] KVPCinematic Portfolio gestartet")
    app.run(host="0.0.0.0", port=8000, debug=False)
