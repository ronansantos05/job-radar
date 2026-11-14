"""Dashboard web (FastAPI), protegido por senha, + agendador da coleta."""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import markdown as md
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import config, db, llm, pipeline

config.load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobradar")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()
_password: str | None = None


def dashboard_password() -> str:
    """Sem DASH_PASSWORD o painel não fica aberto: gera uma senha temporária e avisa no log."""
    global _password
    if _password is None:
        _password = os.getenv("DASH_PASSWORD") or ""
        if not _password:
            _password = secrets.token_urlsafe(12)
            log.warning("DASH_PASSWORD não definida. Senha temporária desta execução: %s", _password)
    return _password


def require_login(cred: HTTPBasicCredentials = Depends(security)) -> None:
    user_ok = secrets.compare_digest(cred.username.encode(), os.getenv("DASH_USER", "admin").encode())
    pass_ok = secrets.compare_digest(cred.password.encode(), dashboard_password().encode())
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Login inválido", headers={"WWW-Authenticate": "Basic"})


async def _scheduler() -> None:
    await asyncio.sleep(3)
    while True:
        await asyncio.to_thread(pipeline.run_once)
        await asyncio.sleep(max(0.25, config.load_config().interval_hours) * 3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    dashboard_password()
    task = None
    if os.getenv("DISABLE_SCHEDULER") != "1":
        task = asyncio.create_task(_scheduler())
    yield
    if task:
        task.cancel()


app = FastAPI(title="job-radar", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
router = APIRouter(dependencies=[Depends(require_login)])


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # CSRF: formulários só valem se vierem do próprio painel.
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Origem não permitida", status_code=403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'")
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- helpers de template

def fmt_date(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(value).strftime("%d/%m/%Y")
    except ValueError:
        return value[:10]


def fmt_datetime(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(value).strftime("%d/%m %H:%M") + " UTC"
    except ValueError:
        return value[:16]


def render_markdown(text: str | None) -> str:
    """Markdown -> HTML seguro: o texto vem de um LLM que leu conteúdo da internet."""
    out = md.markdown(html.escape(text or "", quote=False))
    return re.sub(r'(href|src)="(?!https?://|mailto:)[^"]*"', r'\1="#"', out)


def safe_url(url: str | None) -> str:
    return url if url and url.lower().startswith(("http://", "https://")) else "#"


templates.env.filters["date"] = fmt_date
templates.env.filters["datetime"] = fmt_datetime
templates.env.filters["safe_url"] = safe_url


def page(request: Request, name: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx["running"] = pipeline.state["running"]
    ctx["has_profile"] = bool(config.read_profile())
    ctx["has_key"] = llm.configured()
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def job_or_404(jid: str) -> dict:
    job = db.get_job(jid)
    if not job:
        raise HTTPException(404, "Vaga não encontrada")
    return job


# ---------------------------------------------------------------- rotas

@app.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@router.get("/")
def index(request: Request, view: str = "compativeis", q: str = ""):
    cfg = config.load_config()
    if view not in db.VIEWS:
        view = "compativeis"
    return page(
        request, "index.html",
        jobs=db.list_jobs(view, cfg.min_score, q.strip()[:80]),
        counts=db.counts(cfg.min_score), views=db.VIEWS, view=view, q=q,
        min_score=cfg.min_score, events=db.recent_events(), last_run=db.last_run(),
        model=llm.model(),
    )


@router.post("/atualizar")
def refresh():
    started = pipeline.run_in_background()
    return back("/", "Coleta iniciada. Atualize a página em alguns minutos." if started
                else "Já existe uma coleta em andamento.")


@router.post("/adicionar")
def add_job(title: str = Form(...), company: str = Form(""), url: str = Form(""),
            description: str = Form(...)):
    title, description = title.strip()[:200], description.strip()[:12000]
    if not title or not description:
        return back("/", "Informe pelo menos o título e a descrição da vaga.")
    url = url.strip()[:500]
    if safe_url(url) == "#":
        url = ""
    ext_id = url or hashlib.sha1((title + company + description[:200]).encode()).hexdigest()
    jid = db.insert_job({
        "source": "manual", "ext_id": ext_id, "title": title, "company": company.strip()[:120],
        "url": url, "location": "", "tags": [], "description": description, "salary": "",
        "posted_at": db.now(),
    }, title_hit=1)
    if not jid:
        return back("/", "Essa vaga já estava cadastrada.")
    pipeline.score_one_in_background(jid)
    return back(f"/vaga/{jid}", "Vaga adicionada. A análise aparece em alguns segundos (atualize a página).")


@router.get("/vaga/{jid}")
def job_page(request: Request, jid: str):
    return page(request, "job.html", job=job_or_404(jid), statuses=db.STATUSES,
                min_score=config.load_config().min_score,
                events=db.recent_events(30, jid))


@router.post("/vaga/{jid}/status")
def job_status(jid: str, status: str = Form(...)):
    job_or_404(jid)
    if status not in db.STATUSES:
        raise HTTPException(400, "Status inválido")
    db.set_status(jid, status)
    return back(f"/vaga/{jid}", f"Status alterado para: {db.STATUSES[status]}.")


@router.post("/vaga/{jid}/notas")
def job_notes(jid: str, notes: str = Form("")):
    job_or_404(jid)
    db.set_notes(jid, notes[:5000])
    return back(f"/vaga/{jid}", "Anotações salvas.")


@router.post("/vaga/{jid}/gerar")
def job_generate(jid: str):
    job_or_404(jid)
    try:
        pipeline.tailor(jid)
    except llm.LLMError as e:
        return back(f"/vaga/{jid}", f"Não consegui gerar o currículo: {e}")
    return back(f"/vaga/{jid}", "Currículo e carta gerados. Revise antes de enviar.")


@router.post("/vaga/{jid}/documentos")
def job_docs(jid: str, cv_md: str = Form(""), cover_md: str = Form("")):
    job_or_404(jid)
    db.save_docs(jid, cv_md[:20000], cover_md[:8000])
    return back(f"/vaga/{jid}", "Documentos salvos.")


@router.get("/vaga/{jid}/imprimir")
def job_print(request: Request, jid: str, doc: str = "cv"):
    job = job_or_404(jid)
    text = job["cover_md"] if doc == "carta" else job["cv_md"]
    if not text:
        raise HTTPException(404, "Documento ainda não gerado")
    return templates.TemplateResponse(request, "print.html", {"job": job, "body": render_markdown(text)})


@router.get("/perfil")
def profile_page(request: Request):
    example = Path(config.ROOT / "profile.example.md")
    return page(request, "profile.html", profile=config.read_profile(),
                example=example.read_text(encoding="utf-8") if example.exists() else "",
                preferences=config.load_config().preferences)


@router.post("/perfil")
def profile_save(profile: str = Form("")):
    config.write_profile(profile[:30000])
    return back("/perfil", "Perfil salvo. Ele será usado nas próximas análises e currículos.")


app.include_router(router)
