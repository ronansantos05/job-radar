"""Cliente mínimo da API da Groq (compatível com OpenAI). A chave vem só do ambiente."""
from __future__ import annotations

import json
import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


class LLMError(Exception):
    """Erro ao chamar o modelo. `fatal` indica que não adianta tentar as próximas vagas."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def configured() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def model() -> str:
    return os.getenv("GROQ_MODEL") or DEFAULT_MODEL


def chat(system: str, user: str, *, json_mode: bool = False, max_tokens: int = 1200,
         temperature: float = 0.2, retries: int = 3) -> str:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY não configurada", fatal=True)
    payload = {
        "model": model(),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    url = (os.getenv("GROQ_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"

    last = "sem resposta"
    for attempt in range(retries):
        try:
            r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=90)
        except httpx.HTTPError as e:
            last = f"falha de rede: {type(e).__name__}"
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError):
                raise LLMError("resposta inesperada da API")
        if r.status_code in (401, 403):
            raise LLMError("chave da Groq recusada (confira GROQ_API_KEY)", fatal=True)
        if r.status_code == 404:
            raise LLMError(f"modelo '{model()}' não encontrado (ajuste GROQ_MODEL)", fatal=True)
        last = f"HTTP {r.status_code}: {r.text[:160]}"
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("retry-after", 0))
            except ValueError:
                wait = 0
            if wait > 90:  # limite diário estourado: não adianta insistir agora
                raise LLMError("limite de uso da Groq atingido, tente mais tarde", fatal=True)
            time.sleep(max(wait, 3 * (attempt + 1)))
            continue
        break
    raise LLMError(last, fatal="429" in last)


def chat_json(system: str, user: str, **kwargs) -> dict:
    text = chat(system, user, json_mode=True, **kwargs)
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("o modelo não devolveu JSON")
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            raise LLMError("o modelo devolveu JSON inválido")
    if not isinstance(data, dict):
        raise LLMError("o modelo não devolveu um objeto JSON")
    return data
