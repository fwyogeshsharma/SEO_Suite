"""Structured data extraction + validation against Google rich-result requirements (JSON-LD and Microdata)."""
from __future__ import annotations

# type -> (required, recommended). Based on Google Search Central rich result documentation.
RULES: dict[str, tuple[list[str], list[str]]] = {
    "Article": (["headline", "image"], ["author", "datePublished", "dateModified", "publisher"]),
    "NewsArticle": (["headline", "image"], ["author", "datePublished", "dateModified", "publisher"]),
    "BlogPosting": (["headline", "image"], ["author", "datePublished", "dateModified", "publisher"]),
    "Product": (["name", "image"], ["description", "offers", "aggregateRating", "review", "sku", "brand"]),
    "Offer": (["price", "priceCurrency"], ["availability", "url", "priceValidUntil"]),
    "AggregateRating": (["ratingValue"], ["reviewCount", "ratingCount", "bestRating"]),
    "Review": (["author", "reviewRating"], ["datePublished", "itemReviewed"]),
    "FAQPage": (["mainEntity"], []),
    "Question": (["name", "acceptedAnswer"], []),
    "Answer": (["text"], []),
    "HowTo": (["name", "step"], ["image", "totalTime", "supply", "tool"]),
    "Recipe": (["name", "image"], ["author", "datePublished", "description", "prepTime", "cookTime", "recipeIngredient",
                                    "recipeInstructions", "aggregateRating", "nutrition"]),
    "BreadcrumbList": (["itemListElement"], []),
    "ListItem": (["position"], ["name", "item"]),
    "Organization": (["name"], ["url", "logo", "sameAs", "contactPoint"]),
    "LocalBusiness": (["name", "address"], ["telephone", "openingHours", "openingHoursSpecification", "geo", "image", "priceRange", "url"]),
    "WebSite": (["name", "url"], ["potentialAction"]),
    "WebPage": ([], ["name", "description"]),
    "Person": (["name"], ["url", "sameAs", "jobTitle"]),
    "Event": (["name", "startDate", "location"], ["endDate", "image", "description", "offers", "performer", "eventStatus"]),
    "VideoObject": (["name", "thumbnailUrl", "uploadDate"], ["description", "contentUrl", "embedUrl", "duration"]),
    "JobPosting": (["title", "description", "datePosted", "hiringOrganization", "jobLocation"], ["validThrough", "baseSalary", "employmentType"]),
    "Course": (["name", "description"], ["provider"]),
    "SoftwareApplication": (["name", "offers"], ["aggregateRating", "operatingSystem", "applicationCategory"]),
    "ImageObject": (["contentUrl"], ["creator", "license", "acquireLicensePage"]),
    "PostalAddress": ([], ["streetAddress", "addressLocality", "postalCode", "addressCountry"]),
}
RICH_RESULT = {"Article", "NewsArticle", "BlogPosting", "Product", "FAQPage", "HowTo", "Recipe", "BreadcrumbList", "Event",
               "VideoObject", "JobPosting", "LocalBusiness", "Organization", "Course", "SoftwareApplication", "Review"}


def _walk(node, out: list[dict]) -> None:
    if isinstance(node, list):
        for n in node:
            _walk(n, out)
    elif isinstance(node, dict):
        if "@graph" in node:
            _walk(node["@graph"], out)
        t = node.get("@type")
        if t:
            out.append(node)
        for k, v in node.items():
            if k != "@graph" and isinstance(v, (dict, list)):
                _walk(v, out)


def _present(node: dict, prop: str) -> bool:
    v = node.get(prop)
    return v not in (None, "", [], {})


def validate_page(page) -> list[dict]:
    """Return [{format, type, errors:[], warnings:[], props:int}] for one Page."""
    results: list[dict] = []
    nodes: list[dict] = []
    for block in page.json_ld:
        _walk(block, nodes)
    for node in nodes:
        types = node["@type"] if isinstance(node["@type"], list) else [node["@type"]]
        for t in types:
            req, rec = RULES.get(t, ([], []))
            errs = [f"Missing required property: {p}" for p in req if not _present(node, p)]
            warns = [f"Missing recommended property: {p}" for p in rec if not _present(node, p)]
            if "@context" not in node and node is nodes[0] and not any("@context" in b for b in page.json_ld if isinstance(b, dict)):
                errs.append("Missing @context")
            if t not in RULES:
                warns = ["Type not validated by this tool (check with Google's Rich Results Test)"]
            results.append({"format": "JSON-LD", "type": t, "errors": errs, "warnings": warns, "rich": t in RICH_RESULT})
    for md in page.microdata:
        t = md["type"]
        req, rec = RULES.get(t, ([], []))
        props = set(md["props"])
        results.append({"format": "Microdata", "type": t,
                        "errors": [f"Missing required property: {p}" for p in req if p not in props],
                        "warnings": [f"Missing recommended property: {p}" for p in rec if p not in props],
                        "rich": t in RICH_RESULT})
    if "__invalid_json_ld__" in page.schema_types:
        results.append({"format": "JSON-LD", "type": "(invalid)", "errors": ["JSON-LD block is not valid JSON"], "warnings": [], "rich": False})
    return results
