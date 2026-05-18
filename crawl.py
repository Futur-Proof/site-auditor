#!/usr/bin/env python3
"""
Site auditor — discovers URLs via four sources:
  1. Live crawl (HTML link extraction)
  2. Sitemap (XML, recursive)
  3. Wayback Machine CDX API (historical URLs)
  4. Short-slug inference (abbreviated candidates from full slugs)

Usage:
  python crawl.py [base_url]
  python crawl.py https://www.alpharank.ai
"""

import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://www.alpharank.ai"
PARSED_BASE = urlparse(BASE_URL)
DELAY = 0.3
CDX_TIMEOUT = 30
CDX_RETRIES = 3
CDX_RETRY_DELAY = 5

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SiteAuditor/2.0)"}
session = requests.Session()
session.headers.update(HEADERS)

ASSET_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".ico",
    ".pdf", ".zip", ".mp4", ".mp3",
)

PROBE_PATHS = [
    "/sitemap.xml", "/sitemap_index.xml", "/robots.txt",
    "/terms", "/terms-of-service", "/privacy", "/privacy-policy",
    "/old-home", "/old-home-2", "/spinthewheel",
    "/insight-hub", "/partner", "/ready-to-get-going", "/solutions",
]

STOP_WORDS = {
    "the", "a", "an", "and", "or", "in", "of", "for", "to", "with",
    "how", "why", "what", "your", "our", "is", "it", "its", "at",
    "on", "by", "from", "be", "are", "was", "were", "as", "this",
    "that", "we", "you", "my", "can", "do", "not", "no", "vs",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def get(url, allow_redirects=True, timeout=10):
    try:
        return session.get(url, allow_redirects=allow_redirects, timeout=timeout)
    except requests.RequestException:
        return None


def is_internal(url):
    parsed = urlparse(url)
    host = parsed.netloc.lstrip("www.")
    base_host = PARSED_BASE.netloc.lstrip("www.")
    return parsed.netloc == "" or host == base_host


def normalize(url):
    parsed = urlparse(url)
    return parsed._replace(
        scheme=PARSED_BASE.scheme,
        netloc=PARSED_BASE.netloc,
        fragment="",
        query="",
    ).geturl().rstrip("/") or BASE_URL


def is_asset(url):
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in ASSET_EXTS)


def extract_links(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        abs_url = urljoin(page_url, href)
        if is_internal(abs_url) and not is_asset(abs_url):
            links.add(normalize(abs_url))
    return links


# ── sitemap ───────────────────────────────────────────────────────────────────

def parse_sitemap(url, visited_sitemaps=None):
    if visited_sitemaps is None:
        visited_sitemaps = set()
    if url in visited_sitemaps:
        return set()
    visited_sitemaps.add(url)

    urls = set()
    r = get(url)
    if r is None or r.status_code != 200 or not r.text.strip():
        return urls
    try:
        root = ET.fromstring(r.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            href = loc.text.strip()
            if href.endswith(".xml"):
                urls |= parse_sitemap(href, visited_sitemaps)
            elif is_internal(href) and not is_asset(href):
                urls.add(normalize(href))
    except ET.ParseError:
        pass
    return urls


# ── Wayback CDX ───────────────────────────────────────────────────────────────

def cdx_fetch_page(domain, page, page_size=5):
    """Fetch one page of CDX results using pagination."""
    params = {
        "url": f"{domain}/*",
        "output": "json",
        "fl": "original,statuscode",
        "collapse": "urlkey",
        "filter": "!mimetype:warc/revisit",
        "page": page,
        "pageSize": page_size,
    }
    for attempt in range(CDX_RETRIES):
        try:
            r = requests.get(
                "http://web.archive.org/cdx/search/cdx",
                params=params,
                timeout=CDX_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 503:
                print(f"    CDX 503, retrying in {CDX_RETRY_DELAY}s... (attempt {attempt+1}/{CDX_RETRIES})")
                time.sleep(CDX_RETRY_DELAY)
            else:
                print(f"    CDX returned {r.status_code}")
                return []
        except requests.exceptions.Timeout:
            print(f"    CDX timeout, retrying in {CDX_RETRY_DELAY}s... (attempt {attempt+1}/{CDX_RETRIES})")
            time.sleep(CDX_RETRY_DELAY)
        except Exception as e:
            print(f"    CDX error: {e}")
            return []
    return []


def cdx_num_pages(domain, page_size=5):
    """Get total page count from CDX."""
    params = {
        "url": f"{domain}/*",
        "output": "json",
        "collapse": "urlkey",
        "filter": "!mimetype:warc/revisit",
        "showNumPages": "true",
        "pageSize": page_size,
    }
    for attempt in range(CDX_RETRIES):
        try:
            r = requests.get(
                "http://web.archive.org/cdx/search/cdx",
                params=params,
                timeout=CDX_TIMEOUT,
            )
            if r.status_code == 200:
                text = r.text.strip()
                return int(text) if text.isdigit() else 0
            elif r.status_code == 503:
                time.sleep(CDX_RETRY_DELAY)
        except (requests.exceptions.Timeout, Exception):
            time.sleep(CDX_RETRY_DELAY)
    return 0


def wayback_urls(domain):
    """Pull all historical URLs from Wayback CDX with pagination + resumption fallback."""
    print("  [CDX] Querying Wayback Machine...")
    host = urlparse(domain).netloc or domain

    # try pagination first
    num_pages = cdx_num_pages(host)
    urls = {}  # url -> last known status code

    if num_pages > 0:
        print(f"  [CDX] {num_pages} page(s) to fetch")
        for page in range(num_pages):
            rows = cdx_fetch_page(host, page)
            if not rows:
                continue
            header = rows[0]
            orig_idx = header.index("original") if "original" in header else 0
            status_idx = header.index("statuscode") if "statuscode" in header else 1
            for row in rows[1:]:
                url = row[orig_idx]
                status = row[status_idx] if len(row) > status_idx else "-"
                if is_internal(url) and not is_asset(url):
                    n = normalize(url)
                    urls[n] = status
            time.sleep(0.5)
    else:
        # fallback: resumption key method
        print("  [CDX] Pagination unavailable, using resumption key...")
        resume_key = None
        batch = 0
        while True:
            params = {
                "url": f"{host}/*",
                "output": "json",
                "fl": "original,statuscode",
                "collapse": "urlkey",
                "filter": "!mimetype:warc/revisit",
                "limit": 500,
                "showResumeKey": "true",
            }
            if resume_key:
                params["resumeKey"] = resume_key

            rows = []
            for attempt in range(CDX_RETRIES):
                try:
                    r = requests.get(
                        "http://web.archive.org/cdx/search/cdx",
                        params=params,
                        timeout=CDX_TIMEOUT,
                    )
                    if r.status_code == 200:
                        rows = r.json()
                        break
                    time.sleep(CDX_RETRY_DELAY)
                except Exception:
                    time.sleep(CDX_RETRY_DELAY)

            if not rows:
                break

            header = rows[0]
            orig_idx = header.index("original") if "original" in header else 0
            status_idx = header.index("statuscode") if "statuscode" in header else 1

            # last row may be resume key
            last = rows[-1]
            if len(last) == 1:
                resume_key = last[0]
                data_rows = rows[1:-1]
            else:
                resume_key = None
                data_rows = rows[1:]

            for row in data_rows:
                url = row[orig_idx]
                status = row[status_idx] if len(row) > status_idx else "-"
                if is_internal(url) and not is_asset(url):
                    n = normalize(url)
                    urls[n] = status

            batch += 1
            if not resume_key:
                break
            time.sleep(0.5)

    print(f"  [CDX] Found {len(urls)} unique historical URLs")
    return urls


# ── short-slug inference ──────────────────────────────────────────────────────

def infer_short_slugs(url):
    """
    Given a full slug like /post/the-confidence-problem-why-generative-ai-...
    generate likely abbreviated candidates that may have been the original URLs.
    """
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return set()

    prefix = "/" + parts[0]  # e.g. /post
    slug = parts[1]           # e.g. the-confidence-problem-why-...
    words = slug.split("-")

    candidates = set()

    # strip stop words from front
    filtered = [w for w in words if w.lower() not in STOP_WORDS]

    # try first 1, 2, 3 meaningful words
    for n in range(1, min(5, len(filtered) + 1)):
        short = "-".join(filtered[:n])
        candidates.add(normalize(BASE_URL + prefix + "/" + short))

    # try first 1, 2, 3 raw words (including stop words)
    for n in range(1, min(4, len(words) + 1)):
        short = "-".join(words[n:n+3]) if n < len(words) else None
        if short:
            candidates.add(normalize(BASE_URL + prefix + "/" + short))

    # drop the candidate if it's identical to the original
    candidates.discard(normalize(url))
    return candidates


# ── status check ─────────────────────────────────────────────────────────────

def check_url(url):
    r = get(url, allow_redirects=False)
    if r is None:
        return {"url": url, "status": "ERROR", "final_url": None, "redirect_chain": []}

    chain = []
    current = r
    while current is not None and current.status_code in (301, 302, 303, 307, 308):
        location = current.headers.get("Location", "")
        chain.append((current.status_code, current.url))
        if not location:
            break
        next_url = urljoin(current.url, location)
        current = get(next_url, allow_redirects=False)

    if current is None:
        return {"url": url, "status": "ERROR", "final_url": None, "redirect_chain": chain}

    body = current.text or ""
    status = current.status_code
    if status == 200 and not body.strip():
        status = "200-EMPTY"

    return {
        "url": url,
        "status": status,
        "final_url": current.url if chain else None,
        "redirect_chain": chain,
    }


# ── main crawl ────────────────────────────────────────────────────────────────

def crawl():
    visited = set()
    queue = deque()
    results = []
    url_source = {}

    def enqueue(url, source):
        if url not in visited and url not in url_source:
            queue.append(url)
            url_source[url] = source

    # 1. probes
    for path in PROBE_PATHS:
        enqueue(normalize(BASE_URL + path), "probe")

    # 2. homepage seed
    enqueue(normalize(BASE_URL), "seed")

    # 3. sitemap
    print("\n[1/4] Parsing sitemap...")
    for u in parse_sitemap(f"{BASE_URL}/sitemap.xml"):
        enqueue(u, "sitemap")
    for u in parse_sitemap(f"{BASE_URL}/sitemap_index.xml"):
        enqueue(u, "sitemap")

    # 4. Wayback CDX
    print("\n[2/4] Fetching Wayback CDX history...")
    cdx_results = wayback_urls(BASE_URL)
    for u, cdx_status in cdx_results.items():
        enqueue(u, f"wayback(was:{cdx_status})")

    print(f"\n[3/4] Crawling {len(queue)} queued URLs + link extraction...")

    full_slugs = set()

    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        result = check_url(url)
        result["source"] = url_source.get(url, "crawl")
        results.append(result)

        code = result["status"]
        redirect_note = f" -> {result['final_url']}" if result["final_url"] else ""
        print(f"  [{code}] {url}{redirect_note}  ({result['source']})")

        if code == 200:
            r = get(url)
            if r and "text/html" in r.headers.get("Content-Type", ""):
                for link in extract_links(r.text, url):
                    enqueue(link, "crawl")
                    # collect full slugs for inference
                    path = urlparse(link).path
                    if path.count("/") >= 2:
                        full_slugs.add(link)
            time.sleep(DELAY)

    # 5. short-slug inference
    print(f"\n[4/4] Inferring short-slug candidates from {len(full_slugs)} full slugs...")
    for full_url in full_slugs:
        for candidate in infer_short_slugs(full_url):
            enqueue(candidate, f"inferred(from:{urlparse(full_url).path})")

    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        result = check_url(url)
        result["source"] = url_source.get(url, "inferred")
        results.append(result)

        code = result["status"]
        redirect_note = f" -> {result['final_url']}" if result["final_url"] else ""
        print(f"  [{code}] {url}{redirect_note}  ({result['source']})")
        time.sleep(DELAY * 0.5)

    return results


# ── report ────────────────────────────────────────────────────────────────────

def report(results):
    broken_statuses = (404, 410, "ERROR", "200-EMPTY")
    broken = [r for r in results if r["status"] in broken_statuses]
    redirects = [r for r in results if r["redirect_chain"]]
    live = [r for r in results if r["status"] == 200]

    print("\n" + "=" * 70)
    print(f"AUDIT COMPLETE — {len(results)} URLs checked")
    print("=" * 70)
    print(f"\n  LIVE (200):      {len(live)}")
    print(f"  REDIRECTS:       {len(redirects)}")
    print(f"  BROKEN/EMPTY:    {len(broken)}")

    if broken:
        print("\n--- BROKEN / EMPTY ---")
        for r in sorted(broken, key=lambda x: x["url"]):
            print(f"  [{r['status']:12}]  {r['url']}")
            print(f"  {'':14}  source: {r['source']}")

    if redirects:
        print("\n--- REDIRECTS ---")
        for r in sorted(redirects, key=lambda x: x["url"]):
            chain_str = " -> ".join(str(c) for c, _ in r["redirect_chain"])
            final = f"\n  {'':14}  -> {r['final_url']}" if r["final_url"] else ""
            print(f"  [{chain_str} -> {r['status']}]  {r['url']}{final}")
            print(f"  {'':14}  source: {r['source']}")


if __name__ == "__main__":
    print(f"Target: {BASE_URL}\n")
    results = crawl()
    report(results)
