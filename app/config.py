"""Configuração: config.yaml (público, sem segredos) + variáveis de ambiente (.env, privado)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SOURCES = ["remotive", "remoteok", "himalayas", "jobicy", "weworkremotely", "remotar", "backendbr"]


def load_env(path: Path | None = None) -> None:
    """Lê um .env simples (CHAVE=valor) sem sobrescrever o que já está no ambiente."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    preferences: str = ""
    keywords: list[str] = field(default_factory=list)
    exclude_title: list[str] = field(default_factory=list)
    max_age_days: int = 21
    min_score: int = 60
    max_scored_per_run: int = 40
    fetch_minutes: float = 30
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.getenv("CONFIG_PATH", ROOT / "config.yaml"))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    cfg = Config()
    cfg.preferences = str(data.get("preferences", "")).strip()
    cfg.keywords = [str(k) for k in data.get("keywords", [])]
    cfg.exclude_title = [str(k) for k in data.get("exclude_title", [])]
    cfg.max_age_days = int(data.get("max_age_days", cfg.max_age_days))
    cfg.min_score = int(data.get("min_score", cfg.min_score))
    cfg.max_scored_per_run = int(data.get("max_scored_per_run", cfg.max_scored_per_run))
    cfg.fetch_minutes = float(data.get("fetch_minutes", cfg.fetch_minutes))
    cfg.sources = [str(s) for s in data.get("sources", DEFAULT_SOURCES)]
    return cfg


def data_dir() -> Path:
    """Pasta privada (fora do git): banco de dados e perfil do candidato."""
    d = Path(os.getenv("DATA_DIR", ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_path() -> Path:
    return data_dir() / "profile.md"


def read_profile() -> str:
    p = profile_path()
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def write_profile(text: str) -> None:
    profile_path().write_text(text.strip() + "\n", encoding="utf-8")
