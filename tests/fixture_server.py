"""Tiny site with deliberate SEO problems for testing. Run: python tests/fixture_server.py [port]"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOREM = ("Our team builds reliable widgets for small businesses and explains every step clearly. " * 12)
NEAR = LOREM + "Contact us today for a free quote on custom orders."
NAV = '<nav><a href="/">Home</a> <a href="/about">About</a> <a href="/faq">FAQ</a> <a href="/dup-a">Dup A</a> <a href="/dup-b">Dup B</a> <a href="/near-a">Near A</a> <a href="/near-b">Near B</a> <a href="/old">Old</a> <a href="/chain1">Chain</a> <a href="/loop1">Loop</a> <a href="/broken">Broken</a> <a href="/noindex">Hidden</a> <a href="/en/">EN</a> <a href="/es/">ES</a> <a href="/structured">Structured</a> <a href="/a11y">A11y</a> <a href="/private/x">Private</a> <a href="/js">JS</a> <a href="https://no-such-host.invalid/x">dead ext</a> <a href="/Weird_URL/Page">weird</a></nav>'


def page(title, body, head="", lang="en"):
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{head}</head><body><main>{NAV}{body}</main></body></html>')


ROUTES = {
    "/": lambda: page("Widget Shop — reliable widgets for small business owners", f"<h1>Widget Shop</h1><p>{LOREM}</p>"
                      '<img src="/img.png"><img src="/missing.png" alt="gone">',
                      '<meta name="description" content="Reliable widgets for small businesses with friendly support and quick delivery to your door.">'
                      '<link rel="canonical" href="/"><script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Widget Shop"}</script>'),
    "/about": lambda: page("About the Widget Shop team and our story", f"<h1>About</h1><h4>skip</h4><p>{LOREM} alot of people recieve it. This is is a example.</p>",
                           '<meta name="description" content="About us.">'),
    "/faq": lambda: page("FAQ", "<h1>FAQ</h1><h2>What is a widget?</h2><p>" + LOREM + "</p>"),
    "/dup-a": lambda: page("Duplicate page title that is long enough to pass", f"<h1>Dup</h1><p>{LOREM}</p>"),
    "/dup-b": lambda: page("Duplicate page title that is long enough to pass", f"<h1>Dup</h1><p>{LOREM}</p>"),
    "/near-a": lambda: page("Near duplicate page A is here for testing", f"<h1>Near</h1><p>{LOREM}</p>"),
    "/near-b": lambda: page("Near duplicate page B is here for testing", f"<h1>Near</h1><p>{NEAR}</p>"),
    "/noindex": lambda: page("Hidden page that should not be indexed ok", f"<h1>Hidden</h1><p>{LOREM} unique words zebra quantum</p>",
                             '<meta name="robots" content="noindex, nofollow"><link rel="canonical" href="/about">'),
    "/en/": lambda: page("English home page of the widget shop site", f"<h1>EN</h1><p>{LOREM} english</p>",
                         '<link rel="alternate" hreflang="en" href="http://127.0.0.1:%PORT%/en/"><link rel="alternate" hreflang="es" href="http://127.0.0.1:%PORT%/es/">'
                         '<link rel="alternate" hreflang="xx-bad-code" href="http://127.0.0.1:%PORT%/es/">'),
    "/es/": lambda: page("Pagina de inicio en espanol de la tienda widget", f"<h1>ES</h1><p>{LOREM} espanol</p>", "", lang="es"),
    "/structured": lambda: page("Structured data example page for products", f"<h1>Product</h1><p>{LOREM}</p>",
                                '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Widget"}</script>'),
    "/a11y": lambda: page("Accessibility problems demo page for audits", f'<h1>A11y</h1><p>{LOREM}</p><form action="/x"><input name="q"><button></button></form><a href="/faq"></a>',
                          '<meta name="viewport" content="width=device-width, user-scalable=no">'),
    "/js": lambda: page("JavaScript rendered page for testing the renderer", '<h1>JS</h1><div id="x"></div><script>document.getElementById("x").innerHTML="<p>' + ("rendered words appear only after javascript runs. " * 40) + '</p>"</script>'),
    "/Weird_URL/Page": lambda: page("Weird URL with uppercase and underscores here", f"<h1>Weird</h1><p>{LOREM} weird</p>"),
    "/new": lambda: page("The new page that old URLs redirect to now", f"<h1>New</h1><p>{LOREM} new words here</p>"),
    "/chain2": "R:/new",
    "/chain1": "R:/chain2",
    "/old": "R:/new",
    "/loop1": "R:/loop2",
    "/loop2": "R:/loop1",
}


class H(BaseHTTPRequestHandler):
    port = 8765

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            return self.send(200, "User-agent: *\nDisallow: /private/\nSitemap: http://127.0.0.1:%d/sitemap.xml\n" % self.port, "text/plain")
        if path == "/sitemap.xml":
            urls = ["/", "/about", "/noindex", "/old", "/orphan", "/broken"]
            xml = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
                f"<url><loc>http://127.0.0.1:{self.port}{u}</loc></url>" for u in urls) + "</urlset>"
            return self.send(200, xml, "application/xml")
        if path == "/orphan":
            return self.send(200, page("Orphan page only found in the sitemap file", f"<h1>Orphan</h1><p>{LOREM} orphan</p>"))
        if path == "/img.png":
            return self.send(200, b"\x89PNG\r\n\x1a\n" + b"0" * 300, "image/png")
        if path.startswith("/private"):
            return self.send(200, page("Private area", "<h1>Private</h1>"))
        r = ROUTES.get(path)
        if r is None:
            return self.send(404, "not found", "text/plain")
        if isinstance(r, str):
            self.send_response(301)
            self.send_header("Location", r[2:])
            self.end_headers()
            return
        self.send(200, r().replace("%PORT%", str(self.port)))

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)


if __name__ == "__main__":
    H.port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"fixture site on http://127.0.0.1:{H.port}")
    ThreadingHTTPServer(("127.0.0.1", H.port), H).serve_forever()
