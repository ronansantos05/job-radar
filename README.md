# 📡 job-radar

[![tests](https://github.com/ronansantos05/job-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/ronansantos05/job-radar/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

> **EN:** A self-hosted job-hunting assistant. It collects remote jobs from public APIs, scores each
> one against your profile with an LLM (Groq), writes a tailored résumé and cover letter using only
> facts from your profile, and tracks every application in a password-protected dashboard.
> FastAPI + SQLite + Docker; runs on any small VPS.

Assistente de busca de trabalho remoto que roda na sua própria VPS:

1. **Coleta** vagas remotas de APIs públicas, de tempos em tempos.
2. **Dá uma nota de 0 a 100** para cada vaga comparando com o seu perfil e as suas preferências
   (remoto, PJ/freelance/bolsa, país…), usando um LLM da [Groq](https://groq.com).
3. **Adapta o currículo e a carta de apresentação** a cada vaga, usando **somente** o que você escreveu
   no seu perfil. Nada é inventado.
4. **Acompanha tudo num dashboard**: o que foi coletado, analisado, o que já tem currículo, o que foi
   submetido, entrevistas e recusas, com histórico e anotações por vaga.

O envio da candidatura é sempre seu: o sistema prepara, você revisa e clica. Automatizar o envio viola
os termos de sites como o LinkedIn e costuma terminar em conta bloqueada.

## Como funciona

```
fontes (APIs/RSS) ─► filtro por palavras-chave ─► LLM dá a nota ─► dashboard
                                                                     │
                  seu perfil (privado) ─────────► LLM adapta currículo e carta
                                                                     │
                                            você revisa, envia e marca "submetida"
```

| Parte | Arquivo |
|---|---|
| Coletores (Remotive, RemoteOK, Himalayas, Jobicy, We Work Remotely) | `app/sources.py` |
| Filtro, nota e currículo adaptado | `app/pipeline.py` |
| Cliente da Groq (API compatível com OpenAI) | `app/llm.py` |
| Banco SQLite (vagas, histórico, rodadas) | `app/db.py` |
| Dashboard e agendador | `app/main.py`, `app/templates/` |

Vagas do **LinkedIn**, de e-mail ou de indicação entram pelo botão **“Adicionar uma vaga à mão”**: você
cola o texto e ela recebe nota e currículo como as outras.

## Privacidade: o que nunca vai para o Git

Este repositório é público, então os seus dados ficam fora dele:

| Dado | Onde fica | Protegido por |
|---|---|---|
| Seu perfil (bio, experiência, contatos) | `data/profile.md` (editável pelo dashboard) | `.gitignore` |
| Banco com vagas, currículos e anotações | `data/jobs.db` | `.gitignore` |
| Chave da Groq e senha do dashboard | `.env` | `.gitignore` |

O `config.yaml` é versionado: coloque nele só preferências de busca, nunca dados pessoais.

## Rodando no seu computador

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # preencha GROQ_API_KEY e DASH_PASSWORD
uvicorn app.main:app --port 8000
```

Abra <http://localhost:8000>, entre com o usuário e a senha do `.env`, vá em **Perfil**, escreva o seu
perfil e clique em **Atualizar agora**.

## Colocando na VPS (Docker)

```bash
git clone https://github.com/ronansantos05/job-radar.git && cd job-radar
cp .env.example .env && nano .env      # GROQ_API_KEY, DASH_PASSWORD (longa!) e, se tiver, DOMAIN
```

Escolha **uma** forma de acessar:

**A) Com domínio e HTTPS automático (recomendado).** Aponte o DNS do domínio para o IP da VPS, preencha
`DOMAIN=radar.seudominio.com` no `.env` e rode:

```bash
docker compose --profile https up -d --build
```

**B) Sem domínio, por túnel SSH (seguro, nada fica exposto).** Suba só o app e acesse do seu computador:

```bash
docker compose up -d --build
ssh -L 8000:127.0.0.1:8000 usuario@ip-da-vps      # depois abra http://localhost:8000
```

**C) Direto pelo IP, sem HTTPS.** Coloque `BIND=0.0.0.0` no `.env` e abra `http://ip-da-vps:8000`.
Funciona, mas a senha trafega sem criptografia: use só para testar.

Os dados ficam no volume `radar_data`. Para atualizar: `git pull && docker compose up -d --build`.

## Uso no dia a dia

1. Abra a aba **Compatíveis**: as vagas já vêm ordenadas pela nota.
2. Entre numa vaga, leia a análise (pontos fortes, lacunas, tipo de contrato) e clique em
   **Gerar currículo adaptado**.
3. Revise e edite o texto, abra a **versão para imprimir** e salve como PDF (Ctrl+P).
4. Envie a candidatura no site da vaga e clique em **Marcar como submetida**.
5. Atualize o status conforme a resposta (entrevista, oferta, recusada) e anote o que precisar.

## Configuração (`config.yaml`)

| Campo | O que faz |
|---|---|
| `preferences` | Texto livre que o modelo usa na nota. Diga o que é eliminatório para você |
| `keywords` | A vaga precisa ter pelo menos uma (título, tags ou descrição) para ser analisada |
| `exclude_title` | Palavras no título que descartam a vaga sem gastar chamada do modelo |
| `max_age_days` | Ignora vagas mais antigas que isso |
| `min_score` | Nota a partir da qual a vaga aparece em “Compatíveis” |
| `max_scored_per_run` | Limite de análises por rodada (protege a cota gratuita da Groq) |
| `interval_hours` | Intervalo entre as coletas |
| `sources` | Fontes ativas |

O modelo é escolhido no `.env` (`GROQ_MODEL`). Qualquer API compatível com OpenAI funciona trocando
`GROQ_BASE_URL`.

## Segurança

- Dashboard com login (HTTP Basic); sem `DASH_PASSWORD` o app gera uma senha temporária em vez de abrir.
- Proteção contra CSRF nos formulários e cabeçalhos restritivos (CSP sem scripts, `noindex`).
- O texto das vagas é tratado como **dado não confiável**: é isolado nos prompts, a resposta do modelo
  é validada e o Markdown gerado é sanitizado antes de virar HTML.
- O container roda sem root.

## Testes

```bash
pip install pytest && python -m pytest
```

Os testes não usam rede nem chave: as fontes e o LLM são substituídos por dublês.

## Licença

MIT. As vagas pertencem às respectivas fontes; o job-radar sempre leva você ao anúncio original.
