#!/usr/bin/env python3
"""
Site auditor — discovers URLs via five sources:
  1. Live crawl (HTML link extraction)
  2. Sitemap (XML, recursive)
  3. Wayback Machine CDX (Internet Archive historical URLs)
  4. Common Crawl CDX (independent corpus via cdx-toolkit)
  5. Short-slug inference (abbreviated candidates from full slugs)

Usage:
  python crawl.py <url> [options]

Options:
  --delay FLOAT         Seconds between requests (default: 0.3)
  --cdx-timeout INT     Seconds before giving up on a CDX source (default: 60)
  --skip-cdx            Skip Wayback + Common Crawl lookup
  --skip-infer          Skip short-slug inference
  --probes PATH,...     Extra paths to probe, comma-separated
  --output DIR          Directory to write JSON + text report (default: results/)
"""

import argparse
import json
import os
import queue
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import cdx_toolkit
import requests
from bs4 import BeautifulSoup


# ── constants ─────────────────────────────────────────────────────────────────

ASSET_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".ico",
    ".pdf", ".zip", ".mp4", ".mp3",
)

STANDARD_PROBES = [
    "/sitemap.xml", "/sitemap_index.xml", "/robots.txt",
    "/terms", "/terms-of-service", "/privacy", "/privacy-policy",
    "/about", "/contact", "/login", "/signup",
    "/feed", "/feed.xml", "/atom.xml", "/rss.xml",
    "/404", "/.well-known/security.txt",
]

STOP_WORDS = {
    "the", "a", "an", "and", "or", "in", "of", "for", "to", "with",
    "how", "why", "what", "your", "our", "is", "it", "its", "at",
    "on", "by", "from", "be", "are", "was", "were", "as", "this",
    "that", "we", "you", "my", "can", "do", "not", "no", "vs",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SiteAuditor/2.0)"}


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Site link auditor")
    p.add_argument("url", help="Base URL to audit (e.g. https://www.example.com)")
    p.add_argument("--delay", type=float, default=0.3)
    p.add_argument("--cdx-timeout", type=int, default=60)
    p.add_argument("--skip-cdx", action="store_true")
    p.add_argument("--skip-infer", action="store_true")
    p.add_argument("--show-inferred", action="store_true",
                   help="Include inferred short-slug 404s in the report (noisy without GSC data)")
    p.add_argument("--probes", default="", help="Extra paths to probe, comma-separated")
    p.add_argument("--output", default="results", help="Output directory")
    return p.parse_args()


# ── helpers ───────────────────────────────────────────────────────────────────

def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get(session, url, allow_redirects=True, timeout=10):
    try:
        return session.get(url, allow_redirects=allow_redirects, timeout=timeout)
    except requests.RequestException:
        return None


def is_internal(url, base_host):
    parsed = urlparse(url)
    host = parsed.netloc.lstrip("www.")
    return parsed.netloc == "" or host == base_host


def normalize(url, parsed_base):
    parsed = urlparse(url)
    return parsed._replace(
        scheme=parsed_base.scheme,
        netloc=parsed_base.netloc,
        fragment="",
        query="",
    ).geturl().rstrip("/") or parsed_base.geturl()


def is_asset(url):
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in ASSET_EXTS)


def extract_links(html, page_url, base_host, parsed_base):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        abs_url = urljoin(page_url, href)
        if is_internal(abs_url, base_host) and not is_asset(abs_url):
            links.add(normalize(abs_url, parsed_base))
    return links


# ── sitemap ───────────────────────────────────────────────────────────────────

def parse_sitemap(session, url, base_host, parsed_base, visited=None):
    if visited is None:
        visited = set()
    if url in visited:
        return set()
    visited.add(url)

    urls = set()
    r = get(session, url)
    if r is None or r.status_code != 200 or not r.text.strip():
        return urls
    try:
        root = ET.fromstring(r.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            href = loc.text.strip()
            if href.endswith(".xml"):
                urls |= parse_sitemap(session, href, base_host, parsed_base, visited)
            elif is_internal(href, base_host) and not is_asset(href):
                urls.add(normalize(href, parsed_base))
    except ET.ParseError:
        pass
    return urls


# ── CDX ───────────────────────────────────────────────────────────────────────

def fetch_cdx_source(source_name, source_key, host, base_host, parsed_base, timeout_s):
    result_q = queue.Queue()

    def _fetch():
        urls = {}
        try:
            cdx = cdx_toolkit.CDXFetcher(source=source_key)
            for obj in cdx.iter(f"{host}/*", fl="original,statuscode", limit=5000):
                url = obj.get("original", "")
                status = obj.get("statuscode", "-")
                if url and is_internal(url, base_host) and not is_asset(url):
                    n = normalize(url, parsed_base)
                    urls[n] = status
        except Exception as e:
            print(f"    [{source_name}] error: {e}")
        result_q.put(urls)

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=timeout_s)

    if t.is_alive():
        print(f"    [{source_name}] timed out after {timeout_s}s — skipping")
        return {}

    return result_q.get()


def historical_urls(base_url, base_host, parsed_base, cdx_timeout):
    host = urlparse(base_url).netloc or base_url
    all_urls = {}

    print("  [CDX] Querying Wayback Machine (Internet Archive)...")
    wb = fetch_cdx_source("Wayback", "ia", host, base_host, parsed_base, cdx_timeout)
    print(f"        {len(wb)} URLs")
    all_urls.update(wb)

    print("  [CDX] Querying Common Crawl...")
    cc = fetch_cdx_source("CommonCrawl", "cc", host, base_host, parsed_base, cdx_timeout)
    new = len(set(cc) - set(wb))
    print(f"        {len(cc)} URLs ({new} new beyond Wayback)")
    for url, status in cc.items():
        if url not in all_urls:
            all_urls[url] = status

    print(f"  [CDX] Total unique historical URLs: {len(all_urls)}")
    return all_urls


# ── short-slug inference ──────────────────────────────────────────────────────

def infer_short_slugs(url, base_url, parsed_base):
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return set()

    prefix = "/" + parts[0]
    slug = parts[1]
    words = slug.split("-")
    filtered = [w for w in words if w.lower() not in STOP_WORDS]

    candidates = set()
    for n in range(1, min(5, len(filtered) + 1)):
        candidates.add(normalize(base_url + prefix + "/" + "-".join(filtered[:n]), parsed_base))
    for n in range(1, min(4, len(words) + 1)):
        chunk = "-".join(words[n:n+3]) if n < len(words) else None
        if chunk:
            candidates.add(normalize(base_url + prefix + "/" + chunk, parsed_base))

    candidates.discard(normalize(url, parsed_base))
    return candidates


# ── status check ─────────────────────────────────────────────────────────────

def check_url(session, url):
    r = get(session, url, allow_redirects=False)
    if r is None:
        return {"url": url, "status": "ERROR", "final_url": None, "redirect_chain": []}

    chain = []
    current = r
    while current is not None and current.status_code in (301, 302, 303, 307, 308):
        location = current.headers.get("Location", "")
        chain.append((current.status_code, current.url))
        if not location:
            break
        current = get(session, urljoin(current.url, location), allow_redirects=False)

    if current is None:
        return {"url": url, "status": "ERROR", "final_url": None, "redirect_chain": chain}

    status = current.status_code
    if status == 200 and not (current.text or "").strip():
        status = "200-EMPTY"

    return {
        "url": url,
        "status": status,
        "final_url": current.url if chain else None,
        "redirect_chain": chain,
    }


# ── crawl ─────────────────────────────────────────────────────────────────────

def crawl(base_url, delay, cdx_timeout, skip_cdx, skip_infer, extra_probes):
    parsed_base = urlparse(base_url)
    base_host = parsed_base.netloc.lstrip("www.")
    session = make_session()

    visited = set()
    q = deque()
    results = []
    url_source = {}

    def enqueue(url, source):
        if url not in visited and url not in url_source:
            q.append(url)
            url_source[url] = source

    # 1. standard + extra probes
    all_probes = STANDARD_PROBES + [p if p.startswith("/") else "/" + p for p in extra_probes]
    for path in all_probes:
        enqueue(normalize(base_url + path, parsed_base), "probe")

    # 2. homepage
    enqueue(normalize(base_url, parsed_base), "seed")

    # 3. sitemap
    print("\n[1] Parsing sitemap...")
    for u in parse_sitemap(session, f"{base_url}/sitemap.xml", base_host, parsed_base):
        enqueue(u, "sitemap")
    for u in parse_sitemap(session, f"{base_url}/sitemap_index.xml", base_host, parsed_base):
        enqueue(u, "sitemap")

    # 4. CDX history
    if not skip_cdx:
        print("\n[2] Fetching CDX history (Wayback + Common Crawl)...")
        for u, status in historical_urls(base_url, base_host, parsed_base, cdx_timeout).items():
            enqueue(u, f"cdx(was:{status})")
    else:
        print("\n[2] CDX skipped")

    # 5. live crawl
    full_slugs = set()
    print(f"\n[3] Crawling {len(q)} queued URLs + link extraction...")

    while q:
        url = q.popleft()
        if url in visited:
            continue
        visited.add(url)

        result = check_url(session, url)
        result["source"] = url_source.get(url, "crawl")
        results.append(result)

        code = result["status"]
        redir = f" -> {result['final_url']}" if result["final_url"] else ""
        print(f"  [{code}] {url}{redir}  ({result['source']})")

        if code == 200:
            r = get(session, url)
            if r and "text/html" in r.headers.get("Content-Type", ""):
                for link in extract_links(r.text, url, base_host, parsed_base):
                    enqueue(link, "crawl")
                    if urlparse(link).path.count("/") >= 2:
                        full_slugs.add(link)
            time.sleep(delay)

    # 6. short-slug inference
    if not skip_infer:
        print(f"\n[4] Inferring short-slug candidates from {len(full_slugs)} full slugs...")
        for full_url in full_slugs:
            for candidate in infer_short_slugs(full_url, base_url, parsed_base):
                enqueue(candidate, f"inferred(from:{urlparse(full_url).path})")

        while q:
            url = q.popleft()
            if url in visited:
                continue
            visited.add(url)

            result = check_url(session, url)
            result["source"] = url_source.get(url, "inferred")
            results.append(result)

            code = result["status"]
            redir = f" -> {result['final_url']}" if result["final_url"] else ""
            print(f"  [{code}] {url}{redir}  ({result['source']})")
            time.sleep(delay * 0.5)
    else:
        print("\n[4] Inference skipped")

    return results


# ── report + output ───────────────────────────────────────────────────────────

BROKEN_STATUSES = (404, 410, "ERROR", "200-EMPTY")


def is_inferred(result):
    return result["source"].startswith("inferred(")


def build_report(base_url, results, show_inferred=False):
    all_broken = [r for r in results if r["status"] in BROKEN_STATUSES]
    redirects = [r for r in results if r["redirect_chain"]]
    live = [r for r in results if r["status"] == 200]

    # by default suppress inferred 404s — they're hypothetical without GSC data
    broken = all_broken if show_inferred else [r for r in all_broken if not is_inferred(r)]
    hidden_count = len(all_broken) - len(broken)

    lines = []
    lines.append("=" * 70)
    lines.append(f"AUDIT: {base_url}")
    lines.append(f"RUN:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"TOTAL: {len(results)} URLs checked")
    lines.append("=" * 70)
    lines.append(f"\n  LIVE (200):      {len(live)}")
    lines.append(f"  REDIRECTS:       {len(redirects)}")
    lines.append(f"  BROKEN/EMPTY:    {len(broken)}")
    if hidden_count:
        lines.append(f"  (+ {hidden_count} inferred short-slug 404s hidden — run with --show-inferred to include)")

    if broken:
        lines.append("\n--- BROKEN / EMPTY ---")
        for r in sorted(broken, key=lambda x: x["url"]):
            lines.append(f"  [{str(r['status']):<12}]  {r['url']}")
            lines.append(f"  {'':14}  source: {r['source']}")

    if redirects:
        lines.append("\n--- REDIRECTS ---")
        for r in sorted(redirects, key=lambda x: x["url"]):
            chain_str = " -> ".join(str(c) for c, _ in r["redirect_chain"])
            lines.append(f"  [{chain_str} -> {r['status']}]  {r['url']}")
            if r["final_url"]:
                lines.append(f"  {'':14}  -> {r['final_url']}")
            lines.append(f"  {'':14}  source: {r['source']}")

    return "\n".join(lines)


def save_results(base_url, results, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    host = urlparse(base_url).netloc.replace(".", "_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    stem = f"{host}_{ts}"

    json_path = os.path.join(output_dir, f"{stem}.json")
    txt_path = os.path.join(output_dir, f"{stem}.txt")

    payload = {
        "url": base_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "results": results,
    }
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)

    report_text = build_report(base_url, results)
    with open(txt_path, "w") as f:
        f.write(report_text)

    return json_path, txt_path


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()
    base_url = args.url.rstrip("/")
    extra_probes = [p.strip() for p in args.probes.split(",") if p.strip()]

    print(f"Target: {base_url}\n")

    results = crawl(
        base_url=base_url,
        delay=args.delay,
        cdx_timeout=args.cdx_timeout,
        skip_cdx=args.skip_cdx,
        skip_infer=args.skip_infer,
        extra_probes=extra_probes,
    )

    report_text = build_report(base_url, results, show_inferred=args.show_inferred)
    print("\n" + report_text)

    json_path, txt_path = save_results(base_url, results, args.output)
    print(f"\nResults saved:")
    print(f"  JSON: {json_path}")
    print(f"  Text: {txt_path}")
