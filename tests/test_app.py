"""Testes sem rede e sem chave: o LLM e as fontes são substituídos por dublês."""
import os

import pytest

os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["LLM_DELAY_SECONDS"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config, db, llm, main, pipeline, sources  # noqa: E402

PASSWORD = "senha-de-teste-123"
VERDICT = '{"score": 82, "resumo": "bom", "pontos_fortes": ["python"], "tipo_contrato": "PJ", "remoto": true}'


def fake_job(n=1, title="Python Machine Learning Engineer", **extra):
    job = {"source": "fake", "ext_id": str(n), "title": title, "company": "ACME",
           "url": f"https://example.com/{n}", "location": "Worldwide", "tags": ["python"],
           "description": "We need Python and ML.", "salary": "", "posted_at": db.now()}
    job.update(extra)
    return job


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "chave-falsa")
    monkeypatch.delenv("SIGNUP_CODE", raising=False)
    monkeypatch.delenv("DASH_PASSWORD", raising=False)
    monkeypatch.setattr(pipeline, "wake", lambda: None)
    monkeypatch.setattr(pipeline, "score_one_in_background", lambda uid, jid: None)
    auth._failures.clear()
    db.init()
    yield


def new_client():
    return TestClient(main.app, follow_redirects=False)


def signup(client, email, name="Pessoa", code=None):
    data = {"name": name, "email": email, "password": PASSWORD}
    if code is not None:
        data["code"] = code
    return client.post("/cadastro", data=data)


def make_user(email="a@example.com", profile="Dev Python com experiência em machine learning.", **prefs):
    uid = db.create_user(email, "Pessoa", auth.hash_password(PASSWORD), config.Config(
        keywords=prefs.get("keywords", ["python"]), exclude_title=prefs.get("exclude_title", [])),
        profile=profile)
    return db.get_user(uid)


def add_job_for(user, n=1, **extra):
    jid, _ = db.upsert_job(fake_job(n, **extra))
    db.link(user["id"], jid, 1)
    return jid


# ------------------------------------------------------------ filtro e fontes

def test_match_uses_whole_words():
    cfg = config.Config(keywords=["ml", "python"], exclude_title=["sales"])
    assert pipeline.match(fake_job(title="ML Engineer", description="", tags=[]), cfg) == 1
    assert pipeline.match(fake_job(title="HTML developer", description="", tags=[]), cfg) is None
    assert pipeline.match(fake_job(title="Backend dev", description="Uses Python daily", tags=[]), cfg) == 0
    assert pipeline.match(fake_job(title="Sales Engineer (Python)"), cfg) is None
    assert pipeline.match(fake_job(title="Bolsa em Ciencia de Dados"), config.Config(keywords=["ciência de dados"])) == 1


def test_strip_html_and_dates():
    text = sources.strip_html("<p>Hello <b>world</b></p><script>alert(1)</script><ul><li>a</li><li>b</li></ul>")
    assert "Hello world" in text and "alert" not in text and "- a" in text
    assert sources.parse_date("1794676700").startswith("2026-11-1")
    assert sources.parse_date("2026-11-11T20:17:48") == "2026-11-11T20:17:48+00:00"
    assert sources.parse_date("Fri, 13 Nov 2026 08:00:21 +0000") == "2026-11-13T08:00:21+00:00"
    assert sources.parse_date("lixo") is None


def test_parsers():
    (r,) = sources.parse_remoteok([{"legal": "aviso"}, {"id": "9", "position": "Dev", "company": "X",
                                                        "url": "https://r/9", "tags": ["python"],
                                                        "description": "<p>oi</p>", "salary_min": 0,
                                                        "salary_max": 0, "date": "2026-11-13T08:00:21+00:00"}])
    assert (r["title"], r["description"], r["salary"]) == ("Dev", "oi", "")
    (h,) = sources.parse_himalayas({"jobs": [{"title": "ML", "companyName": "Y", "guid": "https://h/1",
                                               "locationRestrictions": ["Brazil"], "minSalary": 850,
                                               "maxSalary": 1250, "currency": "USD", "salaryPeriod": "monthly",
                                               "pubDate": 1794676700}]})
    assert h["location"] == "Brazil" and h["salary"] == "USD 850–1,250 /monthly"
    rss = ("<rss><channel><item><title>ACME: Senior Dev</title><region>Anywhere</region>"
           "<category>Back-End</category><description>&lt;p&gt;hi&lt;/p&gt;</description>"
           "<pubDate>Fri, 13 Nov 2026 08:00:21 +0000</pubDate><guid>https://w/1</guid>"
           "<link>https://w/1</link></item></channel></rss>")
    (w,) = sources.parse_wwr(rss)
    assert (w["company"], w["title"], w["description"]) == ("ACME", "Senior Dev", "hi")


# ------------------------------------------------------------ pipeline por usuário

def test_run_gives_each_user_their_own_list_and_scores(monkeypatch):
    ana = make_user("ana@example.com", keywords=["python"], exclude_title=["sales"])
    bia = make_user("bia@example.com", keywords=["designer"])
    sem_perfil = make_user("caio@example.com", profile="", keywords=["python"], exclude_title=["sales"])
    monkeypatch.setattr(sources, "SOURCES", {"fake": lambda: [
        fake_job(1), fake_job(2, title="Sales Manager"), fake_job(3, title="Product Designer", tags=[],
                                                                  description="Figma"),
        fake_job(4, title="Old Python job", posted_at="2020-01-01T00:00:00+00:00"),
    ], "broken": lambda: 1 / 0})
    monkeypatch.setattr(config, "load_config", lambda *a: config.Config(sources=["fake", "broken"]))
    seen_profiles = []

    def fake_chat(system, user, **k):
        seen_profiles.append(user)
        return '{"score": 150, "resumo": "bom", "tipo_contrato": "PJ", "remoto": true}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    result = pipeline.run_once()
    assert (result["collected"], result["new"], result["scored"]) == (4, 3, 2)
    assert any("broken" in e for e in result["errors"])

    (a_job,) = db.list_jobs(ana["id"], "todas", 60)
    (b_job,) = db.list_jobs(bia["id"], "todas", 60)
    assert a_job["title"] == "Python Machine Learning Engineer" and b_job["title"] == "Product Designer"
    assert a_job["score"] == 100 and a_job["verdict"]["tipo_contrato"] == "pj"  # nota limitada a 100
    (c_job,) = db.list_jobs(sem_perfil["id"], "todas", 60)
    assert c_job["score"] is None and c_job["status"] == "nova"  # sem perfil, não gasta o modelo
    assert pipeline.run_once()["scored"] == 0  # não repete análise


def test_tailor_uses_only_that_users_profile(monkeypatch):
    ana = make_user("ana@example.com", profile="PERFIL-DA-ANA")
    bia = make_user("bia@example.com", profile="PERFIL-DA-BIA")
    jid = add_job_for(ana)
    db.link(bia["id"], jid, 1)
    prompts = []
    monkeypatch.setattr(llm, "chat", lambda s, u, **k: prompts.append(u) or f"# CV\ntexto\n{pipeline.SEPARATOR}\nPrezados,")
    pipeline.tailor(ana["id"], jid)
    assert "PERFIL-DA-ANA" in prompts[0] and "PERFIL-DA-BIA" not in prompts[0]
    a, b = db.get_job(ana["id"], jid), db.get_job(bia["id"], jid)
    assert a["cv_md"] == "# CV\ntexto" and a["cover_md"] == "Prezados," and a["status"] == "curriculo"
    assert b["cv_md"] is None and b["status"] == "nova"


# ------------------------------------------------------------ cadastro e login

def test_first_signup_is_admin_then_signup_closes():
    c = new_client()
    assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/entrar"
    r = signup(c, "dono@example.com")
    assert r.status_code == 303 and r.headers["location"].startswith("/perfil")
    assert db.get_user_by_email("dono@example.com")["is_admin"] == 1
    assert c.get("/perfil").status_code == 200
    # sem código de convite configurado, ninguém mais se cadastra
    other = new_client()
    signup(other, "intruso@example.com")
    assert db.get_user_by_email("intruso@example.com") is None


def test_signup_with_invite_code(monkeypatch):
    monkeypatch.setenv("SIGNUP_CODE", "convite-secreto")
    c = new_client()
    signup(c, "a@example.com", code="errado")
    assert db.count_users() == 0
    signup(c, "a@example.com", code="convite-secreto")
    signup(new_client(), "b@example.com", code="convite-secreto")
    assert db.count_users() == 2 and db.get_user_by_email("b@example.com")["is_admin"] == 0
    signup(new_client(), "a@example.com", code="convite-secreto")  # e-mail repetido
    signup(new_client(), "invalido", code="convite-secreto")
    assert db.count_users() == 2
    r = new_client().post("/cadastro", data={"name": "X", "email": "c@example.com", "password": "curta",
                                             "code": "convite-secreto"})
    assert r.status_code == 303 and db.count_users() == 2


def test_login_logout_and_lockout():
    make_user("a@example.com")
    c = new_client()
    assert c.post("/entrar", data={"email": "a@example.com", "password": "errada"}).headers["location"].startswith("/entrar")
    assert c.get("/").status_code == 303
    assert c.post("/entrar", data={"email": "A@Example.com ", "password": PASSWORD}).headers["location"] == "/"
    assert c.get("/").status_code == 200
    cookie = c.cookies.get(auth.SESSION_COOKIE)
    assert cookie and db.session_user(cookie)["email"] == "a@example.com"
    c.post("/sair")
    assert db.session_user(cookie) is None and c.get("/").status_code == 303
    # depois de várias senhas erradas, nem a senha certa entra
    for _ in range(auth.MAX_FAILURES):
        c.post("/entrar", data={"email": "a@example.com", "password": "errada"})
    assert c.post("/entrar", data={"email": "a@example.com", "password": PASSWORD}).headers["location"].startswith("/entrar")


def test_passwords_are_hashed_and_change_ends_other_sessions():
    user = make_user("a@example.com")
    assert PASSWORD not in user["password_hash"] and auth.verify_password(PASSWORD, user["password_hash"])
    assert not auth.verify_password("outra", user["password_hash"])
    here, there = new_client(), new_client()
    for c in (here, there):
        c.post("/entrar", data={"email": "a@example.com", "password": PASSWORD})
    here.post("/perfil/senha", data={"current": "errada", "new": "nova-senha-456"})
    assert there.get("/").status_code == 200
    here.post("/perfil/senha", data={"current": PASSWORD, "new": "nova-senha-456"})
    assert here.get("/").status_code == 200 and there.get("/").status_code == 303
    assert auth.verify_password("nova-senha-456", db.get_user(user["id"])["password_hash"])


# ------------------------------------------------------------ isolamento entre contas

def login(email):
    c = new_client()
    c.post("/entrar", data={"email": email, "password": PASSWORD})
    return c


def test_users_cannot_see_or_change_each_others_data():
    ana = make_user("ana@example.com", profile="SEGREDO-DA-ANA")
    bia = make_user("bia@example.com", profile="SEGREDO-DA-BIA")
    jid = add_job_for(ana)
    db.save_score(ana["id"], jid, 91, {"resumo": "ANALISE-DA-ANA", "tipo_contrato": "pj"})
    db.save_docs(ana["id"], jid, "CV-DA-ANA", "CARTA-DA-ANA")
    db.set_notes(ana["id"], jid, "NOTA-DA-ANA")
    a, b = login("ana@example.com"), login("bia@example.com")

    assert "ANALISE-DA-ANA" in a.get(f"/vaga/{jid}").text
    # Bia não tem essa vaga na lista: não lê, não altera, não gera, não imprime
    assert b.get(f"/vaga/{jid}").status_code == 404
    assert b.get(f"/vaga/{jid}/imprimir").status_code == 404
    for path, data in ((f"/vaga/{jid}/status", {"status": "recusada"}), (f"/vaga/{jid}/notas", {"notes": "x"}),
                       (f"/vaga/{jid}/documentos", {"cv_md": "x"}), (f"/vaga/{jid}/gerar", {})):
        assert b.post(path, data=data).status_code == 404
    job = db.get_job(ana["id"], jid)
    assert (job["status"], job["notes"], job["cv_md"]) == ("analisada", "NOTA-DA-ANA", "CV-DA-ANA")

    # mesmo quando as duas têm a mesma vaga, os dados de cada uma são separados
    db.link(bia["id"], jid, 1)
    page = b.get(f"/vaga/{jid}").text
    for secret in ("ANALISE-DA-ANA", "CV-DA-ANA", "CARTA-DA-ANA", "NOTA-DA-ANA", "SEGREDO-DA-ANA"):
        assert secret not in page
    b.post(f"/vaga/{jid}/status", data={"status": "submetida"})
    assert db.get_job(ana["id"], jid)["status"] == "analisada"
    assert db.counts(ana["id"], 60)["ja_submetidas"] == 0 and db.counts(bia["id"], 60)["ja_submetidas"] == 1

    home = b.get("/?view=todas").text + b.get("/perfil").text
    assert "SEGREDO-DA-BIA" in home and "ANA" not in home.replace("ana@example.com", "")
    assert all(e["kind"] != "analisada" for e in db.recent_events(bia["id"]))


def test_manual_jobs_are_private_and_profile_saves_per_user():
    ana, bia = make_user("ana@example.com"), make_user("bia@example.com")
    a, b = login("ana@example.com"), login("bia@example.com")
    r = a.post("/adicionar", data={"title": "Vaga secreta do LinkedIn", "url": "javascript:alert(1)",
                                   "description": "Python"})
    jid = r.headers["location"].split("/vaga/")[1].split("?")[0]
    assert db.get_job(ana["id"], jid)["url"] == "" and b.get(f"/vaga/{jid}").status_code == 404
    assert "Vaga secreta" not in b.get("/?view=todas").text
    pipeline.link_user(bia, config.Config(keywords=["python", "linkedin"]))  # a coleta não vaza vaga manual
    assert db.list_jobs(bia["id"], "todas", 60) == []

    a.post("/perfil", data={"name": "Ana", "profile": "Novo perfil da Ana", "preferences": "remoto",
                            "keywords": "python\nml", "exclude_title": "", "min_score": "250"})
    saved = db.get_user(ana["id"])
    assert (saved["profile"], saved["keywords"], saved["min_score"]) == ("Novo perfil da Ana", "python\nml", 100)
    assert db.get_user(bia["id"])["profile"] != "Novo perfil da Ana"
    assert pipeline.user_config(saved, config.Config()).keywords == ["python", "ml"]


# ------------------------------------------------------------ páginas e segurança

def test_pages_render_and_escape_html():
    user = make_user("a@example.com")
    jid = add_job_for(user, title="<script>alert(1)</script> Dev")
    db.save_score(user["id"], jid, 82, {"resumo": "ok", "pontos_fortes": ["a"], "lacunas": [],
                                        "tipo_contrato": "pj", "remoto": True, "aceita_pais": None, "idioma": "en"})
    c = login("a@example.com")
    home = c.get("/")
    assert home.status_code == 200 and "<script>alert(1)" not in home.text and "82" in home.text
    for view in db.VIEWS:
        assert c.get(f"/?view={view}").status_code == 200
    assert c.get(f"/vaga/{jid}").status_code == 200 and c.get("/vaga/nao-existe").status_code == 404
    assert new_client().get("/entrar").status_code == 200 and new_client().get("/healthz").text == "ok"
    db.save_docs(user["id"], jid, "# Nome\n<img src=x onerror=alert(1)> [x](javascript:alert(1))", "")
    printed = c.get(f"/vaga/{jid}/imprimir")
    assert "<h1>Nome</h1>" in printed.text and "<img" not in printed.text and "javascript:" not in printed.text


def test_csrf_and_status_flow():
    user = make_user("a@example.com")
    jid = add_job_for(user)
    c = login("a@example.com")
    blocked = c.post(f"/vaga/{jid}/status", data={"status": "submetida"},
                     headers={"Origin": "https://site-malicioso.example"})
    assert blocked.status_code == 403 and db.get_job(user["id"], jid)["status"] == "nova"
    for bad in ({"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"}):
        assert c.post(f"/vaga/{jid}/status", data={"status": "submetida"}, headers=bad).status_code == 403
    assert db.get_job(user["id"], jid)["status"] == "nova"
    # Navegador de verdade no próprio painel, inclusive atrás de proxy (Easypanel) e com "Origin: null".
    for good in ({"Sec-Fetch-Site": "same-origin", "Origin": "null"},
                 {"Origin": "https://radar.exemplo.com", "X-Forwarded-Host": "radar.exemplo.com"},
                 {"Origin": "http://testserver"}):
        assert c.post(f"/vaga/{jid}/status", data={"status": "nova"}, headers=good).status_code == 303
    assert c.get("/entrar").headers["referrer-policy"] == "same-origin"
    assert c.post(f"/vaga/{jid}/status", data={"status": "submetida"}).status_code == 303
    job = db.get_job(user["id"], jid)
    assert job["status"] == "submetida" and job["applied_at"]
    assert c.post(f"/vaga/{jid}/status", data={"status": "hackeada"}).status_code == 400


def test_photo_and_base_cv(monkeypatch):
    ana, bia = make_user("a@example.com"), make_user("b@example.com")
    jid = add_job_for(ana)
    db.save_docs(ana["id"], jid, "# Ana", "Carta")
    a, b = login("a@example.com"), login("b@example.com")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    upload = lambda data, name="foto.png": a.post("/perfil/foto", files={"photo": (name, data, "image/png")})

    # só imagem de verdade (pela assinatura) e até 2 MB
    for bad in (b"<svg onload=alert(1)>", b"MZ" + b"0" * 64, png + b"0" * main.PHOTO_MAX):
        assert upload(bad).status_code == 303 and not db.get_extras(ana["id"])["has_photo"]
    assert a.get("/perfil/foto").status_code == 404
    assert "foto=1" not in a.get(f"/vaga/{jid}").text

    assert upload(png).status_code == 303
    got = a.get("/perfil/foto")
    assert got.content == png and got.headers["content-type"] == "image/png"
    assert b.get("/perfil/foto").status_code == 404  # a foto é só da conta dela
    assert "foto=1" in a.get(f"/vaga/{jid}").text and 'src="/perfil/foto"' in a.get("/perfil").text

    # a foto só entra quando pedida, e nunca na carta
    url = f"/vaga/{jid}/imprimir"
    assert 'class="photo"' not in a.get(url).text and "Ver com foto" in a.get(url).text
    assert '<img class="photo"' in a.get(url + "?foto=1").text
    assert '<img class="photo"' not in a.get(url + "?doc=carta&foto=1").text

    # currículo base: gerado só do perfil, por conta
    assert a.get("/perfil/curriculo/imprimir").status_code == 404
    seen = {}
    monkeypatch.setattr(llm, "chat", lambda system, user, **kw: seen.update(user=user) or "# Currículo da Ana")
    assert a.post("/perfil/curriculo/gerar").status_code == 303
    assert "Dev Python" in seen["user"] and db.get_extras(ana["id"])["base_cv"] == "# Currículo da Ana"
    assert "<h1>Currículo da Ana</h1>" in a.get("/perfil/curriculo/imprimir?foto=1").text
    assert "Currículo da Ana" in a.get("/perfil").text
    assert b.get("/perfil/curriculo/imprimir").status_code == 404 and "Currículo da Ana" not in b.get("/perfil").text
    assert a.post("/perfil/curriculo", data={"base_cv": "# Editado"}).status_code == 303
    assert db.get_extras(ana["id"])["base_cv"] == "# Editado" and db.get_extras(ana["id"])["has_photo"]

    assert a.post("/perfil/foto/remover").status_code == 303 and a.get("/perfil/foto").status_code == 404
    assert '<img class="photo"' not in a.get(url + "?foto=1").text


def test_scanner_toggle_and_failed_jobs(monkeypatch):
    ana, bia = make_user("a@example.com"), make_user("b@example.com")
    for n in (1, 2):
        add_job_for(ana, n)
        add_job_for(bia, n)
    a = login("a@example.com")
    assert "Scanner ligado" in a.get("/").text
    assert a.post("/scanner", data={"on": "0"}).status_code == 303
    assert "Ligar scanner" in a.get("/").text and db.scanner_on(bia["id"])

    # desligado = nada é analisado para ela; a outra conta segue normalmente
    monkeypatch.setattr(sources, "SOURCES", {})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: VERDICT)
    assert pipeline.run_once(fetch=None)["scored"] == 2
    assert db.counts(ana["id"], 60)["analisadas"] == 0 and db.counts(bia["id"], 60)["analisadas"] == 2

    # religado: uma vaga que o modelo não consegue analisar não trava a fila
    a.post("/scanner", data={"on": "1"})
    calls = []

    def flaky(system, user, **kw):
        calls.append(user)
        if "https://example.com/1" in user or len(calls) == 1:
            raise llm.LLMError("resposta ruim")
        return VERDICT

    monkeypatch.setattr(llm, "chat", flaky)
    first = pipeline.run_once(fetch=None)
    assert first["scored"] == 1 and first["errors"] and not first["stop"]
    assert pipeline.state["error"] == "" or "resposta ruim" in pipeline.state["error"]
    assert pipeline.run_once(fetch=None)["scored"] == 1  # a que falhou voltou no fim da fila
    assert db.counts(ana["id"], 60)["analisadas"] == 2


def test_json_mode_failure_falls_back_to_plain_text(monkeypatch):
    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body, self.headers = status, body, {}
            self.text = str(body)

        def json(self):
            return self._body

    sent = []

    def post(url, json, headers, timeout):
        sent.append(json)
        if "response_format" in json:
            return Reply(400, {"error": {"message": "Failed to generate JSON", "code": "json_validate_failed"}})
        return Reply(200, {"choices": [{"message": {"content": 'Claro: {"score": 70}'}}]})

    monkeypatch.setattr(llm.httpx, "post", post)
    assert llm.chat_json("s", "u") == {"score": 70}
    assert len(sent) == 2 and "response_format" not in sent[1]


def test_generate_all_and_no_stale_errors(monkeypatch):
    ana, bia = make_user("a@example.com"), make_user("b@example.com")
    jobs = [add_job_for(ana, n) for n in (1, 2, 3)]
    other = add_job_for(bia, 1)
    for jid in jobs[:2]:
        db.save_score(ana["id"], jid, 80, {})
    db.save_score(ana["id"], jobs[2], 10, {})   # abaixo da nota mínima: fica de fora
    db.save_score(bia["id"], other, 90, {})     # de outra conta: fica de fora
    a = login("a@example.com")
    assert "Gerar currículo e carta para as 2 compatíveis" in a.get("/").text

    started = []
    monkeypatch.setattr(pipeline.threading, "Thread", lambda target, daemon: started.append(target) or
                        type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(llm, "chat", lambda s, u, **k: f"# CV\n{pipeline.SEPARATOR}\nCarta")
    assert a.post("/gerar-todas").status_code == 303
    assert pipeline.tailoring[ana["id"]] == 2 and "faltam 2" in a.get("/").text
    assert a.post("/gerar-todas").status_code == 303 and len(started) == 1  # não dispara dois lotes
    started[0]()
    assert ana["id"] not in pipeline.tailoring
    assert [bool(db.get_job(ana["id"], j)["cv_md"]) for j in jobs] == [True, True, False]
    assert not db.get_job(bia["id"], other)["cv_md"]
    assert db.counts(ana["id"], 60)["curriculo"] == 2

    # erro passageiro de análise não fica exposto no painel
    add_job_for(ana, 9)
    monkeypatch.setattr(sources, "SOURCES", {})

    def broken(*args, **kwargs):
        raise llm.LLMError("HTTP 400: Failed to generate JSON")

    monkeypatch.setattr(llm, "chat", broken)
    pipeline.state["error"] = ""
    pipeline.run_once()
    page = a.get("/").text
    assert "Failed to generate JSON" not in page and "Análise interrompida" not in page


def test_pdf_download_is_private_and_clean():
    ana, bia = make_user("a@example.com"), make_user("b@example.com")
    jid = add_job_for(ana, 1)
    db.save_docs(ana["id"], jid, "# Ana Souza\n\nSão Paulo – “ML”\n\n## Experiência\n- **Python** e *SQL* ≥ 5 anos",
                 "Prezados,\n\nSegue a carta.")
    a, b = login("a@example.com"), login("b@example.com")
    r = a.get(f"/vaga/{jid}/pdf?doc=cv")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.pdf"')
    assert a.get(f"/vaga/{jid}/pdf?doc=carta").content.startswith(b"%PDF")
    assert a.get(f"/vaga/{jid}/pdf?doc=cv&foto=1").status_code == 200   # sem foto enviada: sai sem foto
    assert b.get(f"/vaga/{jid}/pdf?doc=cv").status_code == 404          # outra conta não baixa
    assert TestClient(main.app, follow_redirects=False).get(f"/vaga/{jid}/pdf").status_code in (303, 401)
    page = a.get(f"/vaga/{jid}").text
    assert "Baixar currículo (PDF)" in page and "Baixar carta (PDF)" in page
    assert a.get("/perfil/curriculo/pdf").status_code == 404            # currículo base ainda não gerado
    db.set_base_cv(ana["id"], "# Ana Souza\n\nResumo")
    assert a.get("/perfil/curriculo/pdf").content.startswith(b"%PDF")
    # sem fonte no sistema, cai na fonte embutida e não quebra com símbolos
    from app import pdf
    old, pdf.FONT_SETS = pdf.FONT_SETS, ()
    try:
        assert pdf.render("# Ana – “teste” ≥ 5 ✓ 日本", "x").startswith(b"%PDF")
    finally:
        pdf.FONT_SETS = old
    assert pdf.filename("CV", "João Ação", "Acme / Inc. (Remote)") == "CV - Joao Acao - Acme Inc. Remote.pdf"


def test_pdf_layout_tolerates_sloppy_markdown():
    from app import pdf
    text = ("# Ana Souza\n**Email:** a@example.com\n**Summary**\nMachine\u2011learning engineer.\n"
            "- Built end\u2011to\u2011end pipelines\n  with Docker\n## Experience\n"
            "### Head of R&D — Acme | 2021 – Present\n- Led the team\n\n```\n---\n")
    kinds = [b for b in pdf.blocks(text) if b[0] != "gap"]
    assert kinds == [("h1", "Ana Souza"), ("p", "**Email:** a@example.com"), ("h2", "Summary"),
                     ("p", "Machine-learning engineer."), ("bullet", "Built end-to-end pipelines with Docker"),
                     ("h2", "Experience"), ("h3", "Head of R&D — Acme | 2021 – Present"),
                     ("bullet", "Led the team")]
    m = pdf.DATE_TAIL.match("Head of R&D — Acme | 2021 – Present")
    assert m and (m.group(1), m.group(2)) == ("Head of R&D — Acme", "2021 – Present")
    assert pdf.DATE_TAIL.match("MSc — UFRJ (2019 - 2022)").group(2) == "2019 - 2022"
    assert not pdf.DATE_TAIL.match("job-radar — LLM job ranker")
    assert pdf._spans("a **b** *c* [d](https://x.y)") == [("", "a "), ("B", "b"), ("", " "), ("I", "c"),
                                                          ("", " "), ("", "d")]
    assert "Cada fato fica onde o perfil o coloca" in pipeline.TAILOR_SYSTEM
    assert "{" not in pipeline.CV_RULES


def test_brazilian_sources_and_rescore_on_preferences_change():
    (r,) = sources.parse_remotar({"data": [
        {"id": 7, "title": "Cientista de Dados", "type": "remote", "active": True, "subtitle": "ML",
         "description": "<p>Python</p>", "externalLink": "https://x.y/vaga", "createdAt": "2026-11-16T10:00:00-03:00",
         "company": {"name": "Acme"}, "jobTags": [{"tag": {"name": "💼 PJ"}}, {"tag": {"name": "🌍 100% Remoto"}}],
         "jobCategories": [{"category": {"name": "Dados"}}], "jobRequirements": [{"description": "SQL"}],
         "jobSalary": {"from": None, "to": None}},
        {"id": 8, "title": "Híbrida", "type": "hybrid", "active": True},
        {"id": 9, "title": "Vencida", "type": "remote", "active": False}]})
    assert (r["company"], r["url"], r["tags"]) == ("Acme", "https://x.y/vaga", ["PJ", "100% Remoto", "Dados"])
    assert "Python" in r["description"] and "Requisitos: SQL" in r["description"] and r["posted_at"]
    (g,) = sources.parse_github_issues([
        {"number": 1, "title": "[Remoto] Dev Python Pleno na Acme", "html_url": "https://github.com/o/r/issues/1",
         "labels": [{"name": "PJ"}, {"name": "Remoto"}], "body": "Vaga", "created_at": "2026-11-09T12:00:00Z"},
        {"number": 2, "title": "[São Paulo] Dev presencial", "labels": [{"name": "CLT"}], "html_url": "u"},
        {"number": 3, "title": "PR", "pull_request": {}, "labels": [{"name": "Remoto"}]}], "backendbr")
    assert (g["title"], g["company"], g["tags"]) == ("Dev Python Pleno na Acme", "Acme", ["PJ", "Remoto"])
    assert {"remotar", "backendbr"} <= set(sources.SOURCES)

    ana, bia = make_user("a@example.com"), make_user("b@example.com")
    scored, with_cv, other = add_job_for(ana, 1), add_job_for(ana, 2), add_job_for(bia, 1)
    db.save_score(ana["id"], scored, 20, {})
    db.save_score(ana["id"], with_cv, 80, {})
    db.save_docs(ana["id"], with_cv, "# CV", "Carta", generated=True)
    db.save_score(bia["id"], other, 70, {})
    monkey_wake = pipeline.wake
    pipeline.wake = lambda: None
    try:
        a = login("a@example.com")
        form = {"name": "Ana", "profile": ana["profile"], "preferences": ana.get("preferences") or "",
                "keywords": "python", "exclude_title": "", "min_score": "60"}
        r = a.post("/perfil", data=form)                      # nada de perfil/preferências mudou
        assert db.get_job(ana["id"], scored)["status"] == "analisada"
        r = a.post("/perfil", data={**form, "preferences": "Prefiro empresas brasileiras."})
        assert r.status_code == 303 and "nota%20nova" in r.headers["location"] or "nota+nova" in r.headers["location"]
        job = db.get_job(ana["id"], scored)
        assert (job["status"], job["score"]) == ("nova", None)
        assert db.get_job(ana["id"], with_cv)["status"] == "curriculo"   # já trabalhada: não volta
        assert db.get_job(bia["id"], other)["score"] == 70               # outra conta: intocada
    finally:
        pipeline.wake = monkey_wake


def test_questions_about_a_job_are_private(monkeypatch):
    ana, bia = make_user("a@example.com"), make_user("b@example.com")
    jid = add_job_for(ana, 1)
    add_job_for(bia, 1)   # a mesma vaga do catálogo, na lista da outra conta
    db.save_score(ana["id"], jid, 88, {"resumo": "Encaixe forte"})
    a, b = login("a@example.com"), login("b@example.com")
    seen = []

    def fake(system, user, **k):
        seen.append(user)
        return "Estude **PyTorch**. <script>x</script>"

    monkeypatch.setattr(llm, "chat", fake)
    assert "Perguntar sobre esta vaga" in a.get(f"/vaga/{jid}").text
    r = a.post(f"/vaga/{jid}/perguntar", data={"question": "O que estudar?"})
    assert r.status_code == 303 and r.headers["location"].endswith("#perguntas")
    assert "O que estudar?" in seen[0] and ana["profile"] in seen[0] and "Encaixe forte" in seen[0]
    page = a.get(f"/vaga/{jid}").text
    assert "O que estudar?" in page and "<strong>PyTorch</strong>" in page and "<script>x" not in page
    a.post(f"/vaga/{jid}/perguntar", data={"question": "E o salário?"})
    assert "P: O que estudar?" in seen[1]                      # a conversa anterior vai junto
    assert "O que estudar?" not in b.get(f"/vaga/{jid}").text  # a outra conta não vê
    assert a.post(f"/vaga/{jid}/perguntar", data={"question": " "}).status_code == 303 and len(seen) == 2

    def broken(*args, **kwargs):
        raise llm.LLMError("limite atingido", fatal=True)

    monkeypatch.setattr(llm, "chat", broken)
    r = a.post(f"/vaga/{jid}/perguntar", data={"question": "Outra dúvida?"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert len(db.questions(ana["id"], jid)) == 2
    b.post(f"/vaga/{jid}/perguntas/limpar")
    assert len(db.questions(ana["id"], jid)) == 2              # limpar na conta B não apaga as de A
    a.post(f"/vaga/{jid}/perguntas/limpar")
    assert db.questions(ana["id"], jid) == []


def test_question_attachments(monkeypatch):
    import io
    import zipfile
    from app import attach, pdf
    ana = make_user("a@example.com")
    jid = add_job_for(ana, 1)
    a = login("a@example.com")
    calls = []

    def fake(system, user, **k):
        calls.append((user, k.get("image")))
        return "Resposta"

    monkeypatch.setattr(llm, "chat", fake)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    r = a.post(f"/vaga/{jid}/perguntar", data={"question": ""}, files={"file": ("../../tela 1.png", png, "x/y")})
    assert r.headers["location"].endswith("#perguntas")
    assert calls[-1][1] == (png, "image/png") and "tela 1.png" in calls[-1][0]
    assert "[anexo: tela 1.png]" in db.questions(ana["id"], jid)[-1]["question"]

    doc = io.BytesIO()
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:t>Qual sua pretens&#227;o?</w:t></w:p>"
                                        "<w:p><w:t>Envie at&#233; sexta</w:t></w:p></w:document>")
    a.post(f"/vaga/{jid}/perguntar", data={"question": "Como respondo?"},
           files={"file": ("form.docx", doc.getvalue(), "application/octet-stream")})
    assert calls[-1][1] is None and "Qual sua pretensão?\nEnvie até sexta" in calls[-1][0]

    made = pdf.render("# Teste\n\nPergunta do formulario: disponibilidade imediata?", "t")
    a.post(f"/vaga/{jid}/perguntar", data={"question": "E isso?"}, files={"file": ("f.pdf", made, "application/pdf")})
    assert "disponibilidade imediata" in calls[-1][0]

    n = len(calls)
    for name, data in (("a.exe", b"MZ\x00\x00" + b"\x00" * 50), ("big.png", png + b"0" * attach.IMAGE_MAX),
                       ("vazio.pdf", b"%PDF-1.4 nada"), ("x.zip", b"PK\x03\x04lixo")):
        r = a.post(f"/vaga/{jid}/perguntar", data={"question": "Oi, e isso?"}, files={"file": (name, data, "x/y")})
        assert r.status_code == 303 and "msg=" in r.headers["location"], name
    assert len(calls) == n                                   # nada disso chegou ao modelo
    assert attach.safe_name("C:\\x\\<b>print</b>.png") == "b.png"

    page = a.get(f"/vaga/{jid}")
    assert 'enctype="multipart/form-data"' in page.text and "/static/paste.js" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "unsafe-inline'; img" in page.headers["content-security-policy"]   # estilo, não script
    js = TestClient(main.app).get("/static/paste.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "clipboardData" in js.text
