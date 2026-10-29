"""Fetch a page with a real (headless Chromium) browser via Playwright.

Used ONLY as a fallback for pages plain HTTP could not read (JavaScript-rendered
pages, link shorteners that redirect with JS, sites rejecting non-browser
clients). It behaves like a normal visitor: it obeys robots.txt, visits one URL
at a time, and gives up when it meets a challenge / captcha / access-denied
page. It does NOT use stealth plugins, fingerprint tricks or captcha solving.

Setup (once):  pip install playwright && playwright install chromium
"""
import concurrent.futures
import os
import re
import urllib.request
import urllib.robotparser
from urllib.parse import urlparse

from scholarships.telegram_parser import INTERSTITIAL_HOSTS, classify_page, page_title, page_to_text

# a normal desktop Chrome identity (the headless default says "HeadlessChrome")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CHALLENGE = re.compile(
    r"just a moment|attention required|access denied|verify you are (a )?human|"
    r"are you a robot|captcha|checking your browser|request blocked|forbidden", re.I)


def robots_allows(url, timeout=8):
    """False only when robots.txt exists and disallows the URL for generic agents."""
    p = urlparse(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(f"{p.scheme}://{p.netloc}/robots.txt",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rp.parse(r.read().decode("utf-8", "ignore").splitlines())
    except Exception:
        return True  # no readable robots.txt = no restriction
    return rp.can_fetch("*", url)


class BrowserFetcher:
    """One browser, used from ONE dedicated thread (Playwright sync API is thread-bound)."""

    def __init__(self, headless=True, nav_timeout=30000):
        self.headless, self.nav_timeout = headless, nav_timeout
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._pw = self._browser = self._ctx = None

    # ---- runs in the dedicated thread -------------------------------
    def _start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # Playwright's own Chromium needs a recent OS (not macOS 12 and older): then
        # fall back to the Chrome / Edge installed on the machine. Force one with
        # BROWSER_CHANNEL=chrome|msedge.
        forced = os.getenv("BROWSER_CHANNEL", "").strip()
        last = None
        for channel in ([forced] if forced else [None, "chrome", "msedge"]):
            try:
                self._browser = self._pw.chromium.launch(headless=self.headless, channel=channel)
                break
            except Exception as e:
                last = e
        else:
            raise RuntimeError(f"no usable browser (install Chrome or run "
                               f"'playwright install chromium'): {str(last)[:200]}")
        self._ctx = self._browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                              viewport={"width": 1366, "height": 900})

    def _fetch(self, url):
        if not robots_allows(url):
            return {"status": "unreachable", "url": url, "error": "robots.txt disallows"}
        if self._ctx is None:
            self._start()
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
            host = lambda u: (urlparse(u).hostname or "").lower()  # noqa: E731
            if any(host(page.url).endswith(h) for h in INTERSTITIAL_HOSTS):
                try:  # JS interstitial: wait for the redirect to the real page
                    page.wait_for_url(lambda u: not any(host(u).endswith(h) for h in INTERSTITIAL_HOSTS),
                                      timeout=20000)
                    resp = None
                except Exception:
                    pass
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            html = final = title = text = ""
            for _ in range(4):  # let slow JS finish / client-side redirects land
                try:
                    html, final, title = page.content(), page.url, page.title()
                except Exception:  # navigation in progress
                    page.wait_for_timeout(1500)
                    continue
                text = page_to_text(html)
                if len(text) >= 300:
                    break
                page.wait_for_timeout(1500)
            code = resp.status if resp is not None else 200
            if page.url != url and resp is not None and resp.url != page.url:
                code = 200  # a client-side redirect happened; the status above is of the first hop
            if code >= 400:
                return {"status": "unreachable", "url": final, "title": title,
                        "error": f"HTTP {code} (browser)"}
            if CHALLENGE.search(title or "") or (len(text) < 600 and CHALLENGE.search(text)):
                return {"status": "unreachable", "url": final, "title": title,
                        "error": "blocked by bot challenge (browser)"}
            status = classify_page(final, html, text)
            return {"status": status, "url": final, "title": page_title(html) or title,
                    "text": text if status == "ok" else None, "via": "browser"}
        except Exception as e:
            return {"status": "unreachable", "url": url, "error": f"browser: {e!r}"[:200]}
        finally:
            page.close()

    def _close(self):
        for obj in (self._ctx, self._browser):
            try:
                obj and obj.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    # ---- public (callable from any thread) --------------------------
    def fetch(self, url):
        return self.pool.submit(self._fetch, url).result()

    def close(self):
        try:
            self.pool.submit(self._close).result(timeout=20)
        finally:
            self.pool.shutdown(wait=False)