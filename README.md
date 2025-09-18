Green-API Image→PDF Relay

Overview
A local service that:
- Receives Green-API webhooks for incoming messages/media
- Downloads media promptly
- Packs images into A4-aware PDFs using deterministic rules
- Uploads the PDF to Green-API storage and sends it to an admin chat via uploadFile + sendFileByUrl
- Provides a localhost WebUI to monitor jobs, files, and manually resend
- Persists jobs and metadata in SQLite
- Can be packaged into a single runnable binary (PyInstaller)

Architecture
- FastAPI server
  - POST /webhook — Green-API webhook receiver
  - GET /ui — lightweight WebUI (requires token if ADMIN_PASSWORD is set)
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
- Green-API instance credentials (idInstance, apiToken)
- Admin chatId (e.g., 1234567890@c.us)

Setup
1) Clone and install:
   pip install -r requirements.txt

2) Configure environment:
   cp .env.example .env
   # edit .env and fill in values

   The app reads env vars from the environment. If you use a .env loader, add python-dotenv or export manually:
   export GREEN_API_INSTANCE_ID=...
   export GREEN_API_API_TOKEN=...
   export ADMIN_CHAT_ID=...
   export GEMINI_API_KEY=...           # required for auto-replies and file QA
   export GEMINI_MODEL=gemma-3n-E4B-it   # optional, override default model

3) Run the server:
   python run.py

   - WebUI: http://127.0.0.1:8080/ui?token=YOUR_ADMIN_PASSWORD (if set)
   - Webhook endpoint: POST http://YOUR_PUBLIC_TUNNEL/webhook

4) Expose webhook (local testing):
   Use a tunneling tool (e.g., ngrok or cloudflared):
   ngrok http 8080
   Then set your Green-API webhook URL to: https://YOUR-NGROK/webhook

Environment variables
- GREEN_API_BASE_URL (default: https://api.green-api.com)
- GREEN_API_INSTANCE_ID (required)
- GREEN_API_API_TOKEN (required)
- ADMIN_CHAT_ID (required) — chatId for admin/private chat to receive PDFs
- ADMIN_PASSWORD (optional) — token to protect WebUI, pass as ?token=...
- HOST (default: 127.0.0.1)
- PORT (default: 8080)
- WORKERS (default: 2)
- GEMINI_API_KEY (required for LLM features)
- GEMINI_MODEL (optional; defaults to gemma-3n-E4B-it)

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
- uploadFile → returns urlFile
- sendFileByUrl to ADMIN_CHAT_ID with the returned URL
- This avoids re-uploading for re-sends

WebUI
- /ui: lists recent jobs and generated PDFs
- Open PDF and Resend actions (resend re-queues the job)
- Pass ?token=ADMIN_PASSWORD when configured

Local data retention
- Raw media and payloads are saved under storage/
- You can implement retention policies and purge from the WebUI in future iterations

Packaging with PyInstaller
- Create a single-file binary:
  pip install pyinstaller
  pyinstaller -F -n greenapi-relay run.py

  The binary will be at dist/greenapi-relay (or .exe on Windows).
  Run it with the same environment variables as above.

Notes and next steps
- Webhook validation: add signature/origin checks if provided by Green-API
- QR/status: expose a panel if using Green-API endpoints to fetch QR/status
- Grouping: current implementation processes images within a single webhook job; extend to time-window grouping across messages from the same sender if needed
- Circuit breaker and richer retry strategies can be added around upload/send depending on observed errors