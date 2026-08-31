"""Local control panel. Runs on your machine, reads the same SQLite file the
scheduled job writes.

Start it with:  uvicorn web.app:app --reload --port 8000

Deliberately not deployed anywhere public: it has no auth, and the keys it
reports on live in your local .env. If you ever host it, put it behind auth
first.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, pipeline  # noqa: E402
from src.config import BANKS_CSV, settings  # noqa: E402
from src.keypool import KeyPool  # noqa: E402

HERE = Path(__file__).resolve().parent
app = FastAPI(title="Outreach")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

# Only ever holds the result of the last triggered run, for the flash message.
LAST_RUN: dict = {}


@app.on_event("startup")
def _startup():
    db.init()


def render(request: Request, template: str, **ctx):
    ctx.setdefault("nav", template.replace(".html", ""))
    ctx.setdefault("last_run", LAST_RUN)
    ctx.setdefault("cfg", settings)
    return templates.TemplateResponse(request, template, ctx)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db.session() as conn:
        counts = db.counts(conn)
        runs = [dict(r) for r in db.recent_runs(conn, 8)]
    for r in runs:
        r["stats"] = json.loads(r["stats"]) if r["stats"] else {}
    return render(request, "dashboard.html", counts=counts, runs=runs,
                  gaps=settings.missing())


@app.get("/drafts", response_class=HTMLResponse)
def drafts(request: Request, status: str = "pending"):
    with db.session() as conn:
        rows = [dict(r) for r in db.drafts_with_contacts(conn, status)]
    for r in rows:
        try:
            r["reasons"] = json.loads(r["affinity_notes"] or "[]")
        except json.JSONDecodeError:
            r["reasons"] = []
    return render(request, "drafts.html", drafts=rows, status=status)


@app.post("/drafts/{draft_id}/{action}")
def act_on_draft(draft_id: int, action: str,
                 subject: str = Form(None), body: str = Form(None)):
    mapping = {"approve": "approved", "reject": "rejected", "save": None}
    if action not in mapping:
        return RedirectResponse("/drafts", status_code=303)
    with db.session() as conn:
        if subject is not None and body is not None:
            conn.execute("UPDATE drafts SET subject=?, body=? WHERE id=?",
                         (subject, body, draft_id))
        if mapping[action]:
            conn.execute("UPDATE drafts SET status=?, reviewed_at=? WHERE id=?",
                         (mapping[action], db.now(), draft_id))
        if action == "reject":
            conn.execute(
                """UPDATE contacts SET status='skipped' WHERE id =
                   (SELECT contact_id FROM drafts WHERE id=?)""", (draft_id,))
    return RedirectResponse("/drafts", status_code=303)


@app.get("/contacts", response_class=HTMLResponse)
def contacts(request: Request, q: str = "", status: str = ""):
    sql = """SELECT c.*, b.name AS bank_name, b.category, b.priority
             FROM contacts c JOIN banks b ON b.id = c.bank_id WHERE 1=1"""
    params: list = []
    if q:
        sql += """ AND (c.first_name LIKE ? OR c.last_name LIKE ?
                        OR c.title LIKE ? OR b.name LIKE ?)"""
        params += [f"%{q}%"] * 4
    if status:
        sql += " AND c.status = ?"
        params.append(status)
    sql += " ORDER BY c.affinity_score DESC, b.priority ASC LIMIT 300"
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        try:
            r["reasons"] = json.loads(r["affinity_notes"] or "[]")
        except json.JSONDecodeError:
            r["reasons"] = []
    return render(request, "contacts.html", contacts=rows, q=q, status=status)


@app.get("/banks", response_class=HTMLResponse)
def banks(request: Request):
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT b.*, COUNT(c.id) AS contact_count
               FROM banks b LEFT JOIN contacts c ON c.bank_id = b.id
               GROUP BY b.id ORDER BY b.priority ASC, b.name ASC"""
        ).fetchall()]
    return render(request, "banks.html", banks=rows)


@app.post("/banks/upload")
async def upload_banks(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8-sig")
    BANKS_CSV.parent.mkdir(parents=True, exist_ok=True)
    BANKS_CSV.write_text(raw)
    n = pipeline.load_banks()
    LAST_RUN.clear()
    LAST_RUN.update({"stage": "load-banks", "result": {"banks": n}})
    return RedirectResponse("/banks", status_code=303)


@app.post("/banks/{bank_id}/toggle")
def toggle_bank(bank_id: int):
    with db.session() as conn:
        conn.execute("UPDATE banks SET active = 1 - active WHERE id = ?", (bank_id,))
    return RedirectResponse("/banks", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    apollo = KeyPool("apollo", settings.apollo_keys).snapshot()
    model = KeyPool(settings.llm.provider, settings.llm.keys).snapshot()
    return render(request, "settings.html", apollo_keys=apollo,
                  model_keys=model, cfg=settings, gaps=settings.missing())


@app.post("/run/{stage}")
async def trigger(stage: str, background: BackgroundTasks):
    stages = {
        "refresh": pipeline.refresh_targets,
        "enrich": pipeline.enrich_batch,
        "draft": pipeline.draft_batch,
        "push": pipeline.push_to_gmail,
        "daily": pipeline.daily,
    }
    if stage not in stages:
        return RedirectResponse("/", status_code=303)

    def work():
        try:
            result = stages[stage]()
        except Exception as e:
            result = {"error": str(e)}
        LAST_RUN.clear()
        LAST_RUN.update({"stage": stage, "result": result})

    background.add_task(work)
    LAST_RUN.clear()
    LAST_RUN.update({"stage": stage, "result": None})
    return RedirectResponse("/", status_code=303)
