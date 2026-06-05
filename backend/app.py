# ─────────────────────────────────────────────────────────────────────────────
# app.py – Backend API des KVPCinematic Portfolios
#
# Framework  : Flask (Python)
# Laufzeit   : Docker Container auf Azure App Service (linux/amd64)
# Datenbank  : PostgreSQL (Azure Flexible Server)
# Speicher   : Azure Blob Storage (privat – Bilder werden über Proxy geliefert)
#
# ROLLEN-SYSTEM:
#   admin  → Alles: Fotos sehen/hochladen/löschen + Benutzer verwalten
#   user   → Nur eigene Fotos hochladen und sehen
#   viewer → Alle Fotos ansehen und filtern (kein Upload, kein Löschen)
#
# LOGS: Azure Portal → app-fotogalerie-prod → Log stream
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, jsonify, request, send_file, Response, g
from azure.storage.blob import BlobServiceClient
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from werkzeug.security import check_password_hash, generate_password_hash
import psycopg2, psycopg2.errors
import os, zipfile, io, logging, traceback
import requests as req
from functools import wraps
from user_agents import parse as ua_parse
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# ── Rate Limiter ───────────────────────────────────────────────────────────────
# Schützt die API gegen zu viele Anfragen von einer IP-Adresse.
# Standard: 200/Tag, 50/Stunde pro IP. Einzelne Endpunkte können enger begrenzt sein.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",          # Für Produktionsbetrieb: Redis empfohlen
)

# ── Bekannte Bot / KI-Crawler User-Agents ─────────────────────────────────────
# Diese Bots werden vor jeder Anfrage geblockt (403 Forbidden).
# Ausnahme: /health und /robots.txt bleiben offen (Azure-Monitoring).
BLOCKED_BOTS = [
    # KI-Trainingscrawler
    "gptbot", "chatgpt-user", "claude-web", "anthropic", "ccbot",
    "cohere-ai", "google-extended", "amazonbot", "bytespider",
    # SEO-Bots (für private App nicht erwünscht)
    "semrushbot", "ahrefsbot", "dotbot", "mj12bot", "yandexbot",
    "petalbot", "blexbot",
]

@app.before_request
def block_bots():
    """Blockiert bekannte Bot User-Agents vor jeder Route-Verarbeitung."""
    # Monitoring-Endpunkte bleiben immer offen
    if request.path in ("/health", "/robots.txt"):
        return None
    raw_ua = request.headers.get("User-Agent", "").lower()
    for bot in BLOCKED_BOTS:
        if bot in raw_ua:
            ip = (request.headers.get("X-Forwarded-For","").split(",")[0].strip()
                  or request.remote_addr)
            log.warning(f"[BLOCKED] Bot geblockt | Pattern='{bot}' | IP={ip}")
            return jsonify({"error": "Automatisierte Zugriffe sind nicht erlaubt."}), 403

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Umgebungsvariablen ─────────────────────────────────────────────────────────
# Müssen als Azure App Service Konfiguration gesetzt sein:
CONN_STR             = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
CONTAINER            = "photos"
DB_URL               = os.environ["DATABASE_URL"]
CDN_URL              = os.environ.get("CDN_URL", "")

# Ersteinrichtung: Wenn users-Tabelle leer ist, wird automatisch ein Admin angelegt.
# Diese Werte in Azure App Settings setzen → nach erstem Start wieder entfernen.
FIRST_ADMIN_USERNAME = os.environ.get("FIRST_ADMIN_USERNAME", "admin")
FIRST_ADMIN_PASSWORD = os.environ.get("FIRST_ADMIN_PASSWORD", "")  # leer = kein Auto-Setup

# ── Fehlercodes ────────────────────────────────────────────────────────────────
ERROR_CODES = {
    "E001": "Kein Foto im Request",
    "E002": "EXIF-Daten konnten nicht gelesen werden",
    "E003": "GPS-Koordinaten ungültig",
    "E004": "Reverse Geocoding fehlgeschlagen",
    "E005": "Azure Blob Storage Upload fehlgeschlagen",
    "E006": "Datenbankverbindung fehlgeschlagen",
    "E007": "Datenbankabfrage fehlgeschlagen",
    "E008": "Keine Foto-IDs für ZIP-Download angegeben",
    "E009": "Blob nicht gefunden",
    "E010": "Authentifizierung fehlgeschlagen",
    "E011": "ZIP-Erstellung fehlgeschlagen",
    "E013": "Unzureichende Berechtigungen",
}


# ─────────────────────────────────────────────────────────────────────────────
# HILFSFUNKTIONEN
# ─────────────────────────────────────────────────────────────────────────────

def get_client_info():
    """
    Liest IP-Adresse und User-Agent aus dem HTTP-Request.
    Hinter dem Azure Load Balancer steht die echte IP in X-Forwarded-For.
    Gibt (ip_string, lesbarer_client_string) zurück.
    """
    ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.headers.get("X-Real-IP", "")
        or request.remote_addr
        or "unbekannt"
    )
    raw_ua = request.headers.get("User-Agent", "")
    try:
        ua      = ua_parse(raw_ua)
        device  = ua.device.family
        os_str  = f"{ua.os.family} {ua.os.version_string}".strip()
        browser = ua.browser.family
        kind    = "📱 Mobil" if ua.is_mobile else ("💻 Desktop" if ua.is_pc else "🤖 Bot/Other")
        return ip, f"IP: {ip} | {kind} | Gerät: {device} | OS: {os_str} | Browser: {browser}"
    except Exception:
        return ip, f"IP: {ip} | UA: {raw_ua[:80]}"


def log_error(code, detail="", exception=None):
    """Einheitliches Fehler-Logging mit Fehlercode + Client-Info."""
    _, client = get_client_info()
    desc = ERROR_CODES.get(code, "Unbekannter Fehler")
    msg  = f"[{code}] {desc} | {client}"
    if detail:    msg += f" | Detail: {detail}"
    if exception: msg += f" | {type(exception).__name__}: {str(exception)}"
    log.error(msg)
    if exception and not isinstance(exception, (ValueError, KeyError)):
        log.debug(traceback.format_exc())
    return {"error_code": code, "error": desc, "detail": detail}


def log_info(action, detail=""):
    """Info-Log mit Client-Infos für jede erfolgreiche Aktion."""
    _, client = get_client_info()
    msg = f"[OK] {action} | {client}"
    if detail: msg += f" | {detail}"
    log.info(msg)


def log_warning(action, detail=""):
    """Warning-Log z.B. für fehlgeschlagene Logins oder Zugriffsversuche."""
    _, client = get_client_info()
    msg = f"[WARN] {action} | {client}"
    if detail: msg += f" | {detail}"
    log.warning(msg)


def get_db():
    """Öffnet eine neue PostgreSQL-Verbindung. Wirft ConnectionError bei Fehler."""
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        raise ConnectionError(log_error("E006", str(e), e)["error"])


# ─────────────────────────────────────────────────────────────────────────────
# AUTHENTIFIZIERUNG & AUTORISIERUNG
# ─────────────────────────────────────────────────────────────────────────────

def get_current_user():
    """
    Prüft HTTP Basic Auth Credentials gegen die users-Tabelle.
    Gibt ein User-Dict {id, username, role, display_name} zurück oder None.

    Sicherheit:
    - Passwörter werden als pbkdf2-Hash gespeichert (werkzeug.security)
    - Timing-sichere Vergleiche durch check_password_hash
    """
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
            return None    # Benutzer existiert nicht

        user_id, username, role, display_name, password_hash = row

        # Hash-Vergleich (timing-sicher, keine Brute-Force-Anfälligkeit)
        if not check_password_hash(password_hash, auth.password):
            return None    # Falsches Passwort

        return {
            "id":           user_id,
            "username":     username,
            "role":         role,
            "display_name": display_name or username
        }
    except Exception as e:
        log.error(f"[Auth] DB-Fehler: {e}")
        return None


def requires_auth(f):
    """
    Decorator: Schützt eine Route mit HTTP Basic Auth.
    Nach erfolgreicher Prüfung ist der Benutzer als g.user verfügbar.

    Verwendung:
        @app.route("/api/example")
        @requires_auth
        def example():
            user = g.user  # {'id': 1, 'username': 'admin', 'role': 'admin', ...}
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            tried = getattr(request.authorization, "username", "kein Header")
            log_warning("Authentifizierung fehlgeschlagen", f"Versuchter User: '{tried}'")
            return jsonify({"error_code": "E010", "error": ERROR_CODES["E010"]}), 401, {
                "WWW-Authenticate": 'Basic realm="KVPCinematic Portfolio"'
            }
        g.user = user
        return f(*args, **kwargs)
    return decorated


def create_first_admin_if_needed():
    """
    Ersteinrichtung: Legt den ersten Admin-Benutzer an wenn:
    1. Die users-Tabelle leer ist, UND
    2. FIRST_ADMIN_PASSWORD als Umgebungsvariable gesetzt ist.

    Wird bei jedem /health Aufruf geprüft (sehr günstig wenn count > 0).
    Nach dem ersten Start: FIRST_ADMIN_PASSWORD aus Azure App Settings entfernen!
    """
    if not FIRST_ADMIN_PASSWORD:
        return  # Kein Auto-Setup gewünscht
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        if count == 0:
            hashed = generate_password_hash(FIRST_ADMIN_PASSWORD)
            cur.execute(
                "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,'admin',%s)",
                (FIRST_ADMIN_USERNAME, hashed, "Administrator")
            )
            conn.commit()
            log.info(f"[SETUP] Erster Admin-Benutzer angelegt: '{FIRST_ADMIN_USERNAME}'")
        conn.close()
    except Exception as e:
        # Tabelle existiert noch nicht → wird ignoriert (nach DB-Migration verfügbar)
        log.warning(f"[SETUP] Ersteinrichtung übersprungen: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# EXIF & GEOCODING
# ─────────────────────────────────────────────────────────────────────────────

def get_exif_data(file):
    """
    Liest GPS-Koordinaten und Kamera-Infos aus den EXIF-Metadaten.
    Gibt (lat, lng, camera_make, camera_model, device_string) zurück.
    Alle Werte können None sein wenn keine EXIF-Daten vorhanden.
    """
    try:
        img      = Image.open(file)
        exif_raw = img._getexif()
        if not exif_raw:
            log.info(f"[INFO] Kein EXIF | Datei: {file.filename}")
            return None, None, None, None, None

        exif = {TAGS.get(tag, tag): value for tag, value in exif_raw.items()}

        # GPS-Koordinaten auslesen und in Dezimalgrad umrechnen
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
                    log.info(f"[INFO] GPS extrahiert | lat={lat}, lng={lng}")
                except Exception as e:
                    log_error("E003", f"Datei: {file.filename}", e)
                    lat, lng = None, None

        # Kamera-Hersteller und Modell
        make   = str(exif.get("Make",  "")).strip()
        model  = str(exif.get("Model", "")).strip()
        device = f"{make} {model}".strip() if (make or model) else None
        if device:
            log.info(f"[INFO] Kamera: {device}")

        return lat, lng, make or None, model or None, device

    except Exception as e:
        log_error("E002", f"Datei: {getattr(file, 'filename', '?')}", e)
        return None, None, None, None, None


def reverse_geocode(lat, lng):
    """
    Konvertiert GPS-Koordinaten in Ortsname via OpenStreetMap Nominatim.
    Gibt (city, country) zurück. Beide können leer sein bei Fehler.
    """
    try:
        r = req.get(
            f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json",
            headers={"User-Agent": "kvpcinematic-portfolio/1.0 (contact: admin@kvpcinematic.ch)"},
            timeout=5
        )
        r.raise_for_status()
        addr    = r.json().get("address", {})
        city    = addr.get("city") or addr.get("town") or addr.get("village", "")
        country = addr.get("country", "")
        log.info(f"[INFO] Geocoding: {city}, {country}")
        return city, country
    except req.exceptions.Timeout:
        log_error("E004", "Nominatim Timeout nach 5s")
        return "", ""
    except Exception as e:
        log_error("E004", f"lat={lat}, lng={lng}", e)
        return "", ""


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES – ALLGEMEIN
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    """
    Teilt Web-Crawlern mit, dass diese App nicht indexiert werden soll.
    Wird von seriösen Crawlern (Google, Bing) und KI-Bots (GPTBot etc.) respektiert.
    """
    content = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: GPTBot\nDisallow: /\n\n"
        "User-agent: Claude-Web\nDisallow: /\n\n"
        "User-agent: CCBot\nDisallow: /\n\n"
        "User-agent: Google-Extended\nDisallow: /\n\n"
        "User-agent: AmazonBot\nDisallow: /\n"
    )
    return Response(content, mimetype="text/plain")


@app.route("/health", methods=["GET"])
def health():
    """
    Health-Check ohne Authentifizierung.
    Wird von Azure App Service alle ~5 Minuten aufgerufen.
    Prüft auch ob der erste Admin angelegt werden muss (Ersteinrichtung).
    """
    create_first_admin_if_needed()   # Ersteinrichtung wenn nötig
    ip, client = get_client_info()
    log.info(f"[OK] Health-Check | {client}")
    return jsonify({"status": "ok"}), 200


@app.route("/api/me", methods=["GET"])
@requires_auth
def get_me():
    """
    Gibt Informationen über den aktuell eingeloggten Benutzer zurück.
    Wird vom Frontend nach dem Login aufgerufen um die Rolle zu bestimmen.
    """
    log_info("GET /api/me", f"User: {g.user['username']} | Rolle: {g.user['role']}")
    return jsonify({
        "id":           g.user["id"],
        "username":     g.user["username"],
        "role":         g.user["role"],
        "display_name": g.user["display_name"]
    })


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES – FOTOS
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/photos", methods=["GET"])
@requires_auth
@limiter.limit("60 per minute")
def list_photos():
    """
    Gibt alle sichtbaren Fotos zurück (rollenabhängig):
    - admin/viewer: alle Fotos
    - user: nur eigene Fotos (uploaded_by = aktuelle user.id)

    Query-Parameter (optional): city, country, device
    """
    city    = request.args.get("city")
    country = request.args.get("country")
    device  = request.args.get("device")
    user    = g.user

    log_info("GET /api/photos", f"city={city} country={country} device={device} | Rolle={user['role']}")

    try:
        conn = get_db()
        cur  = conn.cursor()

        # LEFT JOIN auf users um den Uploader-Namen zu erhalten
        query = """
            SELECT p.id, p.filename, p.url, p.city, p.country, p.lat, p.lng,
                   p.device, p.camera_make, p.camera_model,
                   p.uploaded_by, u.username AS uploader
            FROM   photos p
            LEFT JOIN users u ON p.uploaded_by = u.id
        """
        params     = []
        conditions = []

        # user-Rolle: nur eigene Fotos anzeigen
        if user["role"] == "user":
            conditions.append("p.uploaded_by = %s")
            params.append(user["id"])

        # Optionale Filter
        if city:    conditions.append("p.city = %s");    params.append(city)
        if country: conditions.append("p.country = %s"); params.append(country)
        if device:  conditions.append("p.device = %s");  params.append(device)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY p.created_at DESC"

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        log_info("Fotos geladen", f"{len(rows)} Einträge | Rolle={user['role']}")
        return jsonify([{
            "id":           r[0],  "filename": r[1], "url":     r[2],
            "city":         r[3],  "country":  r[4], "lat":     r[5],
            "lng":          r[6],  "device":   r[7], "camera_make": r[8],
            "camera_model": r[9],  "uploaded_by": r[10], "uploader": r[11]
        } for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "list_photos", e)), 500


@app.route("/api/locations", methods=["GET"])
@requires_auth
def get_locations():
    """
    Alle verfügbaren Orte für den Filter-Dropdown.
    user sieht nur Orte seiner eigenen Fotos.
    """
    user = g.user
    try:
        conn = get_db()
        cur  = conn.cursor()
        base = ("SELECT DISTINCT p.city, p.country FROM photos p "
                "WHERE p.city IS NOT NULL AND p.city != ''")
        if user["role"] == "user":
            cur.execute(base + " AND p.uploaded_by = %s ORDER BY p.country, p.city",
                        (user["id"],))
        else:
            cur.execute(base + " ORDER BY p.country, p.city")
        rows = cur.fetchall()
        conn.close()
        log_info("GET /api/locations", f"{len(rows)} Orte")
        return jsonify([{"city": r[0], "country": r[1]} for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "get_locations", e)), 500


@app.route("/api/devices", methods=["GET"])
@requires_auth
def get_devices():
    """
    Alle verfügbaren Geräte für den Filter-Dropdown.
    user sieht nur Geräte seiner eigenen Fotos.
    """
    user = g.user
    try:
        conn = get_db()
        cur  = conn.cursor()
        base = ("SELECT DISTINCT p.device FROM photos p "
                "WHERE p.device IS NOT NULL AND p.device != ''")
        if user["role"] == "user":
            cur.execute(base + " AND p.uploaded_by = %s ORDER BY p.device", (user["id"],))
        else:
            cur.execute(base + " ORDER BY p.device")
        rows = cur.fetchall()
        conn.close()
        log_info("GET /api/devices", f"{len(rows)} Geräte")
        return jsonify([{"device": r[0]} for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "get_devices", e)), 500


@app.route("/api/photos", methods=["POST"])
@requires_auth
@limiter.limit("30 per hour")
def upload_photo():
    """
    Foto hochladen → EXIF auslesen → Blob Storage → DB speichern.

    Berechtigungen:
    - admin: darf hochladen
    - user:  darf hochladen (Foto wird ihm zugeordnet)
    - viewer: KEIN Upload erlaubt (403)

    Multipart Form-Data: field "photo" (Bilddatei)
    """
    user = g.user

    # Viewer dürfen nicht hochladen
    if user["role"] == "viewer":
        return jsonify({"error": "Viewer dürfen keine Fotos hochladen"}), 403

    file = request.files.get("photo")
    if not file:
        return jsonify(log_error("E001")), 400

    log_info("Upload gestartet",
             f"Datei: {file.filename} | User: {user['username']} | Rolle: {user['role']}")

    # EXIF-Metadaten lesen (GPS + Kamera)
    lat, lng, camera_make, camera_model, device = get_exif_data(file)
    file.seek(0)  # Dateizeiger zurücksetzen nach dem Lesen

    # GPS → Ortsname (nur wenn Koordinaten vorhanden)
    city, country = "", ""
    if lat and lng:
        city, country = reverse_geocode(lat, lng)
    else:
        log.info(f"[INFO] Kein GPS-Daten → kein Geocoding | {file.filename}")

    # Upload zu Azure Blob Storage
    try:
        blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
        blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=file.filename)
        blob_client.upload_blob(file, overwrite=True)
        log_info("Blob hochgeladen", f"Container: {CONTAINER} | Datei: {file.filename}")
    except Exception as e:
        return jsonify(log_error("E005", file.filename, e)), 500

    # Öffentliche URL (CDN oder direkte Storage-URL)
    url = (f"{CDN_URL}/{file.filename}" if CDN_URL
           else f"https://{blob_svc.account_name}.blob.core.windows.net/{CONTAINER}/{file.filename}")

    # Foto in PostgreSQL speichern
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO photos
               (filename, url, lat, lng, city, country, device, camera_make, camera_model, uploaded_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (file.filename, url, lat, lng, city, country,
             device, camera_make, camera_model, user["id"])
        )
        photo_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        log_info("Foto in DB gespeichert",
                 f"id={photo_id} | city={city or '–'} | device={device or '–'} | uploader={user['username']}")
    except Exception as e:
        return jsonify(log_error("E007", "upload INSERT", e)), 500

    return jsonify({
        "id":      photo_id, "url":     url,
        "city":    city,     "country": country,
        "device":  device
    }), 201


@app.route("/api/photos/<int:photo_id>", methods=["DELETE"])
@requires_auth
def delete_photo(photo_id):
    """
    Foto löschen aus Blob Storage und Datenbank.

    Berechtigung: NUR admin (403 für alle anderen Rollen)

    Ablauf:
    1. Foto-Dateiname aus DB lesen
    2. Blob aus Azure Storage löschen
    3. Eintrag aus DB löschen
    """
    user = g.user

    # Nur Admins dürfen löschen
    if user["role"] != "admin":
        log_warning("Löschen verweigert",
                    f"User: {user['username']} | Rolle: {user['role']} | Benötigt: admin")
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

        # Blob aus Azure Storage löschen (Fehler ignorieren falls bereits gelöscht)
        try:
            blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
            blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
            blob_client.delete_blob()
            log_info("Blob gelöscht", f"id={photo_id} | {filename}")
        except Exception as e:
            log_warning("Blob nicht gelöscht (existiert evtl. nicht mehr)",
                        f"id={photo_id} | {e}")

        # Datenbankeintrag löschen
        cur.execute("DELETE FROM photos WHERE id = %s", (photo_id,))
        conn.commit()
        conn.close()

        log_info("Foto gelöscht", f"id={photo_id} | {filename} | Admin: {user['username']}")
        return jsonify({"success": True, "deleted_id": photo_id})

    except Exception as e:
        return jsonify(log_error("E007", f"delete_photo id={photo_id}", e)), 500


@app.route("/api/photos/<int:photo_id>/image", methods=["GET"])
@requires_auth
@limiter.limit("500 per hour")
def serve_photo(photo_id):
    """
    Bild sicher aus dem privaten Azure Blob Storage ausliefern.

    Da der Container NICHT öffentlich ist, muss jede Bildanfrage über
    diesen Endpunkt gehen. Das Frontend lädt Bilder via fetch() mit
    Authorization-Header und zeigt sie als Blob-URL an.

    Berechtigungen:
    - admin/viewer: alle Bilder
    - user: nur eigene Bilder

    Header: Cache-Control: private, max-age=3600 (1 Stunde Browser-Cache)
    """
    user = g.user
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT filename, uploaded_by FROM photos WHERE id = %s", (photo_id,))
        row  = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"error": "Foto nicht gefunden"}), 404

        filename, uploaded_by = row

        # user-Rolle: darf nur eigene Bilder sehen
        if user["role"] == "user" and uploaded_by != user["id"]:
            return jsonify({"error": "Kein Zugriff auf dieses Foto"}), 403

        # Bild aus Blob Storage laden
        blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
        blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
        data        = blob_client.download_blob().readall()

        # MIME-Type anhand Dateiendung bestimmen
        fn = filename.lower()
        if   fn.endswith(".png"):  mime = "image/png"
        elif fn.endswith(".gif"):  mime = "image/gif"
        elif fn.endswith(".webp"): mime = "image/webp"
        else:                      mime = "image/jpeg"

        response = send_file(io.BytesIO(data), mimetype=mime)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    except Exception as e:
        return jsonify(log_error("E009", f"serve_photo id={photo_id}", e)), 404


@app.route("/api/download", methods=["POST"])
@requires_auth
@limiter.limit("10 per hour")
def download_photos():
    """
    Mehrere Fotos als ZIP-Datei herunterladen.
    Body: { "ids": [1, 2, 3] }
    user darf nur eigene Fotos herunterladen.
    """
    user = g.user
    ids  = (request.json or {}).get("ids", [])
    if not ids:
        return jsonify(log_error("E008")), 400

    log_info("ZIP-Download", f"{len(ids)} Fotos | User: {user['username']}")

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
                    log_warning("ZIP: Foto nicht in DB", f"id={photo_id}")
                    continue

                filename, uploaded_by = row

                # Zugriffsrecht prüfen für user-Rolle
                if user["role"] == "user" and uploaded_by != user["id"]:
                    log_warning("ZIP: Zugriff verweigert", f"id={photo_id}")
                    continue

                try:
                    blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
                    blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
                    data        = blob_client.download_blob().readall()
                    zf.writestr(filename, data)
                    found += 1
                except Exception as e:
                    log_error("E009", f"id={photo_id} | {filename}", e)

        conn.close()
        log_info("ZIP fertig", f"{found}/{len(ids)} Fotos | {zip_buffer.tell()} Bytes")
        zip_buffer.seek(0)
        return send_file(
            zip_buffer, mimetype="application/zip",
            download_name="kvp-portfolio.zip", as_attachment=True
        )
    except Exception as e:
        return jsonify(log_error("E011", str(e), e)), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES – BENUTZERVERWALTUNG (nur admin)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/users", methods=["GET"])
@requires_auth
def list_users():
    """
    Alle Benutzer auflisten.
    Berechtigung: NUR admin
    """
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins können Benutzer verwalten"}), 403
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, username, role, display_name, created_at FROM users ORDER BY created_at"
        )
        rows = cur.fetchall()
        conn.close()
        log_info("GET /api/users", f"{len(rows)} Benutzer | Admin: {g.user['username']}")
        return jsonify([{
            "id":           r[0], "username": r[1],
            "role":         r[2], "display_name": r[3],
            "created_at":   str(r[4])
        } for r in rows])
    except Exception as e:
        return jsonify(log_error("E007", "list_users", e)), 500


@app.route("/api/users", methods=["POST"])
@requires_auth
@limiter.limit("20 per hour")
def create_user():
    """
    Neuen Benutzer anlegen.
    Berechtigung: NUR admin

    Body (JSON):
      username     – Eindeutiger Benutzername (erforderlich)
      password     – Passwort (min. 6 Zeichen, erforderlich)
      role         – 'admin' | 'user' | 'viewer' (Standard: 'viewer')
      display_name – Anzeigename (optional)
    """
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins können Benutzer anlegen"}), 403

    data         = request.json or {}
    username     = data.get("username", "").strip()
    password     = data.get("password", "")
    role         = data.get("role", "viewer")
    display_name = data.get("display_name", username)

    # Validierung
    if not username or not password:
        return jsonify({"error": "username und password sind erforderlich"}), 400
    if role not in ("admin", "user", "viewer"):
        return jsonify({"error": "Ungültige Rolle. Erlaubt: admin, user, viewer"}), 400
    if len(password) < 6:
        return jsonify({"error": "Passwort muss mindestens 6 Zeichen haben"}), 400

    try:
        hashed = generate_password_hash(password)
        conn   = get_db()
        cur    = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,%s,%s) RETURNING id",
            (username, hashed, role, display_name)
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        log_info("Benutzer angelegt",
                 f"id={new_id} | '{username}' | Rolle: {role} | Admin: {g.user['username']}")
        return jsonify({"id": new_id, "username": username, "role": role}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": f"Benutzername '{username}' existiert bereits"}), 409
    except Exception as e:
        return jsonify(log_error("E007", "create_user", e)), 500


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@requires_auth
def delete_user(user_id):
    """
    Benutzer löschen.
    Berechtigung: NUR admin
    Einschränkung: Man kann sich nicht selbst löschen.
    Hinweis: Fotos des gelöschten Benutzers bleiben erhalten (uploaded_by = NULL).
    """
    if g.user["role"] != "admin":
        return jsonify({"error": "Nur Admins können Benutzer löschen"}), 403
    if g.user["id"] == user_id:
        return jsonify({"error": "Du kannst dich nicht selbst löschen"}), 400

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
        row  = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Benutzer nicht gefunden"}), 404

        username = row[0]
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        conn.close()
        log_info("Benutzer gelöscht",
                 f"id={user_id} | '{username}' | Admin: {g.user['username']}")
        return jsonify({"success": True, "deleted_id": user_id})
    except Exception as e:
        return jsonify(log_error("E007", f"delete_user id={user_id}", e)), 500


@app.route("/api/users/<int:user_id>/password", methods=["PATCH"])
@requires_auth
@limiter.limit("10 per hour")
def change_password(user_id):
    """
    Passwort ändern.
    - Admin kann jedes Passwort ändern.
    - User kann nur das eigene Passwort ändern.

    Body: { "password": "neues-passwort" }
    """
    current = g.user
    if current["role"] != "admin" and current["id"] != user_id:
        return jsonify({"error": "Keine Berechtigung"}), 403

    new_pw = (request.json or {}).get("password", "")
    if len(new_pw) < 6:
        return jsonify({"error": "Passwort muss mindestens 6 Zeichen haben"}), 400

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(new_pw), user_id)
        )
        conn.commit()
        conn.close()
        log_info("Passwort geändert", f"user_id={user_id}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify(log_error("E007", "change_password", e)), 500


# ─────────────────────────────────────────────────────────────────────────────
# APP START
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("[OK] KVPCinematic Portfolio gestartet | Lokaler Dev-Server | Port 8000")
    app.run(host="0.0.0.0", port=8000, debug=False)
