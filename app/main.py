"""Dashboard web (FastAPI) com contas de usuário + agendador da coleta."""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import markdown as md
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import attach, auth, config, db, llm, pdf, pipeline

config.load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobradar")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class NeedsLogin(Exception):
    pass


def current_user(request: Request) -> dict:
    user = db.session_user(request.cookies.get(auth.SESSION_COOKIE))
    if not user:
        raise NeedsLogin()
    return user


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    if db.count_users() == 0 and not auth.invite_code():
        log.warning("Nenhuma conta criada e SIGNUP_CODE não definido: a primeira pessoa que abrir "
                    "/cadastro vira administradora. Crie a sua conta agora.")
    stop = threading.Event()
    if os.getenv("DISABLE_SCHEDULER") != "1":
        threading.Thread(target=pipeline.scanner_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()
    pipeline.wake()


app = FastAPI(title="job-radar", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(NeedsLogin)
async def needs_login(_request: Request, _exc: NeedsLogin):
    return RedirectResponse("/entrar", status_code=303)


def same_origin(request: Request) -> bool:
    """O POST veio de uma página do próprio painel?"""
    # Navegadores atuais informam isso direto (em HTTPS e localhost); uma página não consegue forjar.
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # cliente que não é navegador (curl, testes): não há CSRF sem navegador
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")} - {None}
    return urlparse(origin).netloc in hosts


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # CSRF: formulários só valem se vierem do próprio painel.
    if request.method == "POST" and not same_origin(request):
        return PlainTextResponse("Origem não permitida", status_code=403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # "same-origin" e não "no-referrer": com "no-referrer" o navegador manda "Origin: null" até nos
    # formulários do próprio painel, e a checagem de CSRF acima não teria como reconhecê-los.
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; script-src 'self'; "
        "form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'")
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- helpers

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
templates.env.filters["markdown"] = lambda text: Markup(render_markdown(text))


def page(request: Request, name: str, user: dict | None = None, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx["user"] = user
    ctx["scanner_on"] = bool(user and user.get("scanner", 1))
    ctx["has_profile"] = bool(user and (user.get("profile") or "").strip())
    ctx["has_key"] = llm.configured()
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


PHOTO_MAX = 2 * 1024 * 1024
PHOTO_TYPES = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def photo_mime(data: bytes) -> str | None:
    """Tipo pela assinatura do arquivo, nunca pelo nome ou pelo tipo que o navegador informa."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((mime for magic, mime in PHOTO_TYPES if data.startswith(magic)), None)


def print_page(request: Request, user: dict, title: str, text: str, with_photo, toggle_url: str = "",
               pdf_url: str = ""):
    """Pré-visualização. `toggle_url` vazio = documento sem opção de foto (a carta)."""
    has_photo = db.get_extras(user["id"])["has_photo"]
    photo = bool(with_photo) and has_photo
    if photo:
        pdf_url += ("&" if "?" in pdf_url else "?") + "foto=1"
    return templates.TemplateResponse(request, "print.html", {
        "title": title, "body": render_markdown(text), "photo": photo,
        "has_photo": has_photo, "toggle_url": toggle_url, "pdf_url": pdf_url})


def pdf_download(user: dict, text: str, with_photo, *name_parts: str) -> Response:
    """PDF feito no servidor: sai limpo, sem data, endereço e número de página do navegador."""
    photo = db.get_photo(user["id"]) if with_photo else None
    data = pdf.render(text, " - ".join(p for p in name_parts if p), photo[0] if photo else None)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{pdf.filename(*name_parts)}"',
        "Cache-Control": "no-store"})


def job_or_404(uid: int, jid: str) -> dict:
    job = db.get_job(uid, jid)  # só encontra vagas da lista deste usuário
    if not job:
        raise HTTPException(404, "Vaga não encontrada")
    return job


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def start_session(request: Request, uid: int, url: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(auth.SESSION_COOKIE, db.create_session(uid), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
    return response


def signup_allowed() -> bool:
    return bool(auth.invite_code()) or db.count_users() == 0


# ---------------------------------------------------------------- páginas públicas

@app.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@app.get("/entrar")
def login_page(request: Request):
    if db.session_user(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", signup_allowed=signup_allowed())


@app.post("/entrar")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()[:254]
    keys = (f"ip:{client_ip(request)}", f"mail:{email}")
    if any(auth.blocked(k) for k in keys):
        return back("/entrar", "Muitas tentativas. Espere 10 minutos e tente de novo.")
    user = db.get_user_by_email(email)
    # compara mesmo sem usuário, para o tempo de resposta não revelar se o e-mail existe
    ok = auth.verify_password(password, user["password_hash"] if user else auth.hash_password("x"))
    if not (user and ok):
        for k in keys:
            auth.record_failure(k)
        return back("/entrar", "E-mail ou senha incorretos.")
    for k in keys:
        auth.clear_failures(k)
    return start_session(request, user["id"], "/" if (user.get("profile") or "").strip() else "/perfil")


@app.get("/cadastro")
def signup_page(request: Request):
    return page(request, "signup.html", allowed=signup_allowed(), needs_code=bool(auth.invite_code()))


@app.post("/cadastro")
def signup(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
           code: str = Form("")):
    if not signup_allowed():
        return back("/cadastro", "O cadastro está fechado. Peça um convite a quem administra este painel.")
    key = f"signup:{client_ip(request)}"
    if auth.blocked(key):
        return back("/cadastro", "Muitas tentativas. Espere 10 minutos e tente de novo.")
    expected = auth.invite_code()
    if expected and not secrets.compare_digest(code.strip().encode(), expected.encode()):
        auth.record_failure(key)
        return back("/cadastro", "Código de convite incorreto.")
    name, email = " ".join(name.split())[:80], email.strip().lower()[:254]
    if not name or not auth.valid_email(email):
        return back("/cadastro", "Informe seu nome e um e-mail válido.")
    if len(password) < auth.MIN_PASSWORD or len(password) > 200:
        return back("/cadastro", f"A senha precisa ter pelo menos {auth.MIN_PASSWORD} caracteres.")
    # quem atualiza da versão de um usuário só: o perfil antigo vai para a primeira conta
    legacy = config.read_profile() if db.count_users() == 0 else ""
    uid = db.create_user(email, name, auth.hash_password(password), config.load_config(), profile=legacy)
    if uid is None:
        return back("/cadastro", "Já existe uma conta com esse e-mail. Tente entrar.")
    log.info("nova conta criada (id %s)", uid)
    return start_session(request, uid, "/perfil?msg=" + quote("Conta criada. Agora descreva o seu perfil."))


@app.post("/sair")
def logout(request: Request):
    db.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse("/entrar", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- páginas do usuário

@app.get("/")
def index(request: Request, view: str = "compativeis", q: str = "", user: dict = Depends(current_user)):
    if view not in db.VIEWS:
        view = "compativeis"
    min_score = user["min_score"]
    return page(
        request, "index.html", user,
        jobs=db.list_jobs(user["id"], view, min_score, q.strip()[:80]),
        counts=db.counts(user["id"], min_score), views=db.VIEWS, view=view, q=q,
        min_score=min_score, events=db.recent_events(user["id"]), last_run=db.last_run(),
        running=pipeline.state["running"], tailoring=pipeline.tailoring.get(user["id"], 0), scan_error=pipeline.state["error"].partition("|"),
        fetch_minutes=round(config.load_config().fetch_minutes),
        model=llm.model(),
    )


@app.post("/gerar-todas")
def generate_all(user: dict = Depends(current_user)):
    if not (user.get("profile") or "").strip():
        return back("/", "Preencha o perfil antes de gerar currículos.")
    if pipeline.tailoring.get(user["id"]):
        return back("/", "Já existe uma geração em andamento. Recarregue a página para acompanhar.")
    n = pipeline.tailor_all_in_background(user["id"], user["min_score"])
    if not n:
        return back("/", "Nenhuma vaga compatível sem currículo no momento.")
    return back("/", f"Gerando currículo e carta para {n} vaga(s). Elas aparecem em “Currículo pronto” "
                     "conforme ficam prontas; revise cada uma antes de enviar.")


@app.post("/scanner")
def scanner(on: int = Form(...), user: dict = Depends(current_user)):
    db.set_scanner(user["id"], bool(on))
    if on:
        pipeline.wake()
        return back("/", "Scanner ligado: o painel busca vagas novas e analisa a fila sozinho, sem parar.")
    return back("/", "Scanner desligado: nada é buscado nem analisado para a sua conta até você ligar de novo.")


@app.post("/adicionar")
def add_job(title: str = Form(...), company: str = Form(""), url: str = Form(""),
            description: str = Form(...), user: dict = Depends(current_user)):
    title, description = title.strip()[:200], description.strip()[:12000]
    if not title or not description:
        return back("/", "Informe pelo menos o título e a descrição da vaga.")
    url = url.strip()[:500]
    if safe_url(url) == "#":
        url = ""
    ext_id = url or hashlib.sha1((title + company + description[:200]).encode()).hexdigest()
    jid, _new = db.upsert_job({
        "source": "manual", "ext_id": ext_id, "title": title, "company": company.strip()[:120],
        "url": url, "location": "", "tags": [], "description": description, "salary": "",
        "posted_at": db.now(),
    }, owner_id=user["id"])
    if not db.link(user["id"], jid, title_hit=1):
        return back(f"/vaga/{jid}", "Essa vaga já estava na sua lista.")
    pipeline.score_one_in_background(user["id"], jid)
    return back(f"/vaga/{jid}", "Vaga adicionada. A análise aparece em alguns segundos (recarregue a página).")


@app.get("/vaga/{jid}")
def job_page(request: Request, jid: str, user: dict = Depends(current_user)):
    return page(request, "job.html", user, job=job_or_404(user["id"], jid), statuses=db.STATUSES,
                has_photo=db.get_extras(user["id"])["has_photo"],
                min_score=user["min_score"], events=db.recent_events(user["id"], 30, jid),
                questions=db.questions(user["id"], jid))


@app.post("/vaga/{jid}/status")
def job_status(jid: str, status: str = Form(...), user: dict = Depends(current_user)):
    job_or_404(user["id"], jid)
    if status not in db.STATUSES:
        raise HTTPException(400, "Status inválido")
    db.set_status(user["id"], jid, status)
    return back(f"/vaga/{jid}", f"Status alterado para: {db.STATUSES[status]}.")


PASTE_JS = (Path(__file__).parent / "static" / "paste.js").read_text(encoding="utf-8")


@app.get("/static/paste.js")
def paste_js():
    return Response(PASTE_JS, media_type="text/javascript")


@app.post("/vaga/{jid}/perguntar")
def job_ask(jid: str, question: str = Form(""), file: UploadFile | None = File(None),
            user: dict = Depends(current_user)):
    job_or_404(user["id"], jid)
    question = " ".join(question.split())[:1000]
    attachment = None
    if file is not None and file.filename:
        data = file.file.read(attach.DOC_MAX + 1)
        try:
            attachment = attach.read(file.filename, data)
        except attach.AttachError as e:
            return back(f"/vaga/{jid}", str(e))
    if attachment and len(question) < 3:
        question = "O que este anexo mostra e o que devo fazer ou responder?"
    if len(question) < 3:
        return back(f"/vaga/{jid}", "Escreva a pergunta antes de enviar.")
    try:
        pipeline.ask(user["id"], jid, question, attachment)
    except llm.LLMError as e:
        return back(f"/vaga/{jid}", f"Não consegui responder agora: {e}")
    return RedirectResponse(f"/vaga/{jid}#perguntas", status_code=303)


@app.post("/vaga/{jid}/perguntas/limpar")
def job_ask_clear(jid: str, user: dict = Depends(current_user)):
    job_or_404(user["id"], jid)
    db.clear_questions(user["id"], jid)
    return back(f"/vaga/{jid}", "Perguntas apagadas.")


@app.post("/vaga/{jid}/notas")
def job_notes(jid: str, notes: str = Form(""), user: dict = Depends(current_user)):
    job_or_404(user["id"], jid)
    db.set_notes(user["id"], jid, notes[:5000])
    return back(f"/vaga/{jid}", "Anotações salvas.")


@app.post("/vaga/{jid}/gerar")
def job_generate(jid: str, user: dict = Depends(current_user)):
    job_or_404(user["id"], jid)
    try:
        pipeline.tailor(user["id"], jid)
    except llm.LLMError as e:
        return back(f"/vaga/{jid}", f"Não consegui gerar o currículo: {e}")
    return back(f"/vaga/{jid}", "Currículo e carta gerados. Revise antes de enviar.")


@app.post("/vaga/{jid}/documentos")
def job_docs(jid: str, cv_md: str = Form(""), cover_md: str = Form(""), user: dict = Depends(current_user)):
    job_or_404(user["id"], jid)
    db.save_docs(user["id"], jid, cv_md[:20000], cover_md[:8000])
    return back(f"/vaga/{jid}", "Documentos salvos.")


@app.get("/vaga/{jid}/imprimir")
def job_print(request: Request, jid: str, doc: str = "cv", foto: int = 0,
              user: dict = Depends(current_user)):
    job = job_or_404(user["id"], jid)
    text = job["cover_md"] if doc == "carta" else job["cv_md"]
    if not text:
        raise HTTPException(404, "Documento ainda não gerado")
    is_cv = doc != "carta"
    return print_page(request, user, job["title"], text, foto and is_cv,
                      f"/vaga/{jid}/imprimir?doc=cv" if is_cv else "",
                      f"/vaga/{jid}/pdf?doc={'cv' if is_cv else 'carta'}")


@app.get("/vaga/{jid}/pdf")
def job_pdf(jid: str, doc: str = "cv", foto: int = 0, user: dict = Depends(current_user)):
    job = job_or_404(user["id"], jid)
    is_cv = doc != "carta"
    text = job["cv_md"] if is_cv else job["cover_md"]
    if not text:
        raise HTTPException(404, "Documento ainda não gerado")
    return pdf_download(user, text, foto and is_cv, "CV" if is_cv else "Carta", user["name"],
                        job["company"] or job["title"])


@app.get("/perfil")
def profile_page(request: Request, user: dict = Depends(current_user)):
    example = Path(config.ROOT / "profile.example.md")
    accounts = db.all_users() if user["is_admin"] else []
    return page(request, "profile.html", user, extras=db.get_extras(user["id"]),
                example=example.read_text(encoding="utf-8") if example.exists() else "",
                accounts=[{"name": a["name"], "email": a["email"], "created_at": a["created_at"]}
                          for a in accounts],
                invite_on=bool(auth.invite_code()))


@app.post("/perfil")
def profile_save(name: str = Form(...), profile: str = Form(""), preferences: str = Form(""),
                 keywords: str = Form(""), exclude_title: str = Form(""), min_score: int = Form(60),
                 user: dict = Depends(current_user)):
    profile, preferences = profile.strip()[:30000], preferences.strip()[:3000]
    changed = (profile, preferences) != ((user.get("profile") or "").strip(),
                                         (user.get("preferences") or "").strip())
    db.update_user(
        user["id"], name=" ".join(name.split())[:80] or user["name"], profile=profile,
        preferences=preferences, keywords=keywords.strip()[:3000],
        exclude_title=exclude_title.strip()[:3000], min_score=max(0, min(100, min_score)))
    n = pipeline.refresh_in_background(user["id"], rescore=changed)
    if n:
        return back("/perfil", f"Perfil salvo. Como o perfil ou as preferências mudaram, {n} vaga(s) já "
                               "analisadas voltaram para a fila e vão receber nota nova.")
    return back("/perfil", "Perfil salvo.")


@app.post("/perfil/senha")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    user: dict = Depends(current_user)):
    if not auth.verify_password(current, user["password_hash"]):
        return back("/perfil", "A senha atual está incorreta.")
    if len(new) < auth.MIN_PASSWORD or len(new) > 200:
        return back("/perfil", f"A nova senha precisa ter pelo menos {auth.MIN_PASSWORD} caracteres.")
    db.set_password(user["id"], auth.hash_password(new), keep_token=request.cookies.get(auth.SESSION_COOKIE))
    return back("/perfil", "Senha alterada. As outras sessões desta conta foram encerradas.")


@app.get("/perfil/foto")
def photo(user: dict = Depends(current_user)):
    found = db.get_photo(user["id"])  # só a foto da própria conta
    if not found:
        raise HTTPException(404, "Sem foto")
    return Response(found[0], media_type=found[1])


@app.post("/perfil/foto")
def photo_save(photo: UploadFile = File(...), user: dict = Depends(current_user)):
    data = photo.file.read(PHOTO_MAX + 1)
    if not data:
        return back("/perfil", "Escolha um arquivo de imagem.")
    if len(data) > PHOTO_MAX:
        return back("/perfil", "A foto precisa ter no máximo 2 MB.")
    mime = photo_mime(data)
    if not mime:
        return back("/perfil", "Formato não aceito. Envie uma foto JPG, PNG ou WebP.")
    db.set_photo(user["id"], data, mime)
    return back("/perfil", "Foto salva. Ela só entra no currículo quando você abrir a versão com foto.")


@app.post("/perfil/foto/remover")
def photo_remove(user: dict = Depends(current_user)):
    db.set_photo(user["id"], None)
    return back("/perfil", "Foto removida.")


@app.post("/perfil/curriculo/gerar")
def base_cv_generate(user: dict = Depends(current_user)):
    try:
        pipeline.base_cv(user["id"])
    except llm.LLMError as e:
        return back("/perfil", f"Não consegui gerar o currículo base: {e}")
    return back("/perfil", "Currículo base gerado a partir do seu perfil. Veja abaixo.")


@app.post("/perfil/curriculo")
def base_cv_save(base_cv: str = Form(""), user: dict = Depends(current_user)):
    db.set_base_cv(user["id"], base_cv.strip()[:20000])
    return back("/perfil", "Currículo base salvo.")


@app.get("/perfil/curriculo/imprimir")
def base_cv_print(request: Request, foto: int = 0, user: dict = Depends(current_user)):
    text = db.get_extras(user["id"])["base_cv"]
    if not text:
        raise HTTPException(404, "Currículo base ainda não gerado")
    return print_page(request, user, f"Currículo de {user['name']}", text, foto, "/perfil/curriculo/imprimir",
                      "/perfil/curriculo/pdf")


@app.get("/perfil/curriculo/pdf")
def base_cv_pdf(foto: int = 0, user: dict = Depends(current_user)):
    text = db.get_extras(user["id"])["base_cv"]
    if not text:
        raise HTTPException(404, "Currículo base ainda não gerado")
    return pdf_download(user, text, foto, "CV", user["name"])
