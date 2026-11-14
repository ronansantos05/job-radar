# 📡 job-radar

[![tests](https://github.com/ronansantos05/job-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/ronansantos05/job-radar/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

> **EN:** A self-hosted job-hunting assistant. It collects remote jobs from public APIs, scores each
> one against your profile with an LLM (Groq), writes a tailored résumé and cover letter using only
> facts from your profile, and tracks every application in a dashboard. Multi-user: each account has
> its own isolated profile, preferences, scores and documents. FastAPI + SQLite + Docker.

Assistente de busca de trabalho remoto que roda na sua própria VPS:

1. **Coleta** vagas remotas de APIs públicas, de tempos em tempos.
2. **Dá uma nota de 0 a 100** para cada vaga comparando com o seu perfil e as suas preferências
   (remoto, PJ/freelance/bolsa, país…), usando um LLM da [Groq](https://groq.com).
3. **Adapta o currículo e a carta de apresentação** a cada vaga, usando **somente** o que você escreveu
   no seu perfil. Nada é inventado.
4. **Acompanha tudo num dashboard**: o que foi coletado, analisado, o que já tem currículo, o que foi
   submetido, entrevistas e recusas, com histórico e anotações por vaga.
5. **Várias pessoas, dados separados**: cada conta tem perfil, preferências, notas, currículos e
   anotações próprios. Ninguém vê os dados de outra pessoa, nem quem administra o painel.

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
| Contas, senhas e sessões | `app/auth.py` |
| Banco SQLite (catálogo de vagas compartilhado; todo o resto por usuário) | `app/db.py` |
| Dashboard e agendador | `app/main.py`, `app/templates/` |

Vagas do **LinkedIn**, de e-mail ou de indicação entram pelo botão **“Adicionar uma vaga à mão”**: você
cola o texto e ela recebe nota e currículo como as outras.

## Privacidade: o que nunca vai para o Git

Este repositório é público, então os seus dados ficam fora dele:

| Dado | Onde fica | Protegido por |
|---|---|---|
| Perfis, vagas, currículos e anotações de cada conta | `data/radar.db` | `.gitignore` |
| Chave da Groq e código de convite | `.env` (ou variáveis do painel de deploy) | `.gitignore` |

As senhas são guardadas só como hash (scrypt) e as sessões só como hash do token.

O `config.yaml` é versionado: coloque nele só preferências de busca, nunca dados pessoais.

## Rodando no seu computador

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # preencha GROQ_API_KEY e SIGNUP_CODE
uvicorn app.main:app --port 8000
```

Abra <http://localhost:8000>, clique em **Criar conta**, escreva o seu perfil e clique em **Atualizar agora**.

## Contas e convites

- A **primeira conta criada vira administradora**. Crie a sua logo depois de subir o sistema.
- Para outras pessoas se cadastrarem, defina `SIGNUP_CODE` e passe o código para elas. Sem essa variável,
  o cadastro fica fechado depois da primeira conta.
- Todas as contas usam a mesma chave da Groq (a do servidor). O limite de análises por rodada
  (`max_scored_per_run`) vale por pessoa.
- Cada pessoa define, na página **Perfil**, o próprio perfil, as preferências, as palavras-chave e a
  nota mínima. O `config.yaml` só fornece os valores iniciais de uma conta nova.

## Colocando na VPS (Docker)

```bash
git clone https://github.com/ronansantos05/job-radar.git && cd job-radar
cp .env.example .env && nano .env      # GROQ_API_KEY, SIGNUP_CODE e, se tiver, DOMAIN
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

**Easypanel, Coolify e parecidos:** crie um app a partir deste repositório com build por `Dockerfile`,
defina `GROQ_API_KEY` e `SIGNUP_CODE` nas variáveis, monte um **volume** em `/app/data` e aponte o domínio
para a porta `8000`. O HTTPS fica por conta do painel.

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
| `preferences`, `keywords`, `exclude_title`, `min_score` | Valores iniciais de uma conta nova (cada pessoa edita os seus em **Perfil**) |
| `max_age_days` | Ignora vagas mais antigas que isso |
| `max_scored_per_run` | Limite de análises por rodada (protege a cota gratuita da Groq) |
| `interval_hours` | Intervalo entre as coletas |
| `sources` | Fontes ativas |

O modelo é escolhido no `.env` (`GROQ_MODEL`). Qualquer API compatível com OpenAI funciona trocando
`GROQ_BASE_URL`.

## Segurança

- Login por e-mail e senha, com senhas em hash scrypt, cookie de sessão `HttpOnly` e bloqueio temporário
  depois de várias tentativas erradas.
- **Isolamento por conta**: toda consulta de vaga, nota, currículo, anotação e perfil é filtrada pelo
  usuário logado; vagas adicionadas à mão só existem para quem as adicionou. Há testes automáticos disso.
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
