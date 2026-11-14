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
    monkeypatch.setattr(pipeline, "refresh_in_background", lambda uid: True)
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
    assert c.post(f"/vaga/{jid}/status", data={"status": "submetida"}).status_code == 303
    job = db.get_job(user["id"], jid)
    assert job["status"] == "submetida" and job["applied_at"]
    assert c.post(f"/vaga/{jid}/status", data={"status": "hackeada"}).status_code == 400
