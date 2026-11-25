"""AI-assisted fixes. Providers: Anthropic, OpenAI, Gemini, Ollama (local). Degrades to rule-based output if none is configured.

Env: SEO_AI_PROVIDER=anthropic|openai|gemini|ollama (auto-detected from keys if unset), SEO_AI_MODEL (override),
ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, OLLAMA_URL (default http://localhost:11434), OLLAMA_MODEL.
"""
from __future__ import annotations

import json
import os
import re

import httpx

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4o-mini", "gemini": "gemini-2.0-flash", "ollama": "llama3.1"}
EMBED_MODELS = {"openai": "text-embedding-3-small", "gemini": "gemini-embedding-001", "ollama": "nomic-embed-text"}


def provider() -> str:
    p = os.getenv("SEO_AI_PROVIDER", "").lower()
    if p in DEFAULT_MODELS:
        return p
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("OLLAMA_MODEL"):
        return "ollama"
    return ""


def model() -> str:
    p = provider()
    if p == "ollama":
        return os.getenv("SEO_AI_MODEL") or os.getenv("OLLAMA_MODEL") or DEFAULT_MODELS["ollama"]
    return os.getenv("SEO_AI_MODEL") or DEFAULT_MODELS.get(p, "")


def ai_available() -> bool:
    p = provider()
    keys = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
    return bool(p) and (p == "ollama" or bool(os.getenv(keys[p])))


def _ollama_url() -> str:
    return os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def _ask(system: str, user: str, max_tokens: int = 1500) -> str:
    p, m = provider(), model()
    if p == "anthropic":
        import anthropic
        msg = anthropic.Anthropic().messages.create(model=m, max_tokens=max_tokens, system=system,
                                                    messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    if p == "openai":
        r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=120,
                       headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                       json={"model": m, "max_tokens": max_tokens,
                             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    if p == "gemini":
        r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", timeout=120,
                       headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                       json={"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                             "generationConfig": {"maxOutputTokens": max_tokens}})
        r.raise_for_status()
        return "".join(x.get("text", "") for x in r.json()["candidates"][0]["content"]["parts"])
    if p == "ollama":
        r = httpx.post(_ollama_url() + "/api/chat", timeout=300,
                       json={"model": m, "stream": False, "options": {"num_predict": max_tokens},
                             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["message"]["content"]
    raise RuntimeError("No AI provider configured")


def embed(texts: list[str]) -> list[list[float]] | None:
    """Dense embeddings from OpenAI / Gemini / Ollama. Returns None if the active provider has no embedding API (Anthropic)."""
    p = provider()
    texts = [t[:6000] for t in texts]
    if p == "openai":
        out: list[list[float]] = []
        for i in range(0, len(texts), 96):
            r = httpx.post("https://api.openai.com/v1/embeddings", timeout=120,
                           headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                           json={"model": EMBED_MODELS["openai"], "input": texts[i:i + 96]})
            r.raise_for_status()
            out += [d["embedding"] for d in r.json()["data"]]
        return out
    if p == "gemini":
        out = []
        for i in range(0, len(texts), 100):
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODELS['gemini']}:batchEmbedContents", timeout=120,
                           headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                           json={"requests": [{"model": f"models/{EMBED_MODELS['gemini']}", "content": {"parts": [{"text": t}]}}
                                              for t in texts[i:i + 100]]})
            r.raise_for_status()
            out += [e["values"] for e in r.json()["embeddings"]]
        return out
    if p == "ollama":
        out = []
        for t in texts:
            r = httpx.post(_ollama_url() + "/api/embeddings", timeout=120,
                           json={"model": os.getenv("OLLAMA_EMBED_MODEL", EMBED_MODELS["ollama"]), "prompt": t})
            r.raise_for_status()
            out.append(r.json()["embedding"])
        return out
    return None


def info() -> dict:
    return {"provider": provider(), "model": model(), "available": ai_available(), "embeddings": provider() in EMBED_MODELS}


def run_prompt(prompt: str, variables: dict[str, str], max_tokens: int = 600) -> str:
    """Run a user prompt template over one page's fields: placeholders like {url} {title} {h1} {text}."""
    filled = prompt
    for k, v in variables.items():
        filled = filled.replace("{" + k + "}", str(v))
    return _ask("You are an SEO analyst. Answer concisely and only from the provided page data.", filled, max_tokens)


def _json(text: str):
    m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    return json.loads(m.group(1)) if m else None


def fix_page_meta(url: str, title: str, description: str, h1: list[str], text: str, keywords: list[str]) -> dict:
    """Rewrite title + meta description."""
    if not ai_available():
        base = (h1[0] if h1 else title) or url
        return {"ai": False, "title": base[:58], "meta_description": (text[:155].rsplit(" ", 1)[0] + "…") if text else "",
                "note": "Configure an AI provider for AI-written suggestions; this is a rule-based fallback."}
    out = _ask(
        "You are a senior SEO copywriter. Return ONLY JSON: {\"title\": str (<=60 chars), \"meta_description\": str (<=155 chars, includes a benefit and CTA)}. "
        "Match the page's real content; never invent claims.",
        f"URL: {url}\nCurrent title: {title}\nCurrent description: {description}\nH1: {h1}\n"
        f"Target keywords: {keywords}\nPage text (excerpt): {text[:2500]}")
    d = _json(out) or {}
    d["ai"] = True
    return d


def alt_text_suggestions(url: str, text: str, n: int = 1) -> dict:
    if not ai_available():
        return {"ai": False, "note": "Configure an AI provider. Write alt text that describes the image in <125 chars."}
    return {"ai": True, "guidance": _ask("You are an accessibility + SEO expert.", f"Give a short guideline with 3 examples of good alt text for images on this page:\n{url}\n{text[:1500]}", 400)}


def faq_schema(url: str, text: str) -> dict:
    """Generate FAQPage JSON-LD grounded in the page text."""
    if not ai_available():
        return {"ai": False, "note": "Configure an AI provider to generate FAQ schema from page content."}
    out = _ask(
        "You write FAQPage JSON-LD. Use ONLY facts present in the page text. Return ONLY valid JSON-LD (an object with @context and @type FAQPage), 4-6 Q&As, answers 40-60 words.",
        f"URL: {url}\nPage text:\n{text[:5000]}", 2000)
    return {"ai": True, "json_ld": _json(out)}


def content_brief(keyword: str, competitors_notes: str = "") -> dict:
    if not ai_available():
        return {"ai": False, "brief": f"# Content brief: {keyword}\n\n- Define search intent\n- Outline H2s as questions\n- Add FAQ, examples, sources\n\n(Configure an AI provider for a full AI brief.)"}
    return {"ai": True, "brief": _ask(
        "You are an SEO strategist. Produce a markdown content brief: search intent, audience, working title, meta description, "
        "H1/H2/H3 outline (question-style H2s for AI answer engines), entities/terms to cover, FAQ list, internal link ideas, "
        "E-E-A-T suggestions. Be concrete.", f"Primary keyword: {keyword}\n{competitors_notes}", 1800)}


def llms_txt(site_name: str, base: str, pages: list[dict]) -> str:
    """Generate /llms.txt from crawled pages (rule-based, no AI needed)."""
    lines = [f"# {site_name}", "", f"> Key pages of {base}", "", "## Pages", ""]
    for p in pages[:40]:
        desc = f": {p['description']}" if p.get("description") else ""
        lines.append(f"- [{p['title'] or p['url']}]({p['url']}){desc}")
    return "\n".join(lines) + "\n"


def prioritize(issues: list[dict], site_url: str) -> dict:
    """Ask the model for a prioritized action plan from audit output."""
    if not ai_available():
        order = {"critical": 0, "warning": 1, "info": 2}
        top = sorted(issues, key=lambda i: (order[i["severity"]], -i["count"]))[:10]
        return {"ai": False, "plan": "\n".join(f"{n+1}. [{i['severity']}] {i['message']} ({i['count']} URLs) — {i['fix_hint']}" for n, i in enumerate(top))}
    compact = [{k: i[k] for k in ("severity", "category", "message", "count", "fix_hint")} for i in issues]
    return {"ai": True, "plan": _ask(
        "You are a technical SEO lead. Turn this audit into a prioritized 30-day action plan (markdown). "
        "Order by impact/effort. Group into Week 1–4. Include the expected benefit of each item. Flag AI-search (GEO) items separately.",
        f"Site: {site_url}\nIssues JSON: {json.dumps(compact)[:12000]}", 1800)}
