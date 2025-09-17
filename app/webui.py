import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

from .db import Database, get_db
from .main import job_queue
from .storage import Storage

router = APIRouter()
storage = Storage()


def check_auth(token: Optional[str]):
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
    <style>
      body {{ font-family: Arial, sans-serif; margin: 20px; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border: 1px solid #ddd; padding: 8px; }}
      th {{ background: #f3f3f3; }}
      a.button {{ padding: 4px 8px; border: 1px solid #888; border-radius: 4px; text-decoration: none; }}
      .ok {{ color: #0a0; }}
      .err {{ color: #a00; }}
      .muted {{ color: #666; }}
      .row {{ margin-bottom: 10px; }}
    </style>
  </head>
  <body>
    <h1>GreenAPI Image→PDF Relay</h1>
    {body}
  </body>
</html>
"""
    return HTMLResponse(html)


@router.get("/ui")
def ui(db: Database = Depends(get_db), token: Optional[str] = Query(default=None)):
    check_auth(token)
    # naive lists
    with db._conn() as con:
        cur = con.cursor()
        jobs = cur.execute(
            "SELECT id, sender, msg_id, status, created_at, pdf_path FROM jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()

    pdf_files = sorted((storage.base / "pdf").glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]

    rows = ""
    for j in jobs:
        jid, sender, msg_id, status, created_at, pdf_path = j
        link = f'<a class="button" href="/ui/resend/{jid}?token={token}">Resend</a>' if pdf_path else '<span class="muted">-</span>'
        open_pdf = f'<a class="button" href="/ui/file/pdf/{Path(pdf_path).name}?token={token}">Open</a>' if pdf_path else ""
        rows += f"<tr><td>{jid}</td><td>{sender}</td><td>{msg_id}</td><td>{status}</td><td>{created_at}</td><td>{open_pdf} {link}</td></tr>"

    pdf_list = "".join(
        f'<li><a href="/ui/file/pdf/{p.name}?token={token}">{p.name}</a></li>' for p in pdf_files
    )

    body = f"""
    <div class="row">
      <a class="button" href="/ui?token={token}">Refresh</a>
    </div>
    <h2>Recent Jobs</h2>
    <table>
      <tr><th>ID</th><th>Sender</th><th>Msg</th><th>Status</th><th>Created</th><th>Actions</th></tr>
      {rows}
    </table>
    <h2>PDF Files</h2>
    <ul>
      {pdf_list}
    </ul>
    """
    return html_page(body)


@router.get("/ui/file/pdf/{name}")
def get_pdf(name: str, token: Optional[str] = Query(default=None)):
    check_auth(token)
    p = storage.base / "pdf" / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="application/pdf", filename=name)


@router.get("/ui/resend/{job_id}")
async def resend(job_id: int, db: Database = Depends(get_db), token: Optional[str] = Query(default=None)):
    check_auth(token)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await job_queue.put(job_id)
    db.update_job_status(job_id, "PENDING")
    return JSONResponse({"ok": True, "job_id": job_id})