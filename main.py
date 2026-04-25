# -*- coding: utf-8 -*-
"""
JIRA -> Claude Acceptance Criteria Bot
Nasazeni: Railway
Pozadavky: fastapi, httpx, uvicorn
"""

from __future__ import annotations

import os
import json
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
JIRA_BASE_URL     = os.environ.get('JIRA_BASE_URL', '').rstrip('/')
JIRA_EMAIL        = os.environ.get('JIRA_EMAIL', '')
JIRA_API_TOKEN    = os.environ.get('JIRA_API_TOKEN', '')

SYSTEM_PROMPT = (
    'You are an experienced QA analyst. Generate acceptance criteria for JIRA tickets. '
    'Write in Czech language. '
    'Use only these keywords in square brackets: [SCENARIO], [GIVEN], [WHEN], [THEN], [AND]. '
    'Each scenario starts with [SCENARIO] followed by a descriptive name. '
    '[GIVEN] = initial state, [WHEN] = action, [THEN] = expected result, [AND] = additional result. '
    'Cover happy path and edge cases. Be specific. No intro or conclusion, only the AC.'
)

def jira_auth():
    return (JIRA_EMAIL, JIRA_API_TOKEN)


async def get_jira_issue(issue_key: str) -> dict:
    url = f'{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}'
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, auth=jira_auth(), timeout=15)
        resp.raise_for_status()
        return resp.json()


def extract_comments(issue_data: dict) -> str:
    comments = issue_data.get('fields', {}).get('comment', {}).get('comments', [])
    if not comments:
        return ''
    lines = []
    for c in comments:
        author = c.get('author', {}).get('displayName', 'Neznamy')
        body = extract_text_from_adf(c.get('body'))
        if body.strip():
            lines.append(f'[{author}]: {body.strip()}')
    return '\n'.join(lines)


async def update_ac_field(issue_key: str, ac_text: str) -> None:
    url = f'{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}'
    payload = {
        'fields': {
            'customfield_10207': {
                'type': 'doc',
                'version': 1,
                'content': [
                    {
                        'type': 'codeBlock',
                        'content': [{'type': 'text', 'text': ac_text}]
                    }
                ]
            }
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.put(url, json=payload, auth=jira_auth(), timeout=15)
        resp.raise_for_status()
        print(f'[JIRA] AK zapisana do customfield_10207 na ticketu {issue_key}')


async def call_claude(system_prompt: str, user_prompt: str) -> str:
    headers = {
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }
    payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 2048,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_prompt}],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            'https://api.anthropic.com/v1/messages',
            headers=headers,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()['content'][0]['text']


def build_user_prompt(summary: str, description: str, issue_key: str, comments: str = '') -> str:
    desc_text = description or 'Popis neni k dispozici.'
    prompt = f'Vygeneruj akceptacni kriteria pro tento JIRA ticket:\n\nTicket: {issue_key}\nNazev: {summary}\n\nPopis:\n{desc_text}'
    if comments:
        prompt += f'\n\nKomentare z ticketu (obsahuji upresnovani a diskuze):\n{comments}'
    return prompt


def extract_text_from_adf(adf) -> str:
    if not adf:
        return ''
    if isinstance(adf, str):
        return adf
    texts = []
    def walk(node):
        if node.get('type') == 'text':
            texts.append(node.get('text', ''))
        for child in node.get('content', []):
            walk(child)
    walk(adf)
    return '\n'.join(t for t in texts if t.strip())


@app.post('/webhook')
async def webhook(request: Request):
    try:
        body = await request.body()
        print(f'[DEBUG] Raw body: {body[:1000]}')
        payload = json.loads(body)
    except Exception as e:
        print(f'[DEBUG] Parse error: {e}')
        raise HTTPException(400, 'Neplatny JSON payload')

    issue     = payload.get('issue', {})
    fields    = issue.get('fields', {})
    issue_key = issue.get('key', '')
    summary   = fields.get('summary', '')

    print(f'[Webhook] Prijat ticket: {issue_key} | {summary}')

    if not issue_key:
        issue_key = payload.get('issueKey', '') or payload.get('key', '')
        if not issue_key:
            print(f'[DEBUG] Cely payload: {json.dumps(payload, indent=2)[:2000]}')
            raise HTTPException(400, 'Chybi issue key v payloadu')

    print(f'[JIRA] Stahuji detail ticketu {issue_key}...')
    issue_data = await get_jira_issue(issue_key)
    fields     = issue_data.get('fields', {})
    summary    = fields.get('summary', '')
    desc_adf   = fields.get('description')

    description = extract_text_from_adf(desc_adf)
    comments = extract_comments(issue_data)
    print(f'[AC] Generuji AK pro {issue_key} | summary: {summary[:50]} | komentaru: {len(issue_data.get("fields", {}).get("comment", {}).get("comments", []))}')

    ac_text = await call_claude(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(summary, description, issue_key, comments),
    )

    print(f'[AC] Vygenerovano {len(ac_text)} znaku')

    await update_ac_field(issue_key, ac_text)

    return JSONResponse({
        'status': 'ok',
        'issue_key': issue_key,
        'ac_length': len(ac_text),
    })


@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'anthropic': 'ok' if ANTHROPIC_API_KEY else 'missing ANTHROPIC_API_KEY',
        'jira': 'ok' if all([JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN]) else 'missing JIRA config',
    }
