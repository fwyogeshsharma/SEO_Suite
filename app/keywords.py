"""Keyword extraction from crawled content + free keyword ideas via Google Autocomplete."""
from __future__ import annotations

import math
import re
from collections import Counter

import httpx

from .crawler import SiteData

STOP = set("""a an and are as at be but by for from has have i if in into is it its of on or our so that the their
then there these they this to was we were will with you your not can all more about out up what which who how
when why do does did than also just any other some such only own same too very s t""".split())


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z'-]{2,}", text.lower()) if w not in STOP]


def extract_keywords(site: SiteData, top: int = 30) -> list[dict]:
    """TF-IDF over unigrams+bigrams across pages; boosts terms in title/H1."""
    docs = {u: p for u, p in site.pages.items() if p.is_html and p.status == 200 and p.word_count > 30}
    if not docs:
        return []
    df: Counter = Counter()
    tf_total: Counter = Counter()
    boost: Counter = Counter()
    for p in docs.values():
        toks = _tokens(p.text)
        grams = toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
        c = Counter(grams)
        tf_total.update(c)
        df.update(c.keys())
        for t in _tokens(p.title + " " + " ".join(p.h1)):
            boost[t] += 1
    n = len(docs)
    scored = []
    for term, tf in tf_total.items():
        if tf < 2:
            continue
        idf = math.log((1 + n) / (1 + df[term])) + 1
        b = 1 + 0.5 * sum(boost[w] for w in term.split())
        bigram_bonus = 1.4 if " " in term else 1.0
        scored.append((tf * idf * b * bigram_bonus, term, tf, df[term]))
    scored.sort(reverse=True)
    out, used = [], set()
    for s, term, tf, d in scored:
        if any(term in u or u in term for u in used):
            continue
        used.add(term)
        out.append({"keyword": term, "occurrences": tf, "pages": d, "score": round(s, 1)})
        if len(out) >= top:
            break
    return out


async def suggest_keywords(seed: str, lang: str = "en", country: str = "us") -> list[str]:
    """Google Autocomplete expansions (seed + a–z and question prefixes). No API key required."""
    prefixes = ["", "how to ", "what is ", "best ", "why ", "vs ", "for ", "near me"]
    queries = [seed + " " + c for c in "abcdefghijklmnopqrstuvwxyz"][:10] + [p + seed for p in prefixes]
    found: dict[str, None] = {}
    async with httpx.AsyncClient(timeout=8) as client:
        for q in queries:
            try:
                r = await client.get("https://suggestqueries.google.com/complete/search",
                                     params={"client": "firefox", "q": q, "hl": lang, "gl": country})
                if r.status_code == 200:
                    for s in r.json()[1]:
                        found[s] = None
            except Exception:
                continue
    return list(found)[:80]


def content_gap(site_keywords: list[dict], ideas: list[str]) -> list[str]:
    """Ideas whose words don't appear in the site's top keywords → candidate new content."""
    have = " ".join(k["keyword"] for k in site_keywords).split()
    have_set = set(have)
    gaps = []
    for idea in ideas:
        words = [w for w in _tokens(idea)]
        if words and sum(1 for w in words if w not in have_set) >= max(1, len(words) // 2):
            gaps.append(idea)
    return gaps[:40]
