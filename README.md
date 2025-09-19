Green-API Image→PDF Relay

Overview
A local service that:
- Receives Green-API webhooks for incoming messages/media
- Downloads media promptly
- Packs images into A4-aware PDFs using deterministic rules
- Uploads the PDF and sends it back
- Provides a localhost WebUI to monitor jobs, files, and manually resend
- Persists jobs and metadata in SQLite
- Can run with either Green-API or a local WhatsApp Web gateway (no external API)

Architecture
- FastAPI server (Python)
  - POST /webhook — Green-API-compatible webhook receiver
  - GET /ui — lightweight WebUI (requires token if ADMIN_PASSWORD is set)
- Optional Node-based WhatsApp Web gateway (whatsapp-web.js)
  - Mimics Green-API endpoints so the Python client works unchanged
  - Exposes QR code for session pairing and sends/receives messages locally
- SQLite persistence in storage/app.db
- storage/ folder layout:
  storage/
    incoming_payloads/   # raw webhook JSON
    raw/{sender}/{YYYYMMDD}_{msgid}/  # downloaded images
    pdf/                 # generated PDFs
    pdf_meta/            # JSON metadata per PDF
    quarantine/          # failed jobs
- Background worker(s) process jobs from a queue

Prerequisites
- Python 3.10+
- For local WhatsApp mode (no external API): Node.js 18+ and Chrome/Chromium dependencies for puppeteer
- Admin chatId (e.g., 1234567890@c.us) for where PDFs are sent (or reply to sender)

Setup (Python app)
1) Install:
   pip install -r requirements.txt

2) Run:
   python run.py

   - WebUI: http://127.0.0.1:8080/ui?token=YOUR_ADMIN_PASSWORD (if set)
   - Webhook endpoint: POST http://YOUR_PUBLIC_TUNNEL/webhook

3) Optional: expose webhook for remote testing
   ngrok http 8080
   Then set webhook URL to: https://YOUR-NGROK/webhook

Environment variables (Python)
- GREEN_API_BASE_URL (default: https://api.green-api.com)
  - To use the local WhatsApp gateway, set GREEN_API_BASE_URL=http://127.0.0.1:3000
- GREEN_API_INSTANCE_ID (required by Green-API, ignored by local gateway)
- GREEN_API_API_TOKEN (required by Green-API, ignored by local gateway)
- ADMIN_CHAT_ID (optional) — chatId to receive PDFs; if not set, replies go to sender where possible
- ADMIN_PASSWORD (optional) — token to protect WebUI, pass as ?token=...
- HOST (default: 0.0.0.0)
- PORT (default: 8080)
- WORKERS (default: 2)
- GEMINI_API_KEY (required for LLM features)
- GEMINI_MODEL (optional; defaults to gemini-1.5-flash)

Deterministic A4 PDF rules
- A4 @ 300 DPI → 2480 × 3508 px
- Margin: min(15 mm, 3% page width)
- Classification:
  - Full A4: ≥ 95% in both axes → 1 per page
  - Half A4: ≥ 45% in both axes → 2 per page (portrait: stacked; landscape: side-by-side)
  - Quarter A4: ≥ 22% in both axes → up to 4 per page (2×2)
  - Small/mixed: greedy packing, largest first
- Preserve aspect ratio; never upscale past 100%
- Metadata saved in storage/pdf_meta/<name>.json

Recommended send flow
- Direct upload and send when possible
- Fallback: uploadFile → returns urlFile
- Then sendFileByUrl to the destination with the returned URL

WebUI
- /ui: lists recent jobs and generated PDFs
- Open PDF and Resend actions (resend re-queues the job)
- Pass ?token=ADMIN_PASSWORD when configured
- If GREEN_API_BASE_URL is non-Green (e.g., localhost:3000), a WhatsApp Session panel appears showing the QR (served by the local gateway)

Local WhatsApp Web Gateway (no external API)
- Based on whatsapp-web.js. Project located in whatsapp_gateway/
- Mimics the subset of Green-API endpoints used by this app:
  - GET  /qr                              → PNG QR code (204 if not needed)
  - GET  /status                          → session status
  - POST /waInstance:id/sendMessage/:token
  - POST /waInstance:id/sendFileByUrl/:token
  - POST /waInstance:id/SendFileByUpload/:token (multipart/form-data)
  - POST /waInstance:id/uploadFile/:token (multipart → returns {urlFile})
  - GET  /waInstance:id/ReceiveNotification/:token (poll)
  - DELETE /waInstance:id/DeleteNotification/:token/:receiptId
  - POST   /waInstance:id/DeleteNotification/:token {receiptId}
- Files uploaded to the gateway are served under /files/...

Run the gateway:
1) Install Node dependencies:
   cd whatsapp_gateway
   npm install

2) Start gateway:
   npm start
   # Gateway listens on http://127.0.0.1:3000 by default (GATEWAY_PORT env overrides)
   # First run will show a QR at http://127.0.0.1:3000/qr

3) Configure Python app to use gateway:
   export GREEN_API_BASE_URL=http://127.0.0.1:3000
   # INSTANCE ID and TOKEN values are ignored by the gateway but must be non-empty for the client:
   export GREEN_API_INSTANCE_ID=local
   export GREEN_API_API_TOKEN=local

4) Start Python app and open WebUI:
   python run.py
   # Visit http://127.0.0.1:8080/ui
   # A “WhatsApp Session” panel with the QR should be visible (refresh if scanned recently)

Notes and next steps
- The gateway runs headless Chrome via puppeteer (inside whatsapp-web.js). Ensure your environment supports it.
- This is intended for local development and self-hosting; respect WhatsApp terms of service.
- Green-API mode remains supported; just set GREEN_API_BASE_URL back to https://api.green-api.com.