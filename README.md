# site-auditor

A site crawler that finds broken links, dead redirects, and ghost URLs — pages Google still has indexed from old site structures that now return 404.

Works on any site. Pass a URL and get a full audit.

---

## Install

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
python crawl.py <url> [options]
```

### Examples

```bash
# Basic audit
python crawl.py https://www.example.com

# Skip CDX history lookup (faster, live site only)
python crawl.py https://www.example.com --skip-cdx

# Skip short-slug inference (faster, no guessing)
python crawl.py https://www.example.com --skip-infer

# Add site-specific paths to probe
python crawl.py https://www.example.com --probes /old-home,/spinthewheel,/guest-pass

# Custom output directory
python crawl.py https://www.example.com --output ./audits/example

# Slower crawl to be polite to the server
python crawl.py https://www.example.com --delay 1.0

# Full options
python crawl.py https://www.example.com \
  --delay 0.5 \
  --cdx-timeout 90 \
  --probes /old-home,/legacy \
  --output ./audits
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `url` | required | Base URL to audit |
| `--delay` | `0.3` | Seconds between requests |
| `--cdx-timeout` | `60` | Seconds before giving up on a CDX source |
| `--skip-cdx` | off | Skip Wayback + Common Crawl lookup |
| `--skip-infer` | off | Skip short-slug inference |
| `--probes` | — | Extra paths to probe, comma-separated |
| `--output` | `results/` | Directory to write JSON + text report |

---

## Output

Results are saved to `--output` (default: `results/`) as two files per run:

```
results/
  www_example_com_20260518_1430.json   ← machine-readable, all URLs + metadata
  www_example_com_20260518_1430.txt    ← human-readable report
```

The JSON structure:

```json
{
  "url": "https://www.example.com",
  "timestamp": "2026-05-18T14:30:00+00:00",
  "total": 124,
  "results": [
    {
      "url": "https://www.example.com/old-page",
      "status": 404,
      "final_url": null,
      "redirect_chain": [],
      "source": "cdx(was:200)"
    },
    {
      "url": "https://www.example.com/moved",
      "status": 200,
      "final_url": "https://www.example.com/new-page",
      "redirect_chain": [[301, "https://www.example.com/moved"]],
      "source": "crawl"
    }
  ]
}
```

---

## How it works

The auditor discovers URLs from five independent sources, then checks every one live.

### 1. Standard probes

A fixed list of common paths checked on every site:

```
/sitemap.xml  /robots.txt  /terms  /privacy  /privacy-policy
/about  /contact  /login  /feed.xml  /.well-known/security.txt  ...
```

Add site-specific paths with `--probes /old-home,/spinthewheel`.

### 2. Sitemap

Fetches `/sitemap.xml` and `/sitemap_index.xml`. Recursively parses sitemap indexes. All `<loc>` URLs are queued for checking.

### 3. Live crawl

Starting from the homepage, fetches every HTML page and extracts `<a href>` links. Follows the crawl graph until no new internal URLs are found. Assets (images, CSS, JS, fonts) are excluded.

### 4. Wayback Machine + Common Crawl (CDX)

Queries two historical URL corpora using [`cdx-toolkit`](https://github.com/cocrawler/cdx_toolkit):

- **Internet Archive (Wayback Machine)** — full crawl history since 1996
- **Common Crawl** — independent monthly crawl corpus, ~3 billion pages

This is where *ghost URLs* are found — pages that existed on a previous version of the site, were indexed by Google, and now return 404. Neither the live site nor its sitemap knows these exist; only historical crawl data reveals them.

Each source runs in its own thread with a configurable timeout (`--cdx-timeout`). If a source is unavailable (503, network timeout), it fails fast and the crawl continues without it.

### 5. Short-slug inference

For every full URL found during the crawl (e.g. `/post/the-confidence-problem-why-generative-ai-answers-cant-be-trusted`), the auditor generates abbreviated candidates:

- Strip leading stop words, take first 1–4 meaningful words → `/post/confidence-problem`
- Sliding window of 3-word chunks → `/post/why-generative-ai`

Each candidate is checked live. This catches old short canonical URLs from a previous CMS or URL structure that Google may still index — even when CDX data is unavailable.

---

## Status codes

| Status | Meaning |
|--------|---------|
| `200` | Live |
| `200-EMPTY` | Returns 200 but with an empty body (e.g. a placeholder `robots.txt`) |
| `301` / `302` | Redirect — report shows full chain and final destination |
| `404` | Not found |
| `410` | Gone |
| `ERROR` | Network error or timeout |

---

## Source labels

Every result includes a `source` field showing how the URL was discovered:

| Source | Meaning |
|--------|---------|
| `seed` | Homepage |
| `probe` | Standard or custom probe path |
| `sitemap` | Found in sitemap.xml |
| `crawl` | Extracted from a live page's `<a href>` links |
| `cdx(was:200)` | Found in CDX history; was live when last crawled |
| `cdx(was:404)` | Found in CDX history; was already dead when last crawled |
| `inferred(from:/post/...)` | Short-slug candidate derived from a full URL |

---

## CDX availability

The Wayback Machine and Common Crawl CDX APIs are public infrastructure and occasionally return 503 or time out. When this happens:

- Each source has a hard timeout (`--cdx-timeout`, default 60s)
- Timed-out sources are skipped with a warning
- The crawl continues with whatever data was collected
- Short-slug inference covers the same gap when CDX is unavailable

To skip CDX entirely for a fast run: `--skip-cdx`

---

## Requirements

- Python 3.8+
- `requests`
- `beautifulsoup4`
- `cdx-toolkit`

```bash
pip install requests beautifulsoup4 cdx-toolkit
```
