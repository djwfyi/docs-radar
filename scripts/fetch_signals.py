#!/usr/bin/env python3
"""
Docs Opportunity Scout — Hugo Data Pipeline (scripts/fetch_signals.py)
Writes normalized leads with persistent historical tracking (data/seen.json),
encounter counts (seen_count), star velocity (star_delta), cross-signal deduplication,
docs platform classification, and score breakdown tooltips to data/signals.json.
"""

import argparse
import datetime
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

try:
    import dns.resolver
    HAS_DNSPYTHON = True
except ImportError:
    HAS_DNSPYTHON = False

# ---------------------------------------------------------------------------
# Configuration & Filters
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
HEADERS = {
    "User-Agent": "DocsOpportunityScout/3.1",
    "Accept": "application/vnd.github+json",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

ORG_BLOCKLIST = {
    "datadog", "oracle", "pingcap", "mozilla", "vercel", "supabase",
    "netlify", "vuejs", "swagger-api", "microsoft", "google", "aws",
    "amazon", "meta", "facebook", "apple", "ibm", "redhat", "elastic",
    "hashicorp", "cloudflare", "stripe", "twilio", "auth0", "okta",
    "shopify", "gitlab", "github", "jetbrains", "atlassian", "grafana",
    "prometheus", "kubernetes", "cncf", "apache", "eclipse", "linux",
}

NOISE_PATTERNS = re.compile(
    r"(^awesome[-_]|[-_]awesome$|\bawesome\b|\binterview[-_ ]?qa\b|"
    r"\bpersonal[-_ ]?blog\b|\bvitepress-blog\b|\bmy[-_ ]?blog\b|"
    r"\bstudy[-_ ]?notes\b|\bbook[-_ ]?notes\b|\bcheat[-_ ]?sheet\b|"
    r"\btutorial[-_ ]?list\b|\bcurated[-_ ]?list\b)",
    re.IGNORECASE,
)

PARKING_CNAME_FRAGMENTS = (
    "porkbun.com",
    "namebright.com",
    "godaddy.com",
    "secureserver.net",
    "namecheap.com",
    "registrar-servers.com",
    "sedoparking.com",
    "bodis.com",
    "hugedomains.com",
    "dan.com",
    "afternic.com",
    "parkingcrew.net",
)

CNAME_PLATFORM_MAP = {
    "mintlify.app": "Mintlify",
    "mintlify.dev": "Mintlify",
    "redoc.ly": "Redocly",
    "redocly.app": "Redocly",
    "redocly.com": "Redocly",
    "gitbook.io": "GitBook",
    "readme.io": "ReadMe",
    "stoplight.io": "Stoplight",
    "fern.build": "Fern",
    "buildwithfern.com": "Fern",
    "docusaurus": "Docusaurus",
    "readthedocs.io": "Read the Docs",
    "readthedocs.org": "Read the Docs",
    "github.io": "GitHub Pages",
    "pages.dev": "Cloudflare Pages",
    "netlify.app": "Netlify",
    "vercel.app": "Vercel",
    "fly.dev": "Fly.io",
    "render.com": "Render",
    "railway.app": "Railway",
}

TEXT_PLATFORM_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bmintlify\b|mint\.json", re.I), "Mintlify"),
    (re.compile(r"\bredocly\b|\bredoc\b|redocly\.ya?ml", re.I), "Redocly"),
    (re.compile(r"\bdocusaurus\b", re.I), "Docusaurus"),
    (re.compile(r"\bstarlight\b|@astrojs/starlight", re.I), "Astro / Starlight"),
    (re.compile(r"\bvitepress\b", re.I), "VitePress"),
    (re.compile(r"\bfumadocs\b", re.I), "Fumadocs"),
    (re.compile(r"\bmkdocs\b", re.I), "MkDocs"),
    (re.compile(r"\bgitbook\b", re.I), "GitBook"),
    (re.compile(r"\breadme\.io\b|\breadme\.com\b", re.I), "ReadMe"),
    (re.compile(r"\bstoplight\b", re.I), "Stoplight"),
    (re.compile(r"\bbuildwithfern\b|\bfern\s+docs\b", re.I), "Fern"),
    (re.compile(r"\bhugo\b|hugo\.toml", re.I), "Hugo"),
    (re.compile(r"\bantora\b", re.I), "Antora"),
    (re.compile(r"\bsphinx\b|readthedocs", re.I), "Read the Docs"),
    (re.compile(r"\bllms\.txt\b", re.I), "AI / llms.txt"),
    (re.compile(r"\b\.vale\.ini\b|\bvale\s+linter\b", re.I), "Vale Linter"),
]

CI_ISSUE_NOISE = re.compile(
    r"(cloudflare pages|netlify preview|vercel preview|github actions|"
    r"deploy preview|broken workflow|ci pipeline|exdoc together)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Domain, DNS & Platform Detection Helpers
# ---------------------------------------------------------------------------

def infer_platform_from_text(text: str) -> Optional[str]:
    """Detect canonical docs platform from issue titles, descriptions, or HTML."""
    if not text:
        return None
    for pattern, platform in TEXT_PLATFORM_PATTERNS:
        if pattern.search(text):
            return platform
    return None


def extract_root_domain(raw_url: str) -> Optional[str]:
    """Normalize a URL and strip common subdomains to yield the apex domain."""
    if not raw_url:
        return None
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
    try:
        host = urlparse(raw_url).netloc.lower().split(":")[0]
    except Exception:
        return None

    if not host or "." not in host:
        return None

    ignored_roots = (
        "github.com", "gitlab.com", "bitbucket.org", "npmjs.com",
        "pypi.org", "crates.io", "x.com", "twitter.com", "linkedin.com",
        "youtube.com", "ycombinator.com", "discord.gg", "discord.com",
        "vercel.app", "netlify.app", "github.io", "pages.dev",
    )
    if any(host == d or host.endswith("." + d) for d in ignored_roots):
        return None

    prefixes = ("www.", "docs.", "doc.", "developer.", "developers.", "api.", "app.", "help.")
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if host.startswith(p) and host.count(".") >= 2:
                host = host[len(p):]
                changed = True

    return host


def extract_github_repo_from_url(raw_url: str) -> Optional[str]:
    """Extract 'owner/repo' if a URL points directly to a GitHub repository."""
    if not raw_url:
        return None
    try:
        parsed = urlparse(raw_url)
        if parsed.netloc.lower() not in ("github.com", "www.github.com"):
            return None
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) >= 2:
            owner, repo = parts[0].lower(), parts[1].lower()
            if owner not in ("topics", "explore", "marketplace", "orgs", "users", "search"):
                return f"{owner}/{repo}"
    except Exception:
        pass
    return None


def resolve_cname(hostname: str) -> Optional[str]:
    """Return CNAME target for hostname if one exists."""
    if HAS_DNSPYTHON:
        try:
            answers = dns.resolver.resolve(hostname, "CNAME", lifetime=2.5)
            for rdata in answers:
                return str(rdata.target).rstrip(".").lower()
        except Exception:
            return None
    else:
        try:
            canon, _, _ = socket.gethostbyname_ex(hostname)
            canon = canon.lower().rstrip(".")
            if canon and canon != hostname.lower():
                return canon
        except Exception:
            return None
    return None


def detect_platform_from_html(snippet: str) -> str:
    """Infer doc generator from lightweight HTML markers."""
    inferred = infer_platform_from_text(snippet)
    return inferred if inferred else "Custom / Other"


def probe_docs_infrastructure(root_domain: Optional[str]) -> Tuple[str, str, bool]:
    """
    Check docs-specific subdomains and /docs path on root_domain.
    Returns: (detected_platform, evidence_note, has_active_docs)
    """
    if not root_domain:
        return ("Greenfield / None", "No custom domain listed", False)

    subdomains = [
        f"docs.{root_domain}",
        f"developer.{root_domain}",
        f"developers.{root_domain}",
        f"help.{root_domain}",
    ]

    for sub in subdomains:
        cname = resolve_cname(sub)
        if not cname:
            continue

        if any(park in cname for park in PARKING_CNAME_FRAGMENTS):
            return (
                "Greenfield / None",
                f"{sub} points to registrar parking ({cname}) — no docs site yet",
                False,
            )

        for sig, plat in CNAME_PLATFORM_MAP.items():
            if sig in cname:
                return (
                    plat,
                    f"Active {sub} CNAME -> {cname}",
                    True,
                )

        try:
            get_resp = requests.get(f"https://{sub}", timeout=3, headers={"User-Agent": HEADERS["User-Agent"]})
            if get_resp.status_code == 200:
                plat = detect_platform_from_html(get_resp.text[:5000])
                return (plat, f"Active {sub} CNAME -> {cname}", True)
        except Exception:
            pass

        return (
            "Custom / Other",
            f"Active {sub} CNAME -> {cname}",
            True,
        )

    try:
        resp = requests.head(
            f"https://{root_domain}/docs",
            timeout=3,
            allow_redirects=True,
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
        if resp.status_code == 200 and "/docs" in resp.url:
            body_snippet = ""
            try:
                get_resp = requests.get(resp.url, timeout=3, headers={"User-Agent": HEADERS["User-Agent"]})
                body_snippet = get_resp.text[:5000]
            except Exception:
                pass

            platform = detect_platform_from_html(body_snippet)
            return (platform, f"Live /docs endpoint at {resp.url}", True)
    except Exception:
        pass

    return ("Greenfield / None", f"No docs subdomain or /docs route detected on {root_domain}", False)


def probe_github_repo_platform(full_name: str) -> Optional[str]:
    """Inspect top-level files of a GitHub repo for docs framework configs."""
    try:
        r = requests.get(f"https://api.github.com/repos/{full_name}/contents", headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return None
        names = " ".join(item.get("name", "") for item in r.json() if isinstance(item, dict))
        return infer_platform_from_text(names)
    except Exception:
        return None


def fetch_github_repo_meta(full_name: str) -> Optional[dict]:
    """Fetch repository metadata dict from GitHub API."""
    try:
        r = requests.get(f"https://api.github.com/repos/{full_name}", headers=HEADERS, timeout=6)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# GitHub Filtering & Traction Gates
# ---------------------------------------------------------------------------

def is_early_stage_repo(repo_data: dict) -> bool:
    """Filter out enterprise giants, personal blogs, awesome lists, and stale repos."""
    full_name = repo_data.get("full_name", "")
    owner = full_name.split("/")[0].lower() if "/" in full_name else ""
    description = repo_data.get("description") or ""

    if owner in ORG_BLOCKLIST:
        return False
    if repo_data.get("fork") or repo_data.get("archived"):
        return False
    if NOISE_PATTERNS.search(full_name) or NOISE_PATTERNS.search(description):
        return False

    created_str = repo_data.get("created_at", "")
    if created_str:
        try:
            created_dt = datetime.datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            age_days = (datetime.datetime.now(datetime.timezone.utc) - created_dt).days
            if age_days > 365 * 7:
                return False
        except Exception:
            pass

    return True


def passes_traction_gate(repo_data: dict, min_user_stars: int = 15, min_org_stars: int = 5) -> bool:
    """
    Post-fetch traction gate to eliminate 0-star personal scratchpads:
    - Personal user repos require >= min_user_stars (default 15).
    - Organization repos require >= min_org_stars (default 5).
    """
    stars = repo_data.get("stargazers_count", 0)
    owner_type = repo_data.get("owner", {}).get("type", "User")
    if owner_type == "Organization":
        return stars >= min_org_stars
    return stars >= min_user_stars


def check_org_size_ok(owner_login: str, cache: dict) -> bool:
    """Exclude organizations with > 60 public repositories (mature enterprises)."""
    key = owner_login.lower()
    if key in cache:
        return cache[key]
    if key in ORG_BLOCKLIST:
        cache[key] = False
        return False

    try:
        r = requests.get(f"https://api.github.com/users/{owner_login}", headers=HEADERS, timeout=5)
        if r.status_code == 200:
            public_repos = r.json().get("public_repos", 0)
            ok = public_repos <= 60
            cache[key] = ok
            return ok
    except Exception:
        pass

    cache[key] = True
    return True


# ---------------------------------------------------------------------------
# Collectors (Rebalanced Scoring: Base + Traction + Docs Opportunity)
# ---------------------------------------------------------------------------

def collect_github_docs_configs(days_back: int = 14) -> List[dict]:
    """Search GitHub code for newly added documentation framework configs."""
    leads: List[dict] = []
    seen_repos = set()
    org_cache: Dict[str, bool] = {}
    since_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")

    queries = [
        ("mint.json", "Mintlify", "core"),
        ("docs.json mintlify", "Mintlify", "core"),
        ("redocly.yaml", "Redocly", "core"),
        ("docusaurus.config", "Docusaurus", "core"),
        ("mkdocs.yml", "MkDocs", "core"),
        ("vitepress", "VitePress", "core"),
        ("@astrojs/starlight", "Astro / Starlight", "core"),
        ("fumadocs", "Fumadocs", "core"),
        ("antora-playbook.yml", "Antora", "core"),
        ("hugo.toml", "Hugo", "core"),
        ("filename:llms.txt", "AI / llms.txt", "aux"),
        ("filename:.vale.ini", "Vale Linter", "aux"),
    ]

    for q_term, default_platform, tier in queries:
        url = "https://api.github.com/search/code"
        params = {"q": f"{q_term} pushed:>{since_date}", "per_page": 25}
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
            if resp.status_code != 200:
                continue
            items = resp.json().get("items", [])
        except Exception:
            continue

        for item in items:
            repo = item.get("repository", {})
            full_name = repo.get("full_name")
            if not full_name or full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            meta = fetch_github_repo_meta(full_name)
            if not meta or not is_early_stage_repo(meta):
                continue
            if not passes_traction_gate(meta, min_user_stars=25, min_org_stars=10):
                continue

            owner_login = meta.get("owner", {}).get("login", "")
            if not check_org_size_ok(owner_login, org_cache):
                continue

            stars = meta.get("stargazers_count", 0)
            file_path = item.get("path", "")
            homepage = meta.get("homepage") or ""
            root_domain = extract_root_domain(homepage)

            parts = []
            if tier == "core":
                score = 50
                parts.append(f"50 base ({default_platform} config)")
                if stars >= 1000:
                    score += 25
                    parts.append(f"+25 high traction ({stars}★)")
                elif stars >= 100:
                    score += 20
                    parts.append(f"+20 traction ({stars}★)")
                elif stars >= 25:
                    score += 10
                    parts.append(f"+10 early traction ({stars}★)")
                if root_domain:
                    score += 15
                    parts.append("+15 custom domain")
            else:
                score = 35
                parts.append(f"35 base ({default_platform} config)")
                if stars >= 250:
                    score += 20
                    parts.append(f"+20 traction ({stars}★)")
                elif stars >= 25:
                    score += 10
                    parts.append(f"+10 early traction ({stars}★)")
                if root_domain:
                    score += 15
                    parts.append("+15 custom domain")

            desc = (meta.get("description") or "").strip()
            detail = f"Detected `{file_path}` ({stars}★)."
            if desc:
                detail += f" {desc}"

            leads.append({
                "name": full_name,
                "domain": root_domain or "",
                "repo": full_name.lower(),
                "stars": stars,
                "url": meta.get("html_url", f"https://github.com/{full_name}"),
                "signal_type": "DOCS_CONFIG_ADDED",
                "category": "tooling_trend",
                "platform": default_platform,
                "score": min(score, 115),
                "score_breakdown": " · ".join(parts),
                "detail": detail[:260],
            })
            time.sleep(0.2)

    return leads


def collect_github_doc_issues(days_back: int = 14) -> List[dict]:
    """
    Find open issues explicitly asking for docs help or flagging doc drift.
    Note: GitHub /search/issues ignores `stars:`, so we filter by labels / title
    at query time and enforce `passes_traction_gate()` after fetching repo metadata.
    """
    leads: List[dict] = []
    seen_repos = set()
    org_cache: Dict[str, bool] = {}
    since_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")

    bot_exclusions = "-author:app/dependabot -author:app/renovate -author:app/github-actions"
    search_queries = [
        f'is:issue is:open label:documentation,docs created:>{since_date} {bot_exclusions}',
        f'is:issue is:open ("missing docs" OR "add documentation" OR "update docs" OR "doc drift" OR "getting started guide") in:title created:>{since_date} {bot_exclusions}',
    ]

    for q in search_queries:
        try:
            resp = requests.get(
                "https://api.github.com/search/issues",
                headers=HEADERS,
                params={"q": q, "sort": "created", "order": "desc", "per_page": 40},
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            items = resp.json().get("items", [])
        except Exception:
            continue

        for issue in items:
            title = issue.get("title", "")
            body = issue.get("body") or ""
            if CI_ISSUE_NOISE.search(title):
                continue

            repo_url = issue.get("repository_url", "")
            full_name = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
            if not full_name or full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            meta = fetch_github_repo_meta(full_name)
            if not meta or not is_early_stage_repo(meta):
                continue
            if not passes_traction_gate(meta, min_user_stars=15, min_org_stars=5):
                continue

            owner_login = meta.get("owner", {}).get("login", "")
            if not check_org_size_ok(owner_login, org_cache):
                continue

            stars = meta.get("stargazers_count", 0)
            issue_author = (issue.get("user") or {}).get("login", "").lower()
            # Drop self-filed TODO issues on low-traction personal repos
            if issue_author == owner_login.lower() and stars < 25:
                continue

            root_domain = extract_root_domain(meta.get("homepage") or "")
            platform, _, has_docs = probe_docs_infrastructure(root_domain) if root_domain else ("Greenfield / None", "", False)

            if platform in ("Greenfield / None", "Custom / Other"):
                text_plat = infer_platform_from_text(f"{title} {body[:1000]} {meta.get('description') or ''}")
                if text_plat:
                    platform = text_plat
                    has_docs = True
                else:
                    repo_plat = probe_github_repo_platform(full_name)
                    if repo_plat:
                        platform = repo_plat
                        has_docs = True

            # Rebalanced Issue Scoring: 40 base + traction + contributor/docs opportunity
            score = 40
            parts = ["40 base (open docs/drift issue)"]

            if stars >= 1000:
                score += 30
                parts.append(f"+30 high traction ({stars}★)")
            elif stars >= 100:
                score += 20
                parts.append(f"+20 traction ({stars}★)")
            elif stars >= 15:
                score += 10
                parts.append(f"+10 early traction ({stars}★)")

            label_names = {lbl.get("name", "").lower() for lbl in issue.get("labels", []) if isinstance(lbl, dict)}
            if label_names & {"help wanted", "good first issue", "contributions welcome"}:
                score += 15
                parts.append("+15 help wanted")

            if has_docs or root_domain:
                score += 10
                parts.append(f"+10 live site / docs ({platform})")

            leads.append({
                "name": full_name,
                "domain": root_domain or "",
                "repo": full_name.lower(),
                "stars": stars,
                "url": issue.get("html_url", ""),
                "signal_type": "DOC_ISSUE_OPEN",
                "category": "outreach_lead",
                "platform": platform,
                "score": min(score, 115),
                "score_breakdown": " · ".join(parts),
                "detail": f"Open issue ({stars}★): {title}",
            })
            time.sleep(0.2)

    return leads


def collect_show_hn_devtools(days_back: int = 14) -> List[dict]:
    """Pull recent Show HN launches via Algolia HN API and inspect their docs."""
    leads: List[dict] = []
    cutoff_ts = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_back)).timestamp())

    try:
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "tags": "show_hn",
                "numericFilters": f"created_at_i>{cutoff_ts},points>15",
                "hitsPerPage": 50,
            },
            timeout=10,
        )
        hits = resp.json().get("hits", []) if resp.status_code == 200 else []
    except Exception:
        hits = []

    dev_keywords = re.compile(
        r"\b(api|sdk|cli|open[- ]source|developer|devtools|database|kubernetes|"
        r"postgres|llm|agent|infrastructure|framework|compiler|runtime|cloud)\b",
        re.IGNORECASE,
    )

    for hit in hits:
        title = hit.get("title", "")
        story_url = hit.get("url") or ""
        if not story_url or not dev_keywords.search(title):
            continue

        repo_slug = extract_github_repo_from_url(story_url)
        root_domain = extract_root_domain(story_url)
        stars = 0

        if repo_slug:
            meta = fetch_github_repo_meta(repo_slug)
            if meta:
                stars = meta.get("stargazers_count", 0)
                if not root_domain:
                    root_domain = extract_root_domain(meta.get("homepage") or "")

        platform, evidence, has_docs = probe_docs_infrastructure(root_domain)

        # Rebalanced Show HN Scoring: 45 base + HN traction + docs opportunity
        score = 45
        parts = ["45 base (Show HN devtool)"]

        points = hit.get("points", 0)
        if points >= 200:
            score += 35
            parts.append(f"+35 viral HN traction ({points} pts)")
        elif points >= 75:
            score += 25
            parts.append(f"+25 strong HN traction ({points} pts)")
        elif points >= 25:
            score += 15
            parts.append(f"+15 HN traction ({points} pts)")

        if not has_docs:
            score += 20
            parts.append("+20 greenfield docs opportunity")
        else:
            score += 15
            parts.append(f"+15 active {platform} docs")

        clean_name = title.replace("Show HN:", "").strip()
        leads.append({
            "name": clean_name,
            "domain": root_domain or "",
            "repo": repo_slug or "",
            "stars": stars,
            "url": story_url,
            "signal_type": "SHOW_HN_DEVTOOL",
            "category": "outreach_lead",
            "platform": platform,
            "score": min(score, 115),
            "score_breakdown": " · ".join(parts),
            "detail": f"Show HN ({points} pts) — {evidence}",
        })

    return leads


def collect_yc_companies(limit: int = 150) -> List[dict]:
    """Fetch recent Y Combinator companies from per-batch yc-oss API endpoints."""
    leads: List[dict] = []

    # Query the most recent YC batches directly instead of downloading all ~5,700 companies
    batch_endpoints = [
        ("Fall 2026", "https://yc-oss.github.io/api/batches/fall-2026.json"),
        ("Summer 2026", "https://yc-oss.github.io/api/batches/summer-2026.json"),
        ("Spring 2026", "https://yc-oss.github.io/api/batches/spring-2026.json"),
        ("Winter 2026", "https://yc-oss.github.io/api/batches/winter-2026.json"),
        ("Fall 2025", "https://yc-oss.github.io/api/batches/fall-2025.json"),
        ("Summer 2025", "https://yc-oss.github.io/api/batches/summer-2025.json"),
    ]

    target_keywords = (
        "developer", "b2b", "infrastructure", "ai", "api",
        "cloud", "security", "data", "open source", "devtools",
    )

    companies: List[dict] = []
    for batch_label, url in batch_endpoints:
        try:
            resp = requests.get(url, timeout=12, headers={"User-Agent": HEADERS["User-Agent"]})
            if resp.status_code != 200:
                print(f"[!] YC batch fetch failed ({batch_label}): HTTP {resp.status_code}", file=sys.stderr)
                continue
            batch_items = resp.json()
            if isinstance(batch_items, list):
                for item in batch_items:
                    item.setdefault("_batch_label", batch_label)
                companies.extend(batch_items)
        except Exception as exc:
            print(f"[!] YC batch fetch error ({batch_label}): {exc}", file=sys.stderr)

    filtered = []
    for c in companies:
        if c.get("status") not in (None, "", "Active"):
            continue
        industry = str(c.get("industry") or "")
        subindustry = str(c.get("subindustry") or "")
        tags = " ".join(str(t) for t in (c.get("tags") or []))
        one_liner = str(c.get("one_liner") or "")
        searchable = f"{industry} {subindustry} {tags} {one_liner}".lower()

        if any(kw in searchable for kw in target_keywords):
            filtered.append(c)

    print(f"    -> Matched {len(filtered)} relevant YC companies (probing top {min(len(filtered), limit)})")

    for comp in filtered[:limit]:
        name = comp.get("name", "Unknown")
        website = comp.get("website") or ""
        batch = comp.get("batch") or comp.get("_batch_label") or ""
        one_liner = (comp.get("one_liner") or "").strip()
        root_domain = extract_root_domain(website)

        platform, evidence, has_docs = probe_docs_infrastructure(root_domain)

        score = 50
        parts = [f"50 base (YC {batch})"]

        if not has_docs and root_domain:
            score += 20
            parts.append("+20 greenfield docs opportunity")
        elif has_docs:
            score += 15
            parts.append(f"+15 active {platform} docs")

        detail = f"YC {batch}: {one_liner}" if one_liner else f"YC {batch}"
        detail += f" ({evidence})"

        leads.append({
            "name": f"{name} ({batch})",
            "domain": root_domain or "",
            "repo": "",
            "stars": 0,
            "url": website or f"https://www.ycombinator.com/companies/{comp.get('slug', '')}",
            "signal_type": "YC_BATCH",
            "category": "outreach_lead",
            "platform": platform,
            "score": min(score, 115),
            "score_breakdown": " · ".join(parts),
            "detail": detail,
        })

    return leads


# ---------------------------------------------------------------------------
# State Persistence, seen.json Tracking & Persistence Scoring
# ---------------------------------------------------------------------------

def canonical_lead_key(lead: dict) -> str:
    """Group multiple signals for the same startup/project by domain, repo, or URL."""
    domain = (lead.get("domain") or "").strip().lower()
    if domain:
        return f"domain:{domain}"
    repo = (lead.get("repo") or "").strip().lower()
    if repo:
        return f"repo:{repo}"
    url = (lead.get("url") or lead.get("name") or "").rstrip("/").lower()
    return f"url:{url}"


def load_seen_state(seen_path: Path, fallback_data_path: Path) -> Dict[str, dict]:
    """
    Load historical encounter state from data/seen.json.
    Falls back to bootstrapping from data/signals.json on first migration.
    """
    if seen_path.exists():
        try:
            raw = json.loads(seen_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "items" in raw:
                return raw["items"]
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass

    bootstrapped: Dict[str, dict] = {}
    if fallback_data_path.exists():
        try:
            existing = json.loads(fallback_data_path.read_text(encoding="utf-8"))
            for item in existing.get("leads", []):
                k = canonical_lead_key(item)
                if k:
                    bootstrapped[k] = {
                        "name": item.get("name", ""),
                        "url": item.get("url", ""),
                        "first_seen": item.get("first_seen") or datetime.date.today().isoformat(),
                        "last_seen": item.get("first_seen") or datetime.date.today().isoformat(),
                        "seen_count": int(item.get("seen_count") or 1),
                        "first_seen_stars": int(item.get("first_seen_stars") or item.get("stars") or 0),
                        "last_stars": int(item.get("stars") or 0),
                        "last_score": int(item.get("score") or 0),
                        "platform": item.get("platform") or "Greenfield / None",
                        "signal_types": [item.get("signal_type")] if item.get("signal_type") else [],
                    }
        except Exception:
            pass
    return bootstrapped


def save_seen_state(seen_items: Dict[str, dict], seen_path: Path, now_str: str) -> None:
    """Persist cumulative encounter history to data/seen.json."""
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": now_str,
        "total_tracked": len(seen_items),
        "items": seen_items,
    }
    seen_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def merge_with_existing_state(
    new_leads: List[dict],
    data_path: Path,
    seen_path: Path,
) -> dict:
    today_str = datetime.date.today().isoformat()
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    seen_items = load_seen_state(seen_path, fallback_data_path=data_path)

    deduped: Dict[str, dict] = {}
    signal_types_by_key: Dict[str, set] = {}

    for lead in new_leads:
        k = canonical_lead_key(lead)
        if not k or k == "url:":
            continue

        signal_types_by_key.setdefault(k, set()).add(lead.get("signal_type", ""))

        if k not in deduped:
            deduped[k] = lead
        else:
            existing_lead = deduped[k]
            if lead["score"] > existing_lead["score"]:
                primary, secondary = lead, existing_lead
            else:
                primary, secondary = existing_lead, lead

            if primary.get("platform") in ("", "Greenfield / None", "Custom / Other") and secondary.get("platform") not in ("", "Greenfield / None"):
                primary["platform"] = secondary["platform"]
            if secondary.get("stars", 0) > primary.get("stars", 0):
                primary["stars"] = secondary["stars"]
            if not primary.get("repo") and secondary.get("repo"):
                primary["repo"] = secondary["repo"]

            deduped[k] = primary

    merged_leads: List[dict] = []
    new_count = 0

    for k, lead in deduped.items():
        distinct_signals = {s for s in signal_types_by_key.get(k, set()) if s}
        if len(distinct_signals) > 1:
            lead["score"] = min(115, lead["score"] + 10)
            lead["score_breakdown"] += f" · +10 multi-signal ({', '.join(sorted(distinct_signals))})"

        if lead["platform"] == "Greenfield / None":
            inferred = infer_platform_from_text(lead.get("detail", ""))
            if inferred:
                lead["platform"] = inferred

        prev = seen_items.get(k)
        current_stars = int(lead.get("stars") or 0)

        if prev is None:
            seen_count = 1
            first_seen = today_str
            baseline_stars = current_stars
            star_delta = 0
            lead["is_new"] = True
            new_count += 1
        else:
            seen_count = int(prev.get("seen_count") or 1) + 1
            first_seen = prev.get("first_seen") or today_str
            baseline_stars = int(prev.get("first_seen_stars") or prev.get("last_stars") or current_stars)
            star_delta = max(0, current_stars - baseline_stars) if current_stars > 0 else 0
            lead["is_new"] = False

            if lead["platform"] == "Greenfield / None" and prev.get("platform") not in (None, "", "Greenfield / None"):
                lead["platform"] = prev["platform"]

            # Merge historical signal types so multi-run cross-signals are remembered
            historical_signals = set(prev.get("signal_types") or [])
            combined_signals = distinct_signals | historical_signals
            if len(combined_signals) > len(distinct_signals) and len(combined_signals) > 1:
                lead["score"] = min(115, lead["score"] + 10)
                lead["score_breakdown"] += f" · +10 historical multi-signal ({', '.join(sorted(combined_signals))})"

        # Apply encounter-count influence to score
        if seen_count >= 10 and star_delta == 0:
            lead["score"] = max(20, lead["score"] - 10)
            lead["score_breakdown"] += f" · -10 stale ({seen_count}x seen, 0★ gained)"
        elif seen_count >= 5:
            lead["score"] = min(115, lead["score"] + 10)
            lead["score_breakdown"] += f" · +10 persistent ({seen_count}x seen)"
        elif seen_count >= 3:
            lead["score"] = min(115, lead["score"] + 5)
            lead["score_breakdown"] += f" · +5 recurring ({seen_count}x seen)"

        # Star velocity bonus when a repo is actively gaining stars across runs
        if star_delta >= 25:
            lead["score"] = min(115, lead["score"] + 10)
            lead["score_breakdown"] += f" · +10 fast star growth (+{star_delta}★)"
        elif star_delta >= 10:
            lead["score"] = min(115, lead["score"] + 5)
            lead["score_breakdown"] += f" · +5 rising stars (+{star_delta}★)"

        prev_score = int(prev.get("last_score", lead["score"])) if prev else lead["score"]
        lead["score_delta"] = max(0, lead["score"] - prev_score) if prev else 0
        lead["first_seen"] = first_seen
        lead["last_seen"] = today_str
        lead["seen_count"] = seen_count
        lead["first_seen_stars"] = baseline_stars
        lead["star_delta"] = star_delta

        # Update cumulative seen.json record
        all_recorded_signals = sorted(
            distinct_signals | set((prev or {}).get("signal_types") or [])
        )
        seen_items[k] = {
            "name": lead.get("name", ""),
            "url": lead.get("url", ""),
            "first_seen": first_seen,
            "last_seen": today_str,
            "seen_count": seen_count,
            "first_seen_stars": baseline_stars,
            "last_stars": current_stars,
            "last_score": lead["score"],
            "platform": lead.get("platform", "Greenfield / None"),
            "signal_types": all_recorded_signals,
        }

        merged_leads.append(lead)

    save_seen_state(seen_items, seen_path, now_str)

    merged_leads.sort(key=lambda x: (x["score"], x.get("star_delta", 0), x["is_new"]), reverse=True)

    return {
        "updated_at": now_str,
        "new_count": new_count,
        "total_seen_history": len(seen_items),
        "leads": merged_leads,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch documentation opportunity signals for Hugo.")
    parser.add_argument("--days", type=int, default=14, help="Lookback window in days (default: 14)")
    parser.add_argument("--yc-limit", type=int, default=150, help="Max recent YC companies to probe (default: 150)")
    parser.add_argument("--out", default="data/signals.json", help="Output JSON path (default: data/signals.json)")
    parser.add_argument("--seen", default="data/seen.json", help="Persistent seen state JSON path (default: data/seen.json)")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("[!] Warning: GITHUB_TOKEN not set. GitHub code search may return limited results.", file=sys.stderr)

    collected: List[dict] = []

    print("[1/4] Scanning GitHub for newly added docs configs...")
    collected.extend(collect_github_docs_configs(days_back=args.days))

    print("[2/4] Scanning GitHub for open documentation & drift issues...")
    collected.extend(collect_github_doc_issues(days_back=args.days))

    print("[3/4] Scanning Show HN devtool launches...")
    collected.extend(collect_show_hn_devtools(days_back=args.days))

    print("[4/4] Probing recent YC companies for docs infrastructure...")
    collected.extend(collect_yc_companies(limit=args.yc_limit))

    out_path = Path(args.out)
    seen_path = Path(args.seen)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = merge_with_existing_state(collected, out_path, seen_path)

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[✓] Saved {len(payload['leads'])} leads ({payload['new_count']} new; {payload['total_seen_history']} total seen) to {out_path}")


if __name__ == "__main__":
    main()
