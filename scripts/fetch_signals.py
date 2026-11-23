import os
import re
import time
import json
import requests
import dns.resolver
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

USER_AGENT = {"User-Agent": "DocsRadarBot/1.2 (EarlyStageSignalTracker)"}

IGNORED_DOMAINS = (
    "github.com", "huggingface.co", "youtube.com", "x.com", "twitter.com",
    "reddit.com", "vercel.app", "netlify.app", "github.io", "pages.dev"
)
IGNORED_ORGS = {
    "nvidia", "microsoft", "google", "aws", "amazon", "meta", "facebook",
    "apache", "kubernetes", "ultralytics", "vllm-project", "huggingface"
}

def is_ignored_domain(domain):
    return not domain or any(domain == d or domain.endswith(f".{d}") for d in IGNORED_DOMAINS)

# Known CNAME targets for hosted documentation platforms
DOCS_CNAME_SIGNATURES = {
    "mintlify": "Mintlify",
    "gitbook.io": "GitBook",
    "readme.io": "ReadMe",
    "document360": "Document360",
    "stoplight.io": "Stoplight",
    "fern": "Fern",
    "archbee": "Archbee",
    "helpscoutdocs.com": "Help Scout Docs",
    "zendesk.com": "Zendesk Guide",
}

# Expanded Option B queries: (GitHub Search Query, Platform Label)
CODE_SEARCH_TARGETS = [
    ('"navigation" filename:docs.json path:/', "Mintlify (docs.json)"),
    ('"name" filename:mintlify.json path:/', "Mintlify (mintlify.json)"),
    ('"structure" filename:.gitbook.yaml path:/', "GitBook"),
    ('"organization" filename:fern.config.json', "Fern"),
    ('"apis" filename:redocly.yaml path:/', "Redocly"),
    ('"presets" filename:docusaurus.config.js path:/', "Docusaurus"),
    ('"starlight" filename:astro.config.mjs path:/', "Astro Starlight"),
    ('"defineConfig" path:.vitepress', "VitePress"),
    ('"defineDocs" filename:source.config.ts', "Fumadocs"),
    ('"sphinx" filename:.readthedocs.yaml path:/', "Read the Docs / Sphinx"),
    ('"site_name" filename:mkdocs.yml path:/', "MkDocs"),
    ('"StylesPath" filename:.vale.ini path:/', "Vale Linter"),
    ('"#" filename:llms.txt path:/', "llms.txt (AI Docs)"),
]

leads_by_url = {}

def add_or_merge_lead(url, name, domain, signal_type, detail, score):
    """Stores a lead or merges signals & boosts score if the URL was already spotted."""
    if url in leads_by_url:
        existing = leads_by_url[url]
        existing["score"] += score
        if signal_type not in existing["signal_type"]:
            existing["signal_type"] += f" + {signal_type}"
        existing["detail"] += f" | {detail}"
    else:
        leads_by_url[url] = {
            "name": name,
            "url": url,
            "domain": domain,
            "signal_type": signal_type,
            "detail": detail,
            "score": score,
        }

def extract_domain(url):
    if not url:
        return ""
    netloc = urlparse(url).netloc.lower()
    return netloc.replace("www.", "").split(":")[0]

def detect_docs_platform_via_dns(domain):
    """Bonus Signal: Checks CNAME records on docs.<domain> and developer.<domain>."""
    if is_ignored_domain(domain):
        return None

    resolver = dns.resolver.Resolver()
    resolver.timeout = 3.0
    resolver.lifetime = 3.0

    for sub in ["docs", "developer", "developers", "api"]:
        fqdn = f"{sub}.{domain}"
        try:
            answers = resolver.resolve(fqdn, "CNAME")
            for rdata in answers:
                cname_target = str(rdata.target).rstrip(".").lower()
                for signature, vendor_name in DOCS_CNAME_SIGNATURES.items():
                    if signature in cname_target:
                        return f"{vendor_name} via {fqdn} ({cname_target})"
                return f"Custom CNAME on {fqdn} -> {cname_target}"
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.Timeout, dns.exception.DNSException):
            continue
    return None

def github_code_search(query):
    """Rate-limit-aware wrapper for GitHub's 10 req/min Code Search API."""
    url = f"https://api.github.com/search/code?q={requests.utils.quote(query)}&per_page=15"
    res = requests.get(url, headers=GH_HEADERS, timeout=15)

    remaining = int(res.headers.get("X-RateLimit-Remaining", 1))
    if remaining <= 1:
        reset_time = int(res.headers.get("X-RateLimit-Reset", time.time() + 60))
        sleep_seconds = max(reset_time - int(time.time()) + 2, 10)
        print(f"Code Search rate limit reached. Sleeping {sleep_seconds}s...")
        time.sleep(sleep_seconds)
    else:
        time.sleep(6.5)

    if res.status_code == 200:
        return res.json().get("items", [])
    print(f"Warning: Code search returned HTTP {res.status_code} for query: {query}")
    return []

def score_github_repo(repo, base_score):
    """Filters out hobby projects and mega-corps, then enriches with DNS CNAME checks."""
    stars = repo.get("stargazers_count", 0)
    owner_type = repo.get("owner", {}).get("type", "User")
    owner_login = repo.get("owner", {}).get("login", "").lower()
    homepage = repo.get("homepage") or ""
    domain = extract_domain(homepage)

    if owner_login in IGNORED_ORGS or stars < 15 or stars > 3000 or repo.get("fork"):
        return None

    score = base_score
    notes = [f"⭐ {stars}"]

    if owner_type == "Organization":
        score += 25
        notes.append("Org Repo")
    if 25 <= stars <= 1500:
        score += 15
    if not is_ignored_domain(domain):
        score += 15
        notes.append(f"Site: {domain}")
        dns_hit = detect_docs_platform_via_dns(domain)
        if dns_hit:
            score += 25
            notes.append(f"DNS: {dns_hit}")

    return score, domain, " • ".join(notes)

# 1. GitHub: Expanded Option B Config Search
def scan_github_configs():
    seen_repos = set()
    for query, framework in CODE_SEARCH_TARGETS:
        print(f"Scanning GitHub for {framework}...")
        items = github_code_search(query)
        for item in items:
            repo_stub = item["repository"]
            full_name = repo_stub["full_name"]
            if full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            repo_res = requests.get(repo_stub["url"], headers=GH_HEADERS, timeout=10)
            if repo_res.status_code != 200:
                continue
            repo = repo_res.json()
            scored = score_github_repo(repo, base_score=35)
            if not scored:
                continue
            score, domain, meta = scored
            add_or_merge_lead(
                url=repo["html_url"],
                name=full_name,
                domain=domain,
                signal_type="DOCS_CONFIG_ADDED",
                detail=f"[{framework}] ({meta}) — {repo.get('description') or 'No description.'}",
                score=score,
            )

# 2. GitHub: High-Intent Open Documentation Issues
def scan_github_issues():
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    query = f'is:issue is:open label:documentation created:>{since} ("quickstart" OR "API docs" OR "documentation site" OR "getting started" OR "rewrite docs")'
    url = f"https://api.github.com/search/issues?q={requests.utils.quote(query)}&sort=created&order=desc&per_page=20"
    res = requests.get(url, headers=GH_HEADERS, timeout=15)
    seen_issue_repos = set()

    if res.status_code == 200:
        for issue in res.json().get("items", []):
            repo_api_url = issue["repository_url"]
            if repo_api_url in seen_issue_repos:
                continue
            seen_issue_repos.add(repo_api_url)

            repo_res = requests.get(repo_api_url, headers=GH_HEADERS, timeout=10)
            if repo_res.status_code != 200:
                continue
            repo = repo_res.json()
            scored = score_github_repo(repo, base_score=20)
            if not scored:
                continue
            score, domain, meta = scored
            add_or_merge_lead(
                url=issue["html_url"],
                name=repo["full_name"],
                domain=domain,
                signal_type="DOC_ISSUE_OPEN",
                detail=f"({meta}) Issue: {issue['title']}",
                score=score,
            )

# 3. Hacker News: Show HN DevTools + DNS CNAME & HTTP Probe
def scan_hacker_news():
    ids_res = requests.get("https://hacker-news.firebaseio.com/v0/newstories.json", timeout=10)
    if ids_res.status_code != 200:
        return
    dev_keywords = re.compile(r"\b(api|sdk|cli|mcp|agent|deploy|database|postgres|kubernetes|open[- ]source|developer|cloud|llm|infra|workflow|auth)\b", re.I)

    for story_id in ids_res.json()[:80]:
        item = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", timeout=10).json()
        if not item or not item.get("title", "").startswith("Show HN:") or not item.get("url"):
            continue

        title = item["title"]
        if not dev_keywords.search(title):
            continue

        domain = extract_domain(item["url"])
        score = 25
        notes = []

        # Bonus Signal: Check DNS CNAME first, fall back to HTTP probe
        dns_hit = detect_docs_platform_via_dns(domain)
        if dns_hit:
            score += 35
            notes.append(f"DNS Match: {dns_hit}")
        elif not is_ignored_domain(domain):
            for probe in [f"https://docs.{domain}", f"https://{domain}/docs"]:
                try:
                    head = requests.head(probe, timeout=4, allow_redirects=True)
                    if head.status_code == 200:
                        score += 20
                        notes.append(f"Live docs at {probe}")
                        break
                except requests.RequestException:
                    pass

        detail = title if not notes else f"{title} ({' • '.join(notes)})"
        add_or_merge_lead(
            url=item["url"],
            name=title.replace("Show HN:", "").strip()[:65],
            domain=domain,
            signal_type="SHOW_HN_DEVTOOL",
            detail=detail,
            score=score,
        )

# 4. Reddit: Startup & SaaS Founders Asking About Docs
def scan_reddit():
    subreddits = ["startups", "SaaS", "technicalwriting", "devtools"]
    keywords = re.compile(r"\b(documentation|api docs|tech writer|technical writer|mintlify|docusaurus|gitbook|readme|fern)\b", re.I)
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/new.json?limit=30"
        res = requests.get(url, headers=USER_AGENT, timeout=10)
        if res.status_code == 200:
            posts = res.json().get("data", {}).get("children", [])
            for post in posts:
                data = post["data"]
                text = f"{data.get('title', '')} {data.get('selftext', '')}"
                if keywords.search(text):
                    add_or_merge_lead(
                        url=f"https://www.reddit.com{data['permalink']}",
                        name=f"r/{sub}: {data['title'][:55]}",
                        domain="",
                        signal_type="REDDIT_MENTION",
                        detail=data["title"],
                        score=30,
                    )
        time.sleep(2)

if __name__ == "__main__":
    scan_github_configs()
    scan_github_issues()
    scan_hacker_news()
    scan_reddit()

    sorted_leads = sorted(leads_by_url.values(), key=lambda x: x["score"], reverse=True)

    output = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "leads": sorted_leads,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/signals.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Saved {len(sorted_leads)} high-signal leads to data/signals.json")
