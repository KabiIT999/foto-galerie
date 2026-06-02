# WICHTIG für M1: Platform explizit auf amd64 setzen (Azure läuft auf x86_64)
FROM --platform=linux/amd64 python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8000
CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:8000", "--timeout", "120", "app:app"]