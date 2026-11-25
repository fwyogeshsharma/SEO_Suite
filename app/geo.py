"""AI SEO / GEO (Generative Engine Optimization): readiness for ChatGPT, Perplexity, Claude, Gemini, AI Overviews."""
from __future__ import annotations

from .audit import Issue
from .crawler import SiteData

AI_BOTS = {
    "GPTBot": "OpenAI training", "OAI-SearchBot": "ChatGPT search", "ChatGPT-User": "ChatGPT browsing",
    "ClaudeBot": "Anthropic training", "Claude-SearchBot": "Claude search", "Claude-User": "Claude browsing",
    "PerplexityBot": "Perplexity", "Google-Extended": "Gemini training", "Applebot-Extended": "Apple AI",
    "CCBot": "Common Crawl", "Bytespider": "ByteDance",
}
SEARCH_BOTS = {"OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot", "Claude-User", "PerplexityBot"}


def parse_robots_for_bots(robots: str | None) -> dict[str, str]:
    """Return {bot: 'allowed'|'blocked'} using simple group parsing."""
    result = {b: "allowed" for b in AI_BOTS}
    if not robots:
        return result
    groups: list[tuple[list[str], list[str]]] = []
    agents: list[str] = []
    rules: list[str] = []
    last_was_agent = False
    for raw in robots.splitlines():
        line = raw.split("#")[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_was_agent and agents:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(v.lower())
            last_was_agent = True
        else:
            last_was_agent = False
            if k == "disallow":
                rules.append(v)
    if agents:
        groups.append((agents, rules))
    for bot in AI_BOTS:
        specific = [r for a, r in groups if bot.lower() in a]
        wildcard = [r for a, r in groups if "*" in a]
        chosen = specific or wildcard
        if any("/" == d for r in chosen for d in r):
            result[bot] = "blocked"
    return result


def run_geo_audit(site: SiteData) -> tuple[list[Issue], dict]:
    issues: list[Issue] = []
    pages = [p for p in site.pages.values() if p.is_html and p.status == 200]
    n = max(len(pages), 1)
    checks: list[dict] = []

    def check(name: str, passed: bool, pts: int, detail: str):
        checks.append({"name": name, "passed": passed, "points": pts if passed else 0, "max": pts, "detail": detail})

    # 1. AI crawler access
    bots = parse_robots_for_bots(site.robots_txt)
    blocked_search = [b for b in SEARCH_BOTS if bots[b] == "blocked"]
    check("AI search bots can crawl you", not blocked_search, 20,
          "Blocked: " + ", ".join(blocked_search) if blocked_search else "ChatGPT, Claude and Perplexity search bots are allowed")
    if blocked_search:
        issues.append(Issue("geo_bots_blocked", "critical", "geo",
                            "robots.txt blocks AI search crawlers — you can't be cited in their answers",
                            [site.base + "/robots.txt"],
                            "Allow " + ", ".join(blocked_search) + ". You can still block training-only bots (GPTBot, Google-Extended, CCBot) if desired."))

    # 2. llms.txt
    has_llms = bool(site.llms_txt and len(site.llms_txt.strip()) > 20)
    check("llms.txt present", has_llms, 10, "Found /llms.txt" if has_llms else "No /llms.txt (an emerging, optional convention — low cost, uncertain impact)")
    if not has_llms:
        issues.append(Issue("geo_no_llms", "info", "geo", "No /llms.txt file", [site.base + "/llms.txt"],
                            "Add /llms.txt: a markdown summary of your site and links to key pages. Treat as low-priority; major engines haven't confirmed using it."))

    # 3. Structured data coverage
    def pct(f): return sum(1 for p in pages if f(p)) / n
    org = any(t in ("Organization", "LocalBusiness", "Corporation") for p in pages for t in p.schema_types)
    check("Organization/entity schema", org, 15, "Organization schema found" if org else "No Organization schema — AI engines can't confidently identify your entity")
    if not org:
        issues.append(Issue("geo_no_org", "warning", "geo", "No Organization / LocalBusiness schema", [site.base],
                            "Add Organization JSON-LD with name, url, logo and sameAs links (LinkedIn, X, Wikipedia, etc.)."))
    faq_pct = pct(lambda p: "FAQPage" in p.schema_types)
    check("FAQPage schema", faq_pct > 0, 5, f"{round(faq_pct*100)}% of pages")

    # 4. Answer-ready content
    q_pct = pct(lambda p: p.question_headings > 0)
    check("Question-style headings (answer-ready)", q_pct >= 0.2, 15, f"{round(q_pct*100)}% of pages have question headings")
    low_q = [p.url for p in pages if p.question_headings == 0 and p.word_count >= 300]
    if q_pct < 0.2 and low_q:
        issues.append(Issue("geo_no_questions", "warning", "geo", "Few pages answer questions directly", low_q,
                            "Add H2/H3 phrased as user questions followed by a concise 40–60 word direct answer, then detail."))
    struct_pct = pct(lambda p: p.lists_and_tables > 0)
    check("Lists / tables (extractable structure)", struct_pct >= 0.3, 10, f"{round(struct_pct*100)}% of pages")

    # 5. E-E-A-T signals
    auth_pct = pct(lambda p: p.has_author)
    check("Author / expertise signals", auth_pct >= 0.3, 10, f"{round(auth_pct*100)}% of pages show author")
    if auth_pct < 0.3:
        issues.append(Issue("geo_no_author", "info", "geo", "Weak authorship signals (E-E-A-T)",
                            [p.url for p in pages if not p.has_author],
                            "Add bylines linking to author pages with credentials; use Person schema."))
    date_pct = pct(lambda p: p.has_date)
    check("Freshness signals (dates)", date_pct >= 0.3, 5, f"{round(date_pct*100)}% of pages show dates")

    # 6. Citations / outbound authority
    cite_pct = pct(lambda p: len(p.external_links) > 0)
    check("Cites external sources", cite_pct >= 0.3, 5, f"{round(cite_pct*100)}% of pages link out")

    # 7. Depth
    deep_pct = pct(lambda p: p.word_count >= 600)
    check("Substantive content (≥600 words)", deep_pct >= 0.3, 5, f"{round(deep_pct*100)}% of pages")

    total = sum(c["points"] for c in checks)
    maximum = sum(c["max"] for c in checks)
    summary = {"score": round(100 * total / maximum), "checks": checks, "ai_bots": [
        {"bot": b, "purpose": AI_BOTS[b], "status": s} for b, s in bots.items()]}
    return issues, summary
