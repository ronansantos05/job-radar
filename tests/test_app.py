"""Testes sem rede e sem chave: o LLM e as fontes são substituídos por dublês."""
import os

import pytest

os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["DASH_USER"] = "admin"
os.environ["DASH_PASSWORD"] = "senha-de-teste"
os.environ["LLM_DELAY_SECONDS"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, llm, main, pipeline, sources  # noqa: E402

AUTH = ("admin", "senha-de-teste")


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
    db.init()
    config.write_profile("Dev Python com experiência em machine learning.")
    yield


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


# ------------------------------------------------------------ filtro

def test_match_uses_whole_words():
    cfg = config.Config(keywords=["ml", "python"], exclude_title=["sales"])
    assert pipeline.match(fake_job(title="ML Engineer", description="", tags=[]), cfg) == 1
    assert pipeline.match(fake_job(title="HTML developer", description="", tags=[]), cfg) is None
    assert pipeline.match(fake_job(title="Backend dev", description="Uses Python daily", tags=[]), cfg) == 0
    assert pipeline.match(fake_job(title="Sales Engineer (Python)"), cfg) is None


def test_match_ignores_accents():
    cfg = config.Config(keywords=["ciência de dados"])
    assert pipeline.match(fake_job(title="Bolsa em Ciencia de Dados"), cfg) == 1


# ------------------------------------------------------------ fontes

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


# ------------------------------------------------------------ pipeline

def test_run_collects_filters_and_scores(monkeypatch):
    monkeypatch.setattr(sources, "SOURCES", {"fake": lambda: [
        fake_job(1), fake_job(2, title="Sales Manager"), fake_job(3, title="Old Python job",
                                                                  posted_at="2020-01-01T00:00:00+00:00"),
    ], "broken": lambda: 1 / 0})
    monkeypatch.setattr(config, "load_config", lambda *a: config.Config(
        keywords=["python"], exclude_title=["sales"], sources=["fake", "broken"]))
    monkeypatch.setattr(llm, "chat", lambda *a, **k: '{"score": 150, "resumo": "bom", "pontos_fortes": '
                                                      '["python"], "tipo_contrato": "PJ", "remoto": true}')
    result = pipeline.run_once()
    assert (result["collected"], result["new"], result["scored"]) == (3, 1, 1)
    assert any("broken" in e for e in result["errors"])
    (job,) = db.list_jobs("todas", 60)
    assert job["score"] == 100 and job["status"] == "analisada"  # nota fora da faixa é limitada
    assert job["verdict"]["tipo_contrato"] == "pj" and job["verdict"]["remoto"] is True
    assert pipeline.run_once()["new"] == 0  # não duplica


def test_scoring_needs_profile(monkeypatch):
    config.write_profile("")
    db.insert_job(fake_job())
    scored, errors = pipeline.score_pending(config.Config())
    assert scored == 0 and "Perfil vazio" in errors[0]


def test_tailor_splits_cv_and_cover(monkeypatch):
    jid = db.insert_job(fake_job())
    monkeypatch.setattr(llm, "chat", lambda *a, **k: f"# CV\ntexto\n{pipeline.SEPARATOR}\nPrezados,")
    pipeline.tailor(jid)
    job = db.get_job(jid)
    assert job["cv_md"] == "# CV\ntexto" and job["cover_md"] == "Prezados," and job["status"] == "curriculo"


# ------------------------------------------------------------ dashboard

def test_requires_login(client):
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "errada")).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_pages_render(client):
    jid = db.insert_job(fake_job(title="<script>alert(1)</script> Dev"))
    db.save_score(jid, 82, {"resumo": "ok", "pontos_fortes": ["a"], "lacunas": [], "tipo_contrato": "pj",
                            "remoto": True, "aceita_pais": None, "idioma": "en"})
    home = client.get("/", auth=AUTH)
    assert home.status_code == 200 and "<script>alert(1)" not in home.text and "82" in home.text
    for view in db.VIEWS:
        assert client.get(f"/?view={view}", auth=AUTH).status_code == 200
    assert client.get(f"/vaga/{jid}", auth=AUTH).status_code == 200
    assert client.get("/perfil", auth=AUTH).status_code == 200
    assert client.get("/vaga/nao-existe", auth=AUTH).status_code == 404


def test_status_flow_and_csrf(client):
    jid = db.insert_job(fake_job())
    blocked = client.post(f"/vaga/{jid}/status", data={"status": "submetida"}, auth=AUTH,
                          headers={"Origin": "https://site-malicioso.example"})
    assert blocked.status_code == 403 and db.get_job(jid)["status"] == "nova"
    ok = client.post(f"/vaga/{jid}/status", data={"status": "submetida"}, auth=AUTH, follow_redirects=False)
    assert ok.status_code == 303
    job = db.get_job(jid)
    assert job["status"] == "submetida" and job["applied_at"]
    assert db.counts(60)["ja_submetidas"] == 1
    assert client.post(f"/vaga/{jid}/status", data={"status": "hackeada"}, auth=AUTH).status_code == 400


def test_manual_job_and_print_is_sanitized(client, monkeypatch):
    monkeypatch.setattr(pipeline, "score_one_in_background", lambda jid: None)
    r = client.post("/adicionar", auth=AUTH, follow_redirects=False,
                    data={"title": "Vaga do LinkedIn", "url": "javascript:alert(1)", "description": "Python"})
    assert r.status_code == 303
    (job,) = db.list_jobs("todas", 60)
    assert job["source"] == "manual" and job["url"] == ""
    db.save_docs(job["id"], "# Nome\n<img src=x onerror=alert(1)> [x](javascript:alert(1))", "")
    page = client.get(f"/vaga/{job['id']}/imprimir", auth=AUTH)
    assert page.status_code == 200 and "<h1>Nome</h1>" in page.text
    assert "<img" not in page.text and "javascript:" not in page.text


def test_profile_save(client):
    client.post("/perfil", data={"profile": "Novo perfil"}, auth=AUTH)
    assert config.read_profile() == "Novo perfil"
