# David Lloyd Login Backend

Python backend die de mobiele login-flow volgt en als API beschikbaar maakt.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item config\config.example.json config\config.json
```

Vul daarna `config/config.json` met je eigen `username` en `password`.

## Starten

```powershell
.\.venv\Scripts\uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API:

- `GET /` frontend voor configbeheer
- `GET /health`
- `GET /api/config`
- `PUT /api/config`
- `GET /auth/status`
- `POST /auth/login`
- `POST /hmac/refresh`
- `POST /auth/refresh-token`
- `GET /members/me/membership-status`
- `GET /padel/config`
- `GET /padel/slots`
- `GET /padel/availability/{date}`
- `POST /padel/book-generated`
- `POST /padel/book-slot`

De backend bewaart runtime state in `state/session.json`. Zet dit bestand niet in git.

## Padel boeken

Na `POST /auth/login`:

```powershell
Invoke-RestMethod http://127.0.0.1:8017/padel/slots
Invoke-RestMethod http://127.0.0.1:8017/padel/availability/2026-05-23
Invoke-RestMethod -Method Post http://127.0.0.1:8017/padel/book-generated -ContentType "application/json" -Body '{"attempts":10}'
```

Handmatig een slot boeken:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8017/padel/book-slot -ContentType "application/json" -Body '{"date":"2026-05-23","time":"07:00"}'
```

## Belangrijk

De David Lloyd backend gebruikt een HMAC header met een app-specifieke canonicalisatie. De implementatie is bewust geïsoleerd in `app/signing.py`. Als een gesigneerde request `401` of `400` teruggeeft terwijl HMAC ophalen lukt, moeten we alleen `signature_mode` of de canonicalisatie aanpassen.
