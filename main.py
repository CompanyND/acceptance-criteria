# -*- coding: utf-8 -*-
"""
JIRA → Claude Acceptance Criteria Bot
======================================
Nasazení: Railway
Požadavky: fastapi, httpx, uvicorn
"""

from __future__ import annotations

import os
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# Konfigurace – Environment Variables (Railway)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
JIRA_BASE_URL     = os.environ.get("JIRA_BASE_URL", "")       # https://netdirect.atlassian.net
JIRA_EMAIL        = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN    = os.environ.get("JIRA_API_TOKEN", "")

# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def jira_auth() -> tuple[str, str]:
    return (JIRA_EMAIL, JIRA_API_TOKEN)


async def get_jira_issue(issue_key: str) -> dict:
    """Stáhne detail JIRA ticketu."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, auth=jira_auth(), timeout=15)
        resp.raise_for_status()
        return resp.json()


async def update_ac_field(issue_key: str, ac_text: str) -> None:
    """Zapíše AK do custom fieldu customfield_10207 (Akceptační kritéria AI)."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    payload = {
        "fields": {
            "customfield_10207": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "codeBlock",
                        "content": [{"type": "text", "text": ac_text}]
                    }
                ]
            }
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.put(url, json=payload, auth=jira_auth(), timeout=15)
        resp.raise_for_status()
        print(f"[JIRA] AK zapsána do customfield_10207 na ticketu {issue_key}")


async def call_claude(system_prompt: str, user_prompt: str) -> str:
    """Zavolá Claude API a vrátí textovou odpověď."""
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 2048,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


def build_system_prompt() -> str:
    return """Jsi zkušený QA analytik. Tvojí úlohou je generovat akceptační kritéria pro JIRA tickety.

Pravidla formátování:
- Piš v češtině
- Používej výhradně tyto klíčová slova v hranatých závorkách: [SCENARIO], [GIVEN], [WHEN], [THEN], [AND]
- Každý scénář začíná [SCENARIO] s výstižným názvem
- [GIVEN] = výchozí stav / předpoklad
- [WHEN] = akce uživatele nebo systému
- [THEN] = očekávaný výsledek
- [AND] = dodatečný výsledek nebo podmínka (volitelné rozšíření GIVEN/THEN)
- Pokryj happy path i důležité edge cases a negativní scénáře
- Buď konkrétní, vyhni se vágním formulacím
- Nepřidávej žádný úvod ani závěr — pouze samotná AK

Příklad formátu:
[SCENARIO] Úspěšné přihlášení uživatele
[GIVEN] Uživatel je na přihlašovací stránce
[WHEN] Zadá správné přihlašovací údaje a klikne na Přihlásit
[THEN] Je přesměrován na hlavní stránku aplikace
[AND] V pravém horním rohu se zobrazí jeho jméno

[SCENARIO] Přihlášení se špatnými údaji
[GIVEN] Uživatel je na přihlašovací stránce
[WHEN] Zadá nesprávné heslo a klikne na Přihlásit
[THEN] Zobrazí se chybová hláška "Nesprávné přihlašovací údaje"
[AND] Uživatel zůstane na přihlašovací stránce"""


def build_user_prompt(summary: str, description: str, issue_key: str) -> str:
    desc_text = description or "Popis není k dispozici."
    return f"""Vygeneruj akceptační kritéria pro tento JIRA ticket:

Ticket: {issue_key}
Název: {summary}

Popis:
{desc_text}"""


def extract_text_from_adf(adf: dict | str | None) -> str:
    """
    Převede Atlassian Document Format (ADF) na prostý text.
    JIRA API vrací description jako ADF objekt, ne plain text.
    """
    if not adf:
        return ""
    if isinstance(adf, str):
        return adf

    texts = []

    def walk(node: dict) -> None:
        if node.get("type") == "text":
            texts.append(node.get("text", ""))
        for child in node.get("content", []):
            walk(child)

    walk(adf)
    return "\n".join(t for t in texts if t.strip())


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request):
    """
    Přijme webhook z JIRA Automation při změně stavu ticketu.
    Vygeneruje AK přes Claude a zapíše je zpět jako komentář.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Neplatný JSON payload")

    # Vytáhni data z JIRA webhook payloadu
    issue     = payload.get("issue", {})
    fields    = issue.get("fields", {})
    issue_key = issue.get("key", "")
    summary   = fields.get("summary", "")
    desc_adf  = fields.get("description")

    print(f"[Webhook] Přijat ticket: {issue_key} | {summary}")

    if not issue_key:
        raise HTTPException(400, "Chybí issue key v payloadu")

    # Pokud webhook neobsahuje description, stáhni ticket přímo z JIRA API
    if not summary:
        print(f"[JIRA] Stahuji detail ticketu {issue_key}...")
        issue_data = await get_jira_issue(issue_key)
        fields     = issue_data.get("fields", {})
        summary    = fields.get("summary", "")
        desc_adf   = fields.get("description")

    description = extract_text_from_adf(desc_adf)
    print(f"[AC] Generuji AK pro {issue_key}...")

    # Zavolej Claude
    ac_text = await call_claude(
        system_prompt=build_system_prompt(),
        user_prompt=build_user_prompt(summary, description, issue_key),
    )

    print(f"[AC] Vygenerováno {len(ac_text)} znaků")

    # Zapiš AK do custom fieldu customfield_10207
    await update_ac_field(issue_key, ac_text)

    return JSONResponse({
        "status": "ok",
        "issue_key": issue_key,
        "ac_length": len(ac_text),
    })


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "anthropic": "ok" if ANTHROPIC_API_KEY else "⚠️ missing ANTHROPIC_API_KEY",
        "jira": "ok" if all([JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN]) else "⚠️ missing JIRA config",
    }
