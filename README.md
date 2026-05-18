# site-auditor

Crawls a website and reports broken links, redirects, and dead URLs across four discovery sources.

## Discovery sources

| Source | What it finds |
|--------|--------------|
| **Live crawl** | All `<a href>` links on every reachable page |
| **Sitemap** | URLs in `/sitemap.xml` and `/sitemap_index.xml` (recursive) |
| **Wayback CDX** | Historical URLs from the Internet Archive — catches pages deleted in a site rebuild |
| **Common Crawl CDX** | Independent monthly crawl corpus (~3B pages) — finds URLs the Archive never visited |
| **Short-slug inference** | Abbreviated URL candidates derived from full slugs — catches old canonical URLs that redirected to longer slugs |

## Usage

```bash
pip install requests beautifulsoup4
python crawl.py https://www.example.com
```

Defaults to `https://www.alpharank.ai` if no URL is provided.

## Output

```
[200]         https://www.example.com/page       (crawl)
[404]         https://www.example.com/old-page   (wayback(was:200))
[301 -> 200]  https://www.example.com/short      (inferred(from:/post/long-slug))
```

Final report groups results into: LIVE, REDIRECTS, BROKEN/EMPTY.

## CDX Sources

Uses [`cdx-toolkit`](https://github.com/cocrawler/cdx_toolkit) to query both corpora in one pass:

- **Internet Archive (Wayback Machine)** — full history since 1996
- **Common Crawl** — independent monthly crawl, ~3 billion pages, separate index

Both sources are deduplicated and merged. Common Crawl often surfaces URLs the Archive missed, and vice versa. Based on the [Internet Archive CDX Server API](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server).

## Short-slug inference

For each full slug found during crawl (e.g. `/post/the-confidence-problem-why-ai-answers-cant-be-trusted`), generates abbreviated candidates:
- Strip leading stop words, take first 1–4 meaningful words
- Test each candidate live

This catches old short canonical URLs that Google may still have indexed from a previous CMS or URL structure.
