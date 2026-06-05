# ─────────────────────────────────────────────────────────────────────────────
# app.py – Backend API der Foto-Galerie
# Framework: Flask (Python) · Läuft als Docker Container auf Azure App Service
# Logs: Azure Portal → app-fotogalerie-prod → Log stream
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, jsonify, request, send_file, Response
from azure.storage.blob import BlobServiceClient
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import psycopg2
import os, zipfile, io, logging, traceback
import requests as req
from functools import wraps
from user_agents import parse as ua_parse   # pip install pyyaml ua-parser user-agents
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# ── Rate Limiter ──────────────────────────────────────────────────────────────
# Schützt die API vor zu vielen Anfragen von einer IP-Adresse.
# Standard: 200 Anfragen/Tag, 50/Stunde, 10/Minute pro IP
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# ── Bekannte Bot / KI-Crawler User-Agents blockieren ─────────────────────────
# Diese Liste enthält Bots die Inhalte scrapen oder für KI-Training sammeln.
BLOCKED_BOTS = [
    # KI-Trainingscrawler
    "gptbot", "chatgpt", "claude-web", "anthropic", "ccbot",
    "cohere-ai", "google-extended", "amazonbot",
    "bytespider", "petalbot", "applebot",
    # SEO / allgemeine Crawler (für private App nicht erwünscht)
    "semrushbot", "ahrefsbot", "dotbot", "mj12bot",
    "blexbot", "seznambot", "yandexbot",
]

@app.before_request
def block_bots():
    """
    Blockiert bekannte Bot-User-Agents vor jeder Anfrage.
    Ausnahme: /health darf von Monitoring-Tools (curl, Azure) aufgerufen werden.
    """
    # Health-Endpoint bleibt offen für Azure-Monitoring
    if request.path == "/health":
        return None

    raw_ua = request.headers.get("User-Agent", "").lower()
    for bot in BLOCKED_BOTS:
        if bot in raw_ua:
            ip, client = get_client_info()
            log.warning(f"[BLOCKED] Bot geblockt | Bot-Pattern: '{bot}' | {client}")
            return jsonify({
                "error": "Zugriff verweigert",
                "detail": "Automatisierte Zugriffe sind nicht erlaubt."
            }), 403

# ── Logging-Konfiguration ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Umgebungsvariablen ────────────────────────────────────────────────────────
CONN_STR     = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
CONTAINER    = "photos"
DB_URL       = os.environ["DATABASE_URL"]
CDN_URL      = os.environ.get("CDN_URL", "")
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "foto2024")

# ── Fehlercodes ───────────────────────────────────────────────────────────────
ERROR_CODES = {
    "E001": "Kein Foto im Request",
    "E002": "EXIF-Daten konnten nicht gelesen werden",
    "E003": "GPS-Koordinaten ungültig",
    "E004": "Reverse Geocoding fehlgeschlagen (OpenStreetMap)",
    "E005": "Azure Blob Storage Upload fehlgeschlagen",
    "E006": "Datenbankverbindung fehlgeschlagen",
    "E007": "Datenbankabfrage fehlgeschlagen",
    "E008": "Keine Foto-IDs für ZIP-Download angegeben",
    "E009": "Blob für ZIP nicht gefunden",
    "E010": "Authentifizierung fehlgeschlagen",
    "E011": "ZIP-Erstellung fehlgeschlagen",
}


# ── Client-Infos aus Request holen (IP + Browser/Gerät) ──────────────────────
def get_client_info():
    """
    Liest IP-Adresse und User-Agent aus dem HTTP-Request.
    Hinter Azure Load Balancer steht die echte IP in X-Forwarded-For.
    Gibt einen lesbaren String zurück z.B.:
      IP: 144.2.95.66 | Gerät: iPhone / iOS 17.4 | Browser: Mobile Safari
    """
    # IP: Azure setzt X-Forwarded-For, sonst direkte Verbindung
    ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.headers.get("X-Real-IP", "")
        or request.remote_addr
        or "unbekannt"
    )

    # User-Agent parsen → Gerät / OS / Browser
    raw_ua = request.headers.get("User-Agent", "")
    try:
        ua     = ua_parse(raw_ua)
        device = ua.device.family       # z.B. "iPhone", "Samsung SM-S901", "Other"
        os_    = f"{ua.os.family} {ua.os.version_string}".strip()   # z.B. "iOS 17.4"
        browser = ua.browser.family     # z.B. "Mobile Safari", "Chrome"
        is_mobile = "📱 Mobil" if ua.is_mobile else ("💻 Desktop" if ua.is_pc else "🤖 Bot/Other")
        client_str = f"IP: {ip} | {is_mobile} | Gerät: {device} | OS: {os_} | Browser: {browser}"
    except Exception:
        # Falls ua-parser nicht installiert → nur IP loggen
        client_str = f"IP: {ip} | User-Agent: {raw_ua[:80]}"

    return ip, client_str


# ── Einheitliche Logging-Hilfsfunktionen ─────────────────────────────────────
def log_error(code, detail="", exception=None):
    """Fehler mit Code, Beschreibung, Client-Info und optionalem Stacktrace."""
    _, client = get_client_info()
    description = ERROR_CODES.get(code, "Unbekannter Fehler")
    msg = f"[{code}] {description} | {client}"
    if detail:
        msg += f" | Detail: {detail}"
    if exception:
        msg += f" | Exception: {type(exception).__name__}: {str(exception)}"
    log.error(msg)
    if exception and not isinstance(exception, (ValueError, KeyError)):
        log.debug(traceback.format_exc())
    return {"error_code": code, "error": description, "detail": detail}


def log_info(action, detail=""):
    """Info-Log mit Client-IP und Gerät für jeden Schritt."""
    _, client = get_client_info()
    msg = f"[OK] {action} | {client}"
    if detail:
        msg += f" | {detail}"
    log.info(msg)


def log_warning(action, detail=""):
    """Warning-Log mit Client-Info."""
    _, client = get_client_info()
    msg = f"[WARN] {action} | {client}"
    if detail:
        msg += f" | {detail}"
    log.warning(msg)


# ── Datenbankverbindung ───────────────────────────────────────────────────────
def get_db():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        raise ConnectionError(log_error("E006", str(e), e)["error"])


# ── HTTP Basic Auth ───────────────────────────────────────────────────────────
def check_auth(username, password):
    return username == APP_USERNAME and password == APP_PASSWORD


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            user_tried = auth.username if auth else "kein Header"
            log_warning(
                f"[E010] {ERROR_CODES['E010']}",
                f"Versuchter Benutzer: '{user_tried}'"
            )
            return jsonify({"error_code": "E010", "error": ERROR_CODES["E010"]}), 401, {
                "WWW-Authenticate": 'Basic realm="Foto Galerie"'
            }
        log_info("Login erfolgreich", f"User: '{auth.username}'")
        return f(*args, **kwargs)
    return decorated


# ── EXIF-Daten auslesen (GPS + Kamera/Gerät) ─────────────────────────────────
def get_exif_data(file):
    """Liest GPS-Koordinaten und Geräteinfos aus EXIF-Metadaten des Fotos."""
    try:
        img      = Image.open(file)
        exif_raw = img._getexif()
        if not exif_raw:
            log.info(f"[INFO] Kein EXIF in Datei: {file.filename}")
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
                    log.info(f"[INFO] GPS extrahiert | lat={lat}, lng={lng} | Datei: {file.filename}")
                except Exception as e:
                    log_error("E003", f"Datei: {file.filename}", e)
                    lat, lng = None, None

        make   = str(exif.get("Make",  "")).strip()
        model  = str(exif.get("Model", "")).strip()
        device = f"{make} {model}".strip() if (make or model) else None
        if device:
            log.info(f"[INFO] Kamera erkannt | {device} | Datei: {file.filename}")

        return lat, lng, make or None, model or None, device

    except Exception as e:
        log_error("E002", f"Datei: {getattr(file, 'filename', '?')}", e)
        return None, None, None, None, None


# ── Reverse Geocoding ─────────────────────────────────────────────────────────
def reverse_geocode(lat, lng):
    """Konvertiert GPS-Koordinaten via OpenStreetMap in Ortsname."""
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json"
        r   = req.get(url, headers={"User-Agent": "foto-galerie/1.0"}, timeout=5)
        r.raise_for_status()
        addr    = r.json().get("address", {})
        city    = addr.get("city") or addr.get("town") or addr.get("village", "")
        country = addr.get("country", "")
        log.info(f"[INFO] Geocoding erfolgreich | {city}, {country} | lat={lat}, lng={lng}")
        return city, country
    except req.exceptions.Timeout:
        log_error("E004", "Nominatim Timeout nach 5s")
        return "", ""
    except Exception as e:
        log_error("E004", f"lat={lat}, lng={lng}", e)
        return "", ""


# ── GET /robots.txt ───────────────────────────────────────────────────────────
@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    """
    Teilt Web-Crawlern mit, dass sie diese App nicht indexieren sollen.
    Wird von seriösen Crawlern (Google, Bing etc.) respektiert.
    """
    content = (
        "User-agent: *\n"
        "Disallow: /\n\n"
        "# KI-Trainingscrawler explizit ausschliessen\n"
        "User-agent: GPTBot\n"
        "Disallow: /\n\n"
        "User-agent: Claude-Web\n"
        "Disallow: /\n\n"
        "User-agent: CCBot\n"
        "Disallow: /\n\n"
        "User-agent: Google-Extended\n"
        "Disallow: /\n\n"
        "User-agent: AmazonBot\n"
        "Disallow: /\n"
    )
    return Response(content, mimetype="text/plain")


# ── GET /api/photos ───────────────────────────────────────────────────────────
@app.route("/api/photos", methods=["GET"])
@requires_auth
@limiter.limit("30 per minute")
def list_photos():
    city    = request.args.get("city")
    country = request.args.get("country")
    device  = request.args.get("device")
    log_info("GET /api/photos", f"Filter: city={city}, country={country}, device={device}")
    try:
        conn       = get_db()
        cur        = conn.cursor()
        query      = "SELECT id, filename, url, city, country, lat, lng, device, camera_make, camera_model FROM photos"
        params     = []
        conditions = []
        if city:    conditions.append("city = %s");    params.append(city)
        if country: conditions.append("country = %s"); params.append(country)
        if device:  conditions.append("device = %s");  params.append(device)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()
        log_info("Fotos geladen", f"{len(rows)} Einträge zurückgegeben")
        return jsonify([{
            "id": r[0], "filename": r[1], "url": r[2],
            "city": r[3], "country": r[4], "lat": r[5], "lng": r[6],
            "device": r[7], "camera_make": r[8], "camera_model": r[9]
        } for r in rows])
    except Exception as e:
        err = log_error("E007", "list_photos", e)
        return jsonify(err), 500


# ── GET /api/locations ────────────────────────────────────────────────────────
@app.route("/api/locations", methods=["GET"])
@requires_auth
def get_locations():
    log_info("GET /api/locations")
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT DISTINCT city, country FROM photos
            WHERE city IS NOT NULL AND city != ''
            ORDER BY country, city
        """)
        rows = cur.fetchall()
        conn.close()
        log_info("Orte geladen", f"{len(rows)} Einträge")
        return jsonify([{"city": r[0], "country": r[1]} for r in rows])
    except Exception as e:
        err = log_error("E007", "get_locations", e)
        return jsonify(err), 500


# ── GET /api/devices ──────────────────────────────────────────────────────────
@app.route("/api/devices", methods=["GET"])
@requires_auth
def get_devices():
    log_info("GET /api/devices")
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT DISTINCT device FROM photos
            WHERE device IS NOT NULL AND device != ''
            ORDER BY device
        """)
        rows = cur.fetchall()
        conn.close()
        log_info("Geräte geladen", f"{len(rows)} Einträge")
        return jsonify([{"device": r[0]} for r in rows])
    except Exception as e:
        err = log_error("E007", "get_devices", e)
        return jsonify(err), 500


# ── POST /api/photos ──────────────────────────────────────────────────────────
@app.route("/api/photos", methods=["POST"])
@requires_auth
@limiter.limit("20 per hour")
def upload_photo():
    file = request.files.get("photo")
    if not file:
        err = log_error("E001")
        return jsonify(err), 400

    log_info("Upload gestartet", f"Datei: {file.filename} | Grösse: {request.content_length or '?'} Bytes")

    # EXIF auslesen
    lat, lng, camera_make, camera_model, device = get_exif_data(file)
    file.seek(0)

    # Geocoding
    city, country = ("", "")
    if lat and lng:
        city, country = reverse_geocode(lat, lng)
    else:
        log.info(f"[INFO] Kein GPS → kein Geocoding | Datei: {file.filename}")

    # Azure Blob Storage
    try:
        blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
        blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=file.filename)
        blob_client.upload_blob(file, overwrite=True)
        log_info("Blob hochgeladen", f"Container: {CONTAINER} | Datei: {file.filename}")
    except Exception as e:
        err = log_error("E005", file.filename, e)
        return jsonify(err), 500

    # URL
    if CDN_URL:
        url = f"{CDN_URL}/{file.filename}"
    else:
        url = f"https://{blob_svc.account_name}.blob.core.windows.net/{CONTAINER}/{file.filename}"

    # DB speichern
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO photos
               (filename, url, lat, lng, city, country, device, camera_make, camera_model)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (file.filename, url, lat, lng, city, country, device, camera_make, camera_model)
        )
        photo_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        log_info("Foto gespeichert in DB",
                 f"id={photo_id} | city={city or '–'} | device={device or '–'} | url={url}")
    except Exception as e:
        err = log_error("E007", "upload INSERT", e)
        return jsonify(err), 500

    return jsonify({"id": photo_id, "url": url, "city": city, "country": country, "device": device}), 201


# ── POST /api/download ────────────────────────────────────────────────────────
@app.route("/api/download", methods=["POST"])
@requires_auth
@limiter.limit("10 per hour")
def download_photos():
    ids = request.json.get("ids", [])
    if not ids:
        err = log_error("E008")
        return jsonify(err), 400

    log_info("ZIP-Download gestartet", f"{len(ids)} Fotos angefragt | IDs: {ids}")
    try:
        conn       = get_db()
        cur        = conn.cursor()
        zip_buffer = io.BytesIO()
        found      = 0
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for photo_id in ids:
                cur.execute("SELECT filename FROM photos WHERE id = %s", (photo_id,))
                row = cur.fetchone()
                if not row:
                    log_warning(f"[E009] {ERROR_CODES['E009']}", f"id={photo_id} nicht in DB")
                    continue
                try:
                    blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
                    blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=row[0])
                    data = blob_client.download_blob().readall()
                    zf.writestr(row[0], data)
                    log.info(f"[INFO] Foto ins ZIP gepackt | id={photo_id} | {row[0]}")
                    found += 1
                except Exception as e:
                    log_error("E009", f"id={photo_id} | Datei: {row[0]}", e)
        conn.close()
        log_info("ZIP fertig", f"{found}/{len(ids)} Fotos verpackt | Grösse: {zip_buffer.tell()} Bytes")
        zip_buffer.seek(0)
        return send_file(zip_buffer, mimetype="application/zip",
                         download_name="fotos.zip", as_attachment=True)
    except Exception as e:
        err = log_error("E011", str(e), e)
        return jsonify(err), 500


# ── GET /api/photos/<id>/image ────────────────────────────────────────────────
@app.route("/api/photos/<int:photo_id>/image", methods=["GET"])
@requires_auth
@limiter.limit("500 per hour")
def serve_photo(photo_id):
    """
    Proxies das Bild sicher aus Azure Blob Storage.
    Da der Blob-Container privat ist, werden Bilder nur für
    eingeloggte Benutzer über diesen Endpunkt ausgeliefert.
    """
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT filename FROM photos WHERE id = %s", (photo_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Foto nicht gefunden"}), 404

        filename   = row[0]
        blob_svc   = BlobServiceClient.from_connection_string(CONN_STR)
        blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=filename)
        data       = blob_client.download_blob().readall()

        fn = filename.lower()
        if   fn.endswith(".png"):  mime = "image/png"
        elif fn.endswith(".gif"):  mime = "image/gif"
        elif fn.endswith(".webp"): mime = "image/webp"
        else:                      mime = "image/jpeg"

        log_info("Bild serviert", f"id={photo_id} | {filename}")
        response = send_file(io.BytesIO(data), mimetype=mime)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response
    except Exception as e:
        err = log_error("E009", f"serve_photo id={photo_id}", e)
        return jsonify(err), 404


# ── GET /health ───────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    # Kein Auth — wird von Azure App Service als Health-Check aufgerufen
    ip, client = get_client_info()
    log.info(f"[OK] Health-Check | {client}")
    return jsonify({"status": "ok"}), 200


# ── Start (lokal) ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("[OK] App gestartet | Lokaler Entwicklungsserver | Port 8000")
    app.run(host="0.0.0.0", port=8000, debug=False)