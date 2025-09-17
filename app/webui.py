import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse

from .db import Database, get_db
from .tasks import job_queue
from .storage import Storage

router = APIRouter()
storage = Storage()


def check_auth(token: Optional[str], db: Optional[Database] = None):
    expected = None
    if db:
        expected = db.get_setting("ADMIN_PASSWORD", None)
    if not expected:
        expected = os.getenv("ADMIN_PASSWORD")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def html_page(body: str) -> HTMLResponse:
    html = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <title>Relay WebUI</title>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>
      :root {{
        --bg: #0f1120;
        --card: #16182a;
        --muted: #8a8fa6;
        --text: #e7e9f5;
        --accent: #7c4dff;
        --accent2: #00d4ff;
        --ok: #3ddc97;
        --err: #ff6b6b;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
        color: var(--text);
        background:
          radial-gradient(1200px 600px at 10% -10%, rgba(124,77,255,0.18), transparent 70%),
          radial-gradient(1000px 500px at 110% 10%, rgba(0,212,255,0.15), transparent 60%),
          linear-gradient(180deg, #0b0d1a, #0f1120);
        min-height: 100vh;
      }}
      header {{
        padding: 24px 20px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px solid rgba(255,255,255,0.06);
        background: linear-gradient(180deg, rgba(255,255,255,0.02), transparent);
      }}
      .brand {{
        font-weight: 700;
        letter-spacing: 0.3px;
        font-size: 18px;
      }}
      .container {{ padding: 24px; max-width: 1200px; margin: 0 auto; }}
      .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }}
      .card {{
        background: var(--card);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 14px;
        padding: 16px;
        box-shadow: 0 10px 25px rgba(0,0,0,0.25);
      }}
      .card h3 {{ margin: 0 0 10px; font-size: 16px; color: #fff; }}
      .muted {{ color: var(--muted); }}
      .button {{
        display: inline-block; padding: 8px 12px; border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.12); color: #fff; text-decoration: none;
        background: linear-gradient(135deg, rgba(124,77,255,0.25), rgba(0,212,255,0.18));
        transition: transform .08s ease, filter .15s ease, opacity .2s ease;
      }}
      .button:hover {{ filter: brightness(1.06); transform: translateY(-1px); }}
      .button.danger {{ background: linear-gradient(135deg, rgba(255,107,107,0.25), rgba(255,0,102,0.18)); }}
      .badge {{ padding: 4px 8px; border-radius: 999px; font-size: 12px; border: 1px solid rgba(255,255,255,0.2); }}
      .ok {{ color: var(--ok); }}
      .err {{ color: var(--err); }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid rgba(255,255,255,0.08); padding: 10px; font-size: 14px; }}
      th {{ text-align: left; color: #cfd3e4; }}
      .row {{ margin-bottom: 10px; }}
      input[type="text"], input[type="password"], textarea {{
        width: 100%; padding: 10px; border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.12); background: #0e1020; color: #fff;
      }}
      textarea {{ min-height: 100px; resize: vertical; }}
      form .actions {{ margin-top: 10px; display: flex; gap: 10px; }}
      .hint {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
      .grid-2 {{ display:grid; gap:12px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
      label {{ display:block; margin: 8px 0 6px; font-size: 13px; color:#cfd3e4; }}
    </style>
  </head>
  <body>
    <header>
      <div class="brand">GreenAPI Image→PDF Relay</div>
      <div>
        <a class="button" href="https://green-api.com" target="_blank" rel="noreferrer">Green-API</a>
        <a class="button" href="/ui?token={os.getenv("ADMIN_PASSWORD","")}" title="Refresh">Refresh</a>
      </div>
    </header>
    <div class="container">
      {body}
    </div>
  </body>
</html>
"""
    return HTMLResponse(html)


@router.get("/ui")
def ui(db: Database = Depends(get_db), token: Optional[str] = Query(default=None)):
    check_auth(token, db)
    # lists
    with db._conn() as con:
        cur = con.cursor()
        jobs = cur.execute(
            "SELECT id, sender, msg_id, status, created_at, pdf_path FROM jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()

    pdf_files = sorted((storage.base / "pdf").glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]

    rows = ""
    for j in jobs:
        jid, sender, msg_id, status, created_at, pdf_path = j
        link = f'<a class="button" href="/ui/resend/{jid}?token={token}">Resend</a>' if pdf_path else '<span class="muted">-</span>'
        open_pdf = f'<a class="button" href="/ui/file/pdf/{Path(pdf_path).name}?token={token}">Open</a>' if pdf_path else ""
        rows += f"<tr><td>{jid}</td><td>{sender}</td><td>{msg_id}</td><td><span class='badge'>{status}</span></td><td>{created_at}</td><td>{open_pdf} {link}</td></tr>"

    pdf_list = "".join(
        f'<li><a class="button" href="/ui/file/pdf/{p.name}?token={token}">{p.name}</a></li>' for p in pdf_files
    )

    # settings snapshot
    auto_enabled = (db.get_setting("auto_reply_enabled", "0") or "0") == "1"
    sys_prompt = db.get_setting("auto_reply_system_prompt", "") or ""

    # current settings display helpers
    def status_badge(val: Optional[str]) -> str:
        return "<span class='badge ok'>Set</span>" if (val and len(val) > 0) else "<span class='badge'>Not set</span>"

    gemini_set = status_badge(db.get_setting("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY")))
    green_base = db.get_setting("GREEN_API_BASE_URL", os.getenv("GREEN_API_BASE_URL", "https://api.green-api.com")) or ""
    green_id = db.get_setting("GREEN_API_INSTANCE_ID", os.getenv("GREEN_API_INSTANCE_ID", "")) or ""
    green_token_set = status_badge(db.get_setting("GREEN_API_API_TOKEN", os.getenv("GREEN_API_API_TOKEN")))
    admin_chat_id = db.get_setting("ADMIN_CHAT_ID", os.getenv("ADMIN_CHAT_ID", "")) or ""
    workers_val = db.get_setting("WORKERS", os.getenv("WORKERS", "2")) or "2"
    admin_pw_set = status_badge(db.get_setting("ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD")))

    settings_html = f"""
    <div class="card">
      <h3>Settings</h3>
      <form action="/ui/settings?token={token}" method="post">
        <label for="system_prompt">AI System Prompt</label>
        <textarea id="system_prompt" name="system_prompt" placeholder="You are a concise helpful WhatsApp assistant.">{sys_prompt}</textarea>
        <div class="hint">Assistant behavior for auto replies.</div>

        <div class="grid-2">
          <div>
            <label for="GEMINI_API_KEY">Gemini API Key {gemini_set}</label>
            <input type="password" id="GEMINI_API_KEY" name="GEMINI_API_KEY" placeholder="Paste Gemini API key"/>
            <div class="hint">Leave blank to keep current.</div>
          </div>

          <div>
            <label for="ADMIN_PASSWORD">Admin Password {admin_pw_set}</label>
            <input type="password" id="ADMIN_PASSWORD" name="ADMIN_PASSWORD" placeholder="Set UI password"/>
            <div class="hint">Used for accessing this page (?token=...). Leave blank to keep current.</div>
          </div>

          <div>
            <label for="GREEN_API_BASE_URL">Green API Base URL</label>
            <input type="text" id="GREEN_API_BASE_URL" name="GREEN_API_BASE_URL" value="{green_base}" placeholder="https://api.green-api.com"/>
          </div>

          <div>
            <label for="GREEN_API_INSTANCE_ID">Green API Instance ID</label>
            <input type="text" id="GREEN_API_INSTANCE_ID" name="GREEN_API_INSTANCE_ID" value="{green_id}" placeholder="e.g., 110100"/>
          </div>

          <div>
            <label for="GREEN_API_API_TOKEN">Green API Token {green_token_set}</label>
            <input type="password" id="GREEN_API_API_TOKEN" name="GREEN_API_API_TOKEN" placeholder="Paste Green-API token"/>
            <div class="hint">Leave blank to keep current.</div>
          </div>

          <div>
            <label for="ADMIN_CHAT_ID">Admin Chat ID</label>
            <input type="text" id="ADMIN_CHAT_ID" name="ADMIN_CHAT_ID" value="{admin_chat_id}" placeholder="1234567890@c.us"/>
          </div>

          <div>
            <label for="WORKERS">Workers</label>
            <input type="text" id="WORKERS" name="WORKERS" value="{workers_val}" placeholder="2"/>
          </div>
        </div>

        <div class="row" style="margin-top:10px;">
          Status:
          {"<span class='badge ok'>Auto Reply Enabled</span>" if auto_enabled else "<span class='badge'>Auto Reply Disabled</span>"}
          <a class="button" href="/ui/auto-reply/toggle?token={token}">{'Disable' if auto_enabled else 'Enable'}</a>
        </div>

        <div class="actions">
          <button class="button" type="submit">Save Settings</button>
        </div>
      </form>
    </div>
    """

    body = f"""
    <div class="grid">
      {settings_html}
      <div class="card">
        <h3>Recent Jobs</h3>
        <table>
          <tr><th>ID</th><th>Sender</th><th>Msg</th><th>Status</th><th>Created</th><th>Actions</th></tr>
          {rows}
        </table>
      </div>
      <div class="card">
        <h3>Recent PDFs</h3>
        <div class="row">
          {pdf_list if pdf_list else '<span class="muted">No PDFs yet</span>'}
        </div>
      </div>
    </div>
    """
    return html_page(body)


@router.get("/ui/file/pdf/{name}")
def get_pdf(name: str, db: Database = Depends(get_db), token: Optional[str] = Query(default=None)):
    check_auth(token, db)
    p = storage.base / "pdf" / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="application/pdf", filename=name)


@router.get("/ui/resend/{job_id}")
async def resend(job_id: int, db: Database = Depends(get_db), token: Optional[str] = Query(default=None)):
    check_auth(token, db)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await job_queue.put(job_id)
    db.update_job_status(job_id, "PENDING")
    return JSONResponse({"ok": True, "job_id": job_id})


@router.get("/ui/auto-reply/toggle")
def toggle_auto_reply(db: Database = Depends(get_db), token: Optional[str] = Query(default=None)):
    check_auth(token, db)
    cur = db.get_setting("auto_reply_enabled", "0") or "0"
    new_val = "0" if cur == "1" else "1"
    db.set_setting("auto_reply_enabled", new_val)
    return RedirectResponse(url=f"/ui?token={token}", status_code=302)


@router.post("/ui/settings")
async def save_settings(request: Request, db: Database = Depends(get_db), token: Optional[str] = Query(default=None)):
    check_auth(token, db)
    form = await request.form()

    # Text settings
    sp = (form.get("system_prompt") or "").strip()
    if sp != "":
        db.set_setting("auto_reply_system_prompt", sp)

    # Optional secrets/texts: only set if provided (non-empty) to avoid erasing existing
    for key in [
        "GEMINI_API_KEY",
        "GREEN_API_API_TOKEN",
        "ADMIN_PASSWORD",
    ]:
        val = (form.get(key) or "").strip()
        if val:
            db.set_setting(key, val)

    # Non-secret values (allow update even empty to explicit clear? keep current behavior: update if provided)
    for key in [
        "GREEN_API_BASE_URL",
        "GREEN_API_INSTANCE_ID",
        "ADMIN_CHAT_ID",
        "WORKERS",
    ]:
        val = form.get(key)
        if val is not None:
            db.set_setting(key, (val or "").strip())

    return RedirectResponse(url=f"/ui?token={token}", status_code=302)