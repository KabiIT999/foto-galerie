from flask import Flask, jsonify, request, send_file
from azure.storage.blob import BlobServiceClient
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import psycopg2, os, zipfile, io, requests as req

app = Flask(__name__)

CONN_STR = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
CONTAINER = "photos"
DB_URL    = os.environ["DATABASE_URL"]
CDN_URL   = os.environ.get("CDN_URL", "")

def get_db():
    return psycopg2.connect(DB_URL)

def get_gps_from_exif(file):
    try:
        img = Image.open(file)
        exif = img._getexif()
        if not exif:
            return None, None
        for tag, value in exif.items():
            if TAGS.get(tag) == "GPSInfo":
                gps = {GPSTAGS.get(t): v for t, v in value.items()}
                lat = gps.get("GPSLatitude")
                lng = gps.get("GPSLongitude")
                if lat and lng:
                    lat_dec = lat[0] + lat[1]/60 + lat[2]/3600
                    lng_dec = lng[0] + lng[1]/60 + lng[2]/3600
                    return round(lat_dec, 6), round(lng_dec, 6)
    except Exception:
        pass
    return None, None

def reverse_geocode(lat, lng):
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json"
        r = req.get(url, headers={"User-Agent": "foto-galerie/1.0"}, timeout=5)
        data = r.json()
        addr = data.get("address", {})
        city    = addr.get("city") or addr.get("town") or addr.get("village", "")
        country = addr.get("country", "")
        return city, country
    except Exception:
        return "", ""

@app.route("/api/photos", methods=["GET"])
def list_photos():
    city    = request.args.get("city")
    country = request.args.get("country")
    conn = get_db()
    cur  = conn.cursor()
    query  = "SELECT id, filename, url, city, country, lat, lng FROM photos"
    params = []
    if city:
        query += " WHERE city = %s"
        params.append(city)
    elif country:
        query += " WHERE country = %s"
        params.append(country)
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return jsonify([{
        "id": r[0], "filename": r[1], "url": r[2],
        "city": r[3], "country": r[4], "lat": r[5], "lng": r[6]
    } for r in rows])

@app.route("/api/locations", methods=["GET"])
def get_locations():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT DISTINCT city, country FROM photos WHERE city IS NOT NULL AND city != '' ORDER BY country, city")
    rows = cur.fetchall()
    conn.close()
    return jsonify([{"city": r[0], "country": r[1]} for r in rows])

@app.route("/api/photos", methods=["POST"])
def upload_photo():
    file = request.files.get("photo")
    if not file:
        return jsonify({"error": "Kein Foto"}), 400
    lat, lng = get_gps_from_exif(file)
    file.seek(0)
    city, country = ("", "")
    if lat and lng:
        city, country = reverse_geocode(lat, lng)
    blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
    blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=file.filename)
    blob_client.upload_blob(file, overwrite=True)
    if CDN_URL:
        url = f"{CDN_URL}/{file.filename}"
    else:
        url = f"https://{blob_svc.account_name}.blob.core.windows.net/{CONTAINER}/{file.filename}"
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO photos (filename, url, lat, lng, city, country) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (file.filename, url, lat, lng, city, country)
    )
    photo_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({"id": photo_id, "url": url, "city": city, "country": country}), 201

@app.route("/api/download", methods=["POST"])
def download_photos():
    ids = request.json.get("ids", [])
    if not ids:
        return jsonify({"error": "Keine IDs"}), 400
    conn = get_db()
    cur  = conn.cursor()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for photo_id in ids:
            cur.execute("SELECT filename FROM photos WHERE id = %s", (photo_id,))
            row = cur.fetchone()
            if row:
                blob_svc    = BlobServiceClient.from_connection_string(CONN_STR)
                blob_client = blob_svc.get_blob_client(container=CONTAINER, blob=row[0])
                data = blob_client.download_blob().readall()
                zf.writestr(row[0], data)
    conn.close()
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype="application/zip",
                     download_name="fotos.zip", as_attachment=True)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)