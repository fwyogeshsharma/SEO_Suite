# SEO Suite

A self-hosted SEO crawler + audit platform: technical SEO, AI-search (GEO), duplicates & similarity, structured data, accessibility,
mobile, hreflang, sitemaps, integrations (Search Console, GA4, PageSpeed), scheduling, comparison, exports, CLI and an MCP server.

## Run
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8080       # open http://localhost:8080
    # optional, for JavaScript rendering / mobile emulation / axe-core:
    pip install playwright && playwright install chromium

Everything below works with no API keys unless noted.

## Feature map
| Area | Where | Notes |
|---|---|---|
| Broken links, errors, redirects | **Response & Links** | 4xx/5xx/timeouts with every source page + anchor; redirect chains, loops, temp redirects, redirects-to-error; broken images/CSS/JS; external link checks |
| Titles & meta | **Titles & Meta** | length, pixel width, duplicates, missing, multiples, H1/H2 analysis, URL quality |
| Robots & directives | **Directives** | meta robots, X-Robots-Tag, indexability + reason, canonicals (missing/self/other/bad target/conflicts), pagination, robots.txt blocks |
| hreflang | **Hreflang** | missing return links, invalid codes, self-reference, x-default, non-200/noindex targets, canonical conflicts (HTML, HTTP header, sitemap) |
| Exact duplicates | **Duplicates → Exact** | content hash + HTML hash groups |
| Near duplicates | **Duplicates → Near** | MinHash similarity, adjustable threshold |
| Semantic similarity & clustering | **Duplicates → Semantic / Clusters** | free local TF-IDF; **AI → embeddings** for real semantic vectors (OpenAI/Gemini/Ollama); low-relevance outliers |
| XML sitemaps | **Sitemaps** | generate from crawl (indexable 200s, hreflang, images, priority, 50k split + index); audit existing sitemaps vs crawl |
| Site visualisations | **Visualise** | force-directed link graph, directory tree, crawl-depth chart; link score (internal PageRank) in Pages |
| Crawl limit & configuration | **⚙ Crawl settings** | up to 100,000 pages, depth, concurrency, delay, user-agent, robots, nofollow, include/exclude regex, subdomains, sitemap seeding |
| Save & open crawls | **Crawls** | every audit is saved to SQLite and can be re-opened |
| Crawl comparison | **Crawls → Compare** | new/removed URLs, changed fields, issue deltas with newly affected / fixed URLs |
| Scheduling | **Crawls → Schedules** | hourly/daily/weekly/monthly or N minutes; in-app or `python -m app.cli run-due` from cron / Task Scheduler |
| Password-protected sites | ⚙ → Password-protected | Basic/Digest auth, headers, cookies, form login (with CSRF hidden fields) |
| JavaScript rendering | ⚙ → Rendering | headless Chromium; reports content/links that only exist after JS |
| Custom JavaScript | ⚙ → Custom JavaScript | snippet run on each rendered page, results in **Custom** |
| Custom search & extraction | ⚙ → Custom search/extraction | contains / not-contains / regex; CSS, XPath, regex extraction |
| Mobile usability | **Mobile & A11y** | viewport/zoom/fixed width/plugins (static) + overflow, tap-target size, small text (rendered) |
| Accessibility | **Mobile & A11y** | WCAG-style static checks + full axe-core audit when rendering |
| Structured data & validation | **Structured Data** | JSON-LD + Microdata + RDFa types, required/recommended properties for 25+ schema types |
| Spelling & grammar | **Content** | pyspellchecker (11 languages) + grammar rules; optional LanguageTool server (`LANGUAGETOOL_URL`) |
| Segmentation | **Segments** | rules on any field (folder, status, words, schema, `issue:<id>`…); per-segment health table |
| Google Analytics | **Integrations** | GA4 Data API + merged with crawl (traffic on non-indexable/orphan pages, uncrawled URLs) |
| Search Console | **Integrations** | queries, pages, URL Inspection (index status) |
| PageSpeed Insights | **Integrations** | Lighthouse scores, lab + field Core Web Vitals, opportunities; batch top-N pages |
| AI integration | **AI**, **Settings** | Anthropic, OpenAI, Gemini, Ollama; run any prompt over pages; AI fixes/briefs/plans |
| Looker / Data Studio report | **Export** | always-latest CSV feeds for `=IMPORTDATA()` → Looker Studio, plus printable HTML report |
| Email notifications & exports | **Export**, schedules | SMTP digest with changes vs previous crawl + XLSX/CSV/JSON attachments |
| Command-line automation | `python -m app.cli` | crawl/export/compare/sitemap/schedule/run-due; `--fail-on critical` exit code for CI |
| MCP server | `python -m app.cli mcp` | tools: crawl_site, get_issues, get_report, compare_crawls, generate_sitemap, pagespeed, traffic_stats |
| First-party traffic analytics | **Traffic** | pin a site, paste `<script src=".../t.js">`; cookie-less, bots ignored |

## Configuration
Open **Settings** to enter keys for this session (memory only), or use environment variables:

    ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / OLLAMA_MODEL (+ OLLAMA_URL)    SEO_AI_PROVIDER, SEO_AI_MODEL
    GSC_ACCESS_TOKEN | GA_ACCESS_TOKEN + GA_PROPERTY_ID   (or GOOGLE_CLIENT_ID/SECRET + GOOGLE_REFRESH_TOKEN for auto-refresh)
    PSI_API_KEY (optional, raises PageSpeed quota)    DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD
    SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_FROM        LANGUAGETOOL_URL        SEO_DB (database path)

## Command line
    python -m app.cli crawl https://example.com --max-pages 500 --export-dir out --formats xlsx,csv --compare-last --fail-on critical
    python -m app.cli crawl https://staging.example.com --basic-auth user:pass --js --mobile
    python -m app.cli list | export 3 --dir out --formats xlsx | compare 2 3 | sitemap 3 --out sitemap.xml
    python -m app.cli schedule add https://example.com --every weekly --email me@x.com --exports xlsx
    python -m app.cli run-due
    claude mcp add seo-suite -- python -m app.cli mcp      # run from this folder

## Tests
    python -m pytest tests -q         # crawls a built-in fixture site with planted SEO problems
    python tests/fixture_server.py    # serve that site at http://127.0.0.1:8765 to try the UI

## Limits / honest notes
- Static crawl by default; enable JavaScript rendering for JS sites (slower, needs Playwright + Chromium).
- Speed check is server response time; use PageSpeed Insights for Core Web Vitals.
- Spelling/grammar and accessibility checks are heuristics — expect some false positives (brand names, jargon). Static accessibility
  can't check colour contrast; enable axe-core for that.
- Structured-data validation follows Google's documented required/recommended properties; confirm important pages in Google's Rich Results Test.
- Semantic clusters from TF-IDF are keyword-based; use embeddings (AI tab) for true semantic grouping.
- Crawl responsibly; only crawl sites you own or have permission to crawl.
- The server has no login — run it locally or behind your own authentication. `/api/settings` can change credentials for the running process.
