"""Banco SQLite: vagas, histórico de eventos e rodadas de coleta."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

STATUSES = {
    "nova": "Na fila de análise",
    "analisada": "Analisada",
    "curriculo": "Currículo pronto",
    "submetida": "Submetida",
    "entrevista": "Entrevista",
    "oferta": "Oferta",
    "recusada": "Recusada",
    "descartada": "Descartada",
}

# Abas do dashboard -> filtro SQL (:min é a nota mínima do config.yaml)
VIEWS = {
    "compativeis": ("Compatíveis", "status='analisada' AND score>=:min"),
    "curriculo": ("Currículo pronto", "status='curriculo'"),
    "submetidas": ("Submetidas", "status='submetida'"),
    "entrevistas": ("Entrevistas", "status IN ('entrevista','oferta')"),
    "recusadas": ("Recusadas", "status='recusada'"),
    "fila": ("Na fila", "status='nova'"),
    "descartadas": ("Descartadas", "status='descartada' OR (status='analisada' AND score<:min)"),
    "todas": ("Todas", "1=1"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT, url TEXT, location TEXT,
  tags TEXT, description TEXT, salary TEXT, posted_at TEXT, collected_at TEXT,
  title_hit INTEGER DEFAULT 0, status TEXT DEFAULT 'nova', score INTEGER, verdict TEXT,
  cv_md TEXT, cover_md TEXT, notes TEXT DEFAULT '', applied_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, at TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
  collected INTEGER DEFAULT 0, new INTEGER DEFAULT 0, scored INTEGER DEFAULT 0, errors TEXT DEFAULT '');
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_id(source: str, ext_id: str) -> str:
    return hashlib.sha1(f"{source}:{ext_id}".encode()).hexdigest()[:16]


@contextmanager
def connect():
    con = sqlite3.connect(config.data_dir() / "jobs.db", timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


def add_event(con, jid: str, kind: str, detail: str = "") -> None:
    con.execute("INSERT INTO events(job_id, at, kind, detail) VALUES(?,?,?,?)", (jid, now(), kind, detail))


def insert_job(job: dict, title_hit: int = 0) -> str | None:
    """Insere a vaga se ainda não existir. Devolve o id quando é nova, senão None."""
    jid = job_id(job["source"], job["ext_id"])
    with connect() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO jobs(id, source, title, company, url, location, tags, description,"
            " salary, posted_at, collected_at, title_hit, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, job["source"], job["title"], job["company"], job["url"], job["location"],
             json.dumps(job["tags"], ensure_ascii=False), job["description"], job["salary"],
             job["posted_at"], now(), title_hit, now()),
        )
        if cur.rowcount:
            add_event(con, jid, "coletada", job["source"])
            return jid
    return None


def _row(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["tags"] = json.loads(d["tags"] or "[]")
    d["verdict"] = json.loads(d["verdict"]) if d["verdict"] else {}
    d["status_label"] = STATUSES.get(d["status"], d["status"])
    return d


def get_job(jid: str) -> dict | None:
    with connect() as con:
        return _row(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def list_jobs(view: str, min_score: int, q: str = "", limit: int = 300) -> list[dict]:
    where = VIEWS.get(view, VIEWS["todas"])[1]
    params: dict = {"min": min_score, "limit": limit}
    sql = f"SELECT * FROM jobs WHERE ({where})"
    if q:
        sql += " AND (title LIKE :q OR company LIKE :q OR tags LIKE :q)"
        params["q"] = f"%{q}%"
    sql += " ORDER BY score IS NULL, score DESC, posted_at DESC LIMIT :limit"
    with connect() as con:
        return [_row(r) for r in con.execute(sql, params)]


def pending(limit: int) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE status='nova' ORDER BY title_hit DESC, posted_at DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]


def save_score(jid: str, score: int, verdict: dict) -> None:
    with connect() as con:
        con.execute("UPDATE jobs SET score=?, verdict=?, status='analisada', updated_at=? WHERE id=? AND status='nova'",
                    (score, json.dumps(verdict, ensure_ascii=False), now(), jid))
        add_event(con, jid, "analisada", f"nota {score}")


def save_docs(jid: str, cv_md: str, cover_md: str, generated: bool = False) -> None:
    with connect() as con:
        con.execute("UPDATE jobs SET cv_md=?, cover_md=?, updated_at=? WHERE id=?", (cv_md, cover_md, now(), jid))
        if generated:
            con.execute("UPDATE jobs SET status='curriculo' WHERE id=? AND status IN ('nova','analisada','descartada')",
                        (jid,))
            add_event(con, jid, "currículo gerado")
        else:
            add_event(con, jid, "currículo editado")


def set_status(jid: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    with connect() as con:
        con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, now(), jid))
        if status == "submetida":
            con.execute("UPDATE jobs SET applied_at=? WHERE id=? AND applied_at IS NULL", (now(), jid))
        add_event(con, jid, "status", STATUSES[status])


def set_notes(jid: str, notes: str) -> None:
    with connect() as con:
        con.execute("UPDATE jobs SET notes=?, updated_at=? WHERE id=?", (notes, now(), jid))


def counts(min_score: int) -> dict:
    with connect() as con:
        one = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
        out = {
            "coletadas": one("SELECT COUNT(*) FROM jobs"),
            "analisadas": one("SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL"),
            "ja_submetidas": one("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"),
        }
        for key, (_label, where) in VIEWS.items():
            out[key] = con.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", {"min": min_score}).fetchone()[0]
        return out


def recent_events(limit: int = 15, jid: str | None = None) -> list[dict]:
    sql = ("SELECT e.at, e.kind, e.detail, e.job_id, j.title, j.company FROM events e"
           " JOIN jobs j ON j.id = e.job_id")
    args: tuple = ()
    if jid:
        sql += " WHERE e.job_id=?"
        args = (jid,)
    else:
        sql += " WHERE e.kind != 'coletada'"
    sql += " ORDER BY e.id DESC LIMIT ?"
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args + (limit,))]


def start_run() -> int:
    with connect() as con:
        return con.execute("INSERT INTO runs(started_at) VALUES(?)", (now(),)).lastrowid


def finish_run(run_id: int, collected: int, new: int, scored: int, errors: list[str]) -> None:
    with connect() as con:
        con.execute("UPDATE runs SET finished_at=?, collected=?, new=?, scored=?, errors=? WHERE id=?",
                    (now(), collected, new, scored, "\n".join(errors), run_id))


def last_run() -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
