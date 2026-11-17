"""Banco SQLite. O catálogo de vagas é compartilhado; todo o resto é separado por usuário.

Regra de isolamento: nenhuma função devolve nota, status, currículo, anotação ou perfil sem
receber o `uid` e filtrar por ele.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import auth, config

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

# Abas do dashboard -> filtro SQL (:min é a nota mínima do usuário)
VIEWS = {
    "compativeis": ("Compatíveis", "u.status='analisada' AND u.score>=:min"),
    "curriculo": ("Currículo pronto", "u.status='curriculo'"),
    "submetidas": ("Submetidas", "u.status='submetida'"),
    "entrevistas": ("Entrevistas", "u.status IN ('entrevista','oferta')"),
    "recusadas": ("Recusadas", "u.status='recusada'"),
    "fila": ("Na fila", "u.status='nova'"),
    "descartadas": ("Descartadas", "(u.status='descartada' OR (u.status='analisada' AND u.score<:min))"),
    "todas": ("Todas", "1=1"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0, created_at TEXT,
  profile TEXT DEFAULT '', preferences TEXT DEFAULT '', keywords TEXT DEFAULT '',
  exclude_title TEXT DEFAULT '', min_score INTEGER DEFAULT 60);
CREATE TABLE IF NOT EXISTS extras(
  user_id INTEGER PRIMARY KEY, photo BLOB, photo_mime TEXT, base_cv TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT, url TEXT, location TEXT,
  tags TEXT, description TEXT, salary TEXT, posted_at TEXT, collected_at TEXT, owner_id INTEGER);
CREATE TABLE IF NOT EXISTS user_jobs(
  user_id INTEGER NOT NULL, job_id TEXT NOT NULL, title_hit INTEGER DEFAULT 0,
  status TEXT DEFAULT 'nova', score INTEGER, verdict TEXT, cv_md TEXT, cover_md TEXT,
  notes TEXT DEFAULT '', applied_at TEXT, updated_at TEXT, PRIMARY KEY(user_id, job_id));
CREATE INDEX IF NOT EXISTS user_jobs_status ON user_jobs(user_id, status);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, job_id TEXT, at TEXT, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_user ON events(user_id, id);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
  collected INTEGER DEFAULT 0, new INTEGER DEFAULT 0, scored INTEGER DEFAULT 0, errors TEXT DEFAULT '');
"""

_JOB_COLS = ("j.id, j.source, j.title, j.company, j.url, j.location, j.tags, j.description, j.salary,"
             " j.posted_at, j.owner_id")
_USER_JOB_COLS = ("u.status, u.score, u.verdict, u.cv_md, u.cover_md, u.notes, u.applied_at, u.title_hit,"
                  " u.updated_at")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_id(source: str, ext_id: str) -> str:
    return hashlib.sha1(f"{source}:{ext_id}".encode()).hexdigest()[:16]


@contextmanager
def connect():
    con = sqlite3.connect(config.data_dir() / "radar.db", timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


MIGRATIONS = (
    "ALTER TABLE users ADD COLUMN scanner INTEGER DEFAULT 1",    # scanner ligado/desligado por conta
    "ALTER TABLE user_jobs ADD COLUMN tries INTEGER DEFAULT 0",  # análises que falharam nessa vaga
)
MAX_TRIES = 3


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                con.execute(sql)
            except sqlite3.OperationalError:
                pass  # coluna já existe


# ---------------------------------------------------------------- usuários e sessões

def count_users() -> int:
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(email: str, name: str, password_hash: str, defaults: config.Config,
                profile: str = "") -> int | None:
    """Cria a conta já com as preferências padrão. A primeira conta vira administradora."""
    with connect() as con:
        first = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        try:
            cur = con.execute(
                "INSERT INTO users(email, name, password_hash, is_admin, created_at, profile, preferences,"
                " keywords, exclude_title, min_score) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (email, name, password_hash, int(first), now(), profile, defaults.preferences,
                 "\n".join(defaults.keywords), "\n".join(defaults.exclude_title), defaults.min_score))
        except sqlite3.IntegrityError:
            return None  # e-mail já cadastrado
        return cur.lastrowid


def get_user(uid: int) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None


def all_users() -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM users ORDER BY id")]


def update_user(uid: int, *, name: str, profile: str, preferences: str, keywords: str,
                exclude_title: str, min_score: int) -> None:
    with connect() as con:
        con.execute("UPDATE users SET name=?, profile=?, preferences=?, keywords=?, exclude_title=?, min_score=?"
                    " WHERE id=?", (name, profile, preferences, keywords, exclude_title, min_score, uid))


def set_scanner(uid: int, on: bool) -> None:
    with connect() as con:
        con.execute("UPDATE users SET scanner=? WHERE id=?", (int(on), uid))


def scanner_on(uid: int) -> bool:
    with connect() as con:
        row = con.execute("SELECT scanner FROM users WHERE id=?", (uid,)).fetchone()
        return bool(row and row["scanner"])


def get_extras(uid: int) -> dict:
    """Foto e currículo base ficam fora de `users` para não pesar em cada requisição."""
    with connect() as con:
        row = con.execute("SELECT photo IS NOT NULL AS has_photo, base_cv FROM extras WHERE user_id=?",
                          (uid,)).fetchone()
        return {"has_photo": bool(row and row["has_photo"]), "base_cv": (row["base_cv"] if row else "") or ""}


def get_photo(uid: int) -> tuple[bytes, str] | None:
    with connect() as con:
        row = con.execute("SELECT photo, photo_mime FROM extras WHERE user_id=?", (uid,)).fetchone()
        return (bytes(row["photo"]), row["photo_mime"]) if row and row["photo"] is not None else None


def set_photo(uid: int, data: bytes | None, mime: str | None = None) -> None:
    with connect() as con:
        con.execute("INSERT INTO extras(user_id, photo, photo_mime) VALUES(?,?,?)"
                    " ON CONFLICT(user_id) DO UPDATE SET photo=excluded.photo, photo_mime=excluded.photo_mime",
                    (uid, data, mime))


def set_base_cv(uid: int, text: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO extras(user_id, base_cv) VALUES(?,?)"
                    " ON CONFLICT(user_id) DO UPDATE SET base_cv=excluded.base_cv", (uid, text))


def set_password(uid: int, password_hash: str, keep_token: str | None = None) -> None:
    """Troca a senha e derruba as outras sessões abertas dessa conta."""
    with connect() as con:
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, uid))
        keep = auth.token_hash(keep_token) if keep_token else ""
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (uid, keep))


def create_session(uid: int) -> str:
    token = auth.new_token()
    expires = (datetime.now(timezone.utc) + timedelta(days=auth.SESSION_DAYS)).isoformat(timespec="seconds")
    with connect() as con:
        con.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
        con.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?,?,?)",
                    (auth.token_hash(token), uid, expires))
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    with connect() as con:
        row = con.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
            (auth.token_hash(token), now())).fetchone()
        return dict(row) if row else None


def delete_session(token: str | None) -> None:
    if token:
        with connect() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (auth.token_hash(token),))


# ---------------------------------------------------------------- catálogo de vagas (compartilhado)

def upsert_job(job: dict, owner_id: int | None = None) -> tuple[str, bool]:
    """Grava a vaga no catálogo. Vagas adicionadas à mão têm dono e só ele as vê."""
    ext = f"{owner_id}:{job['ext_id']}" if owner_id else job["ext_id"]
    jid = job_id(job["source"], ext)
    with connect() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO jobs(id, source, title, company, url, location, tags, description, salary,"
            " posted_at, collected_at, owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, job["source"], job["title"], job["company"], job["url"], job["location"],
             json.dumps(job["tags"], ensure_ascii=False), job["description"], job["salary"],
             job["posted_at"], now(), owner_id))
        return jid, bool(cur.rowcount)


def unlinked_jobs(uid: int, cutoff: str) -> list[dict]:
    """Vagas públicas recentes que este usuário ainda não tem na lista dele."""
    with connect() as con:
        rows = con.execute(
            f"SELECT {_JOB_COLS} FROM jobs j WHERE j.owner_id IS NULL"
            " AND (j.posted_at IS NULL OR j.posted_at>=?)"
            " AND NOT EXISTS(SELECT 1 FROM user_jobs u WHERE u.user_id=? AND u.job_id=j.id)", (cutoff, uid))
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            out.append(d)
        return out


def prune_catalog(days: int = 60) -> None:
    """Apaga vagas públicas antigas que ninguém tem na lista."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as con:
        con.execute("DELETE FROM jobs WHERE owner_id IS NULL AND collected_at<?"
                    " AND NOT EXISTS(SELECT 1 FROM user_jobs u WHERE u.job_id=jobs.id)", (cutoff,))


# ---------------------------------------------------------------- vagas de um usuário

def add_event(con, uid: int, jid: str, kind: str, detail: str = "") -> None:
    con.execute("INSERT INTO events(user_id, job_id, at, kind, detail) VALUES(?,?,?,?,?)",
                (uid, jid, now(), kind, detail))


def link(uid: int, jid: str, title_hit: int = 0) -> bool:
    with connect() as con:
        cur = con.execute("INSERT OR IGNORE INTO user_jobs(user_id, job_id, title_hit, updated_at) VALUES(?,?,?,?)",
                          (uid, jid, title_hit, now()))
        return bool(cur.rowcount)


def _row(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["tags"] = json.loads(d["tags"] or "[]")
    d["verdict"] = json.loads(d["verdict"]) if d["verdict"] else {}
    d["status_label"] = STATUSES.get(d["status"], d["status"])
    return d


_SELECT = f"SELECT {_JOB_COLS}, {_USER_JOB_COLS} FROM user_jobs u JOIN jobs j ON j.id=u.job_id WHERE u.user_id=:uid"


def get_job(uid: int, jid: str) -> dict | None:
    with connect() as con:
        return _row(con.execute(_SELECT + " AND u.job_id=:jid", {"uid": uid, "jid": jid}).fetchone())


def list_jobs(uid: int, view: str, min_score: int, q: str = "", limit: int = 300) -> list[dict]:
    where = VIEWS.get(view, VIEWS["todas"])[1]
    params: dict = {"uid": uid, "min": min_score, "limit": limit}
    sql = _SELECT + f" AND {where}"
    if q:
        sql += " AND (j.title LIKE :q OR j.company LIKE :q OR j.tags LIKE :q)"
        params["q"] = f"%{q}%"
    sql += " ORDER BY u.score IS NULL, u.score DESC, j.posted_at DESC LIMIT :limit"
    with connect() as con:
        return [_row(r) for r in con.execute(sql, params)]


def pending(uid: int, limit: int) -> list[dict]:
    with connect() as con:
        # vaga cuja análise falhou vai para o fim da fila e sai dela depois de MAX_TRIES tentativas
        rows = con.execute(_SELECT + " AND u.status='nova' AND u.tries<:max"
                           " ORDER BY u.tries, u.title_hit DESC, j.posted_at DESC LIMIT :limit",
                           {"uid": uid, "limit": limit, "max": MAX_TRIES})
        return [_row(r) for r in rows]


def add_try(uid: int, jid: str) -> None:
    with connect() as con:
        con.execute("UPDATE user_jobs SET tries=tries+1 WHERE user_id=? AND job_id=?", (uid, jid))


def reset_tries(uid: int) -> None:
    with connect() as con:
        con.execute("UPDATE user_jobs SET tries=0 WHERE user_id=? AND status='nova'", (uid,))


def requeue_scored(uid: int) -> int:
    """Perfil ou preferências mudaram: as notas antigas deixam de valer. Só mexe nas vagas apenas
    analisadas; as que já têm currículo, candidatura ou descarte manual ficam como estão."""
    with connect() as con:
        cur = con.execute("UPDATE user_jobs SET status='nova', score=NULL, verdict=NULL, tries=0, updated_at=?"
                          " WHERE user_id=? AND status='analisada'", (now(), uid))
        return cur.rowcount


def save_score(uid: int, jid: str, score: int, verdict: dict) -> None:
    with connect() as con:
        con.execute("UPDATE user_jobs SET score=?, verdict=?, status='analisada', updated_at=?"
                    " WHERE user_id=? AND job_id=? AND status='nova'",
                    (score, json.dumps(verdict, ensure_ascii=False), now(), uid, jid))
        add_event(con, uid, jid, "analisada", f"nota {score}")


def save_docs(uid: int, jid: str, cv_md: str, cover_md: str, generated: bool = False) -> None:
    with connect() as con:
        con.execute("UPDATE user_jobs SET cv_md=?, cover_md=?, updated_at=? WHERE user_id=? AND job_id=?",
                    (cv_md, cover_md, now(), uid, jid))
        if generated:
            con.execute("UPDATE user_jobs SET status='curriculo' WHERE user_id=? AND job_id=?"
                        " AND status IN ('nova','analisada','descartada')", (uid, jid))
            add_event(con, uid, jid, "currículo gerado")
        else:
            add_event(con, uid, jid, "currículo editado")


def set_status(uid: int, jid: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    with connect() as con:
        con.execute("UPDATE user_jobs SET status=?, updated_at=? WHERE user_id=? AND job_id=?",
                    (status, now(), uid, jid))
        if status == "submetida":
            con.execute("UPDATE user_jobs SET applied_at=? WHERE user_id=? AND job_id=? AND applied_at IS NULL",
                        (now(), uid, jid))
        add_event(con, uid, jid, "status", STATUSES[status])


def set_notes(uid: int, jid: str, notes: str) -> None:
    with connect() as con:
        con.execute("UPDATE user_jobs SET notes=?, updated_at=? WHERE user_id=? AND job_id=?",
                    (notes, now(), uid, jid))


def counts(uid: int, min_score: int) -> dict:
    base = "SELECT COUNT(*) FROM user_jobs u WHERE u.user_id=:uid AND "
    params = {"uid": uid, "min": min_score}
    with connect() as con:
        one = lambda where: con.execute(base + where, params).fetchone()[0]  # noqa: E731
        out = {
            "coletadas": one("1=1"),
            "analisadas": one("u.score IS NOT NULL"),
            "ja_submetidas": one("u.applied_at IS NOT NULL"),
        }
        for key, (_label, where) in VIEWS.items():
            out[key] = one(where)
        return out


def recent_events(uid: int, limit: int = 15, jid: str | None = None) -> list[dict]:
    sql = ("SELECT e.at, e.kind, e.detail, e.job_id, j.title, j.company FROM events e"
           " JOIN jobs j ON j.id=e.job_id WHERE e.user_id=?")
    args: tuple = (uid,)
    if jid:
        sql += " AND e.job_id=?"
        args += (jid,)
    sql += " ORDER BY e.id DESC LIMIT ?"
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args + (limit,))]


# ---------------------------------------------------------------- rodadas de coleta

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
