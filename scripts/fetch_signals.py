#!/usr/bin/env python3
"""
Docs Opportunity Scout — Hugo Data Pipeline (scripts/fetch_signals.py)
Writes normalized leads with historical tracking (is_new, score_delta, first_seen),
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
    "User-Agent": "DocsOpportunityScout/2.2",
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

CI_ISSUE_NOISE = re.compile(
    r"(cloudflare pages|netlify preview|vercel preview|github actions|"
    r"deploy preview|broken workflow|ci pipeline|exdoc together)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Domain & DNS Helpers
# ---------------------------------------------------------------------------

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
    if not snippet:
        return "Custom / Other"
    if "mintlify" in snippet:
        return "Mintlify"
    if "docusaurus" in snippet:
        return "Docusaurus"
    if "starlight" in snippet or "astro" in snippet:
        return "Astro / Starlight"
    if "vitepress" in snippet:
        return "VitePress"
    if "fumadocs" in snippet:
        return "Fumadocs"
    if "mkdocs" in snippet:
        return "MkDocs"
    if "gitbook" in snippet:
        return "GitBook"
    if "readme" in snippet:
        return "ReadMe"
    if "hugo" in snippet:
        return "Hugo"
    return "Custom / Other"


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
                body_snippet = get_resp.text[:4000].lower()
            except Exception:
                pass

            platform = detect_platform_from_html(body_snippet)
            return (platform, f"Live /docs endpoint at {resp.url}", True)
    except Exception:
        pass

    return ("Greenfield / None", f"No docs subdomain or /docs route detected on {root_domain}", False)


# ---------------------------------------------------------------------------
# GitHub Filtering & Collectors
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


def collect_github_docs_configs(days_back: int = 14) -> List[dict]:
    """Search GitHub code for newly added documentation framework configs."""
    leads: List[dict] = []
    seen_repos = set()
    org_cache: Dict[str, bool] = {}
    since_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")

    queries = [
        ("mint.json", "Mintlify", "core"),
        ("docs.json mintlify", "Mintlify", "core"),
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

            try:
                r_meta = requests.get(f"https://api.github.com/repos/{full_name}", headers=HEADERS, timeout=6)
                if r_meta.status_code != 200:
                    continue
                meta = r_meta.json()
            except Exception:
                continue

            stars = meta.get("stargazers_count", 0)
            if stars < 10 or not is_early_stage_repo(meta):
                continue

            owner_login = meta.get("owner", {}).get("login", "")
            if not check_org_size_ok(owner_login, org_cache):
                continue

            file_path = item.get("path", "")
            homepage = meta.get("homepage") or ""
            root_domain = extract_root_domain(homepage)

            parts = []
            if tier == "core":
                score = 95
                parts.append(f"95 base ({default_platform} config)")
            else:
                score = 65
                parts.append(f"65 base ({default_platform} config)")

            if root_domain:
                score += 10
                parts.append("+10 custom domain")
            if 25 <= stars <= 2500:
                score += 5
                parts.append("+5 early traction (25–2.5k★)")

            desc = (meta.get("description") or "").strip()
            detail = f"Detected `{file_path}` ({stars}★)."
            if desc:
                detail += f" {desc}"

            leads.append({
                "name": full_name,
                "domain": root_domain or "",
                "url": meta.get("html_url", f"https://github.com/{full_name}"),
                "signal_type": "DOCS_CONFIG_ADDED",
                "platform": default_platform,
                "score": min(score, 120),
                "score_breakdown": " · ".join(parts),
                "detail": detail[:260],
            })
            time.sleep(0.2)

    return leads


def collect_github_doc_issues(days_back: int = 14) -> List[dict]:
    """Find open issues explicitly asking for docs help or flagging doc drift."""
    leads: List[dict] = []
    seen_repos = set()
    org_cache: Dict[str, bool] = {}
    since_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")

    search_queries = [
        f'is:issue is:open label:documentation created:>{since_date} stars:25..3000',
        f'is:issue is:open "documentation" "drift" created:>{since_date} stars:20..5000',
        f'is:issue is:open "need docs" OR "missing docs" created:>{since_date} stars:20..3000',
    ]

    for q in search_queries:
        try:
            resp = requests.get(
                "https://api.github.com/search/issues",
                headers=HEADERS,
                params={"q": q, "sort": "created", "order": "desc", "per_page": 25},
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            items = resp.json().get("items", [])
        except Exception:
            continue

        for issue in items:
            title = issue.get("title", "")
            if CI_ISSUE_NOISE.search(title):
                continue

            repo_url = issue.get("repository_url", "")
            full_name = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
            if not full_name or full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            try:
                r_meta = requests.get(repo_url, headers=HEADERS, timeout=6)
                if r_meta.status_code != 200:
                    continue
                meta = r_meta.json()
            except Exception:
                continue

            if not is_early_stage_repo(meta):
                continue

            owner_login = meta.get("owner", {}).get("login", "")
            if not check_org_size_ok(owner_login, org_cache):
                continue

            root_domain = extract_root_domain(meta.get("homepage") or "")
            platform, _, has_docs = probe_docs_infrastructure(root_domain) if root_domain else ("Greenfield / None", "", False)

            score = 90
            parts = ["90 base (open docs/drift issue)"]
            if root_domain:
                score += 5
                parts.append("+5 custom domain")
            if has_docs:
                score += 5
                parts.append("+5 active docs site")

            stars = meta.get("stargazers_count", 0)

            leads.append({
                "name": full_name,
                "domain": root_domain or "",
                "url": issue.get("html_url", ""),
                "signal_type": "DOC_ISSUE_OPEN",
                "platform": platform if has_docs else "Greenfield / None",
                "score": score,
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

        root_domain = extract_root_domain(story_url)
        platform, evidence, has_docs = probe_docs_infrastructure(root_domain)

        if has_docs:
            score = 85
            parts = [f"85 base (Show HN + live {platform} docs)"]
        else:
            score = 60
            parts = ["60 base (Show HN launch, no docs yet)"]

        points = hit.get("points", 0)
        if points >= 75:
            score += 5
            parts.append(f"+5 HN traction ({points} pts)")

        clean_name = title.replace("Show HN:", "").strip()
        leads.append({
            "name": clean_name,
            "domain": root_domain or "",
            "url": story_url,
            "signal_type": "SHOW_HN_DEVTOOL",
            "platform": platform,
            "score": score,
            "score_breakdown": " · ".join(parts),
            "detail": f"Show HN ({points} pts) — {evidence}",
        })

    return leads


def collect_yc_companies(limit: int = 150) -> List[dict]:
    """Fetch recent Y Combinator companies from the public YC OSS directory."""
    leads: List[dict] = []
    try:
        resp = requests.get("https://yc-oss.github.io/api/companies/all.json", timeout=15)
        if resp.status_code != 200:
            return leads
        companies = resp.json()
    except Exception:
        return leads

    recent_batches = {"W24", "S24", "F24", "W25", "S25", "F25", "W26", "S26"}
    filtered = [
        c for c in companies
        if c.get("batch") in recent_batches
        and any(
            tag in (c.get("industry", "") + " " + " ".join(c.get("tags", []))).lower()
            for tag in ("developer", "b2b", "infrastructure", "ai", "api", "cloud", "security", "data")
        )
    ][:limit]

    for comp in filtered:
        name = comp.get("name", "Unknown")
        website = comp.get("website") or ""
        batch = comp.get("batch", "")
        one_liner = (comp.get("one_liner") or "").strip()
        root_domain = extract_root_domain(website)

        platform, evidence, has_docs = probe_docs_infrastructure(root_domain)
        if has_docs:
            score = 75
            breakdown = f"75 base (YC {batch} + active {platform} docs)"
        else:
            score = 35
            breakdown = f"35 base (YC {batch} greenfield — no docs site yet)"

        detail = f"YC {batch}: {one_liner}" if one_liner else f"YC {batch}"
        detail += f" ({evidence})"

        leads.append({
            "name": f"{name} ({batch})",
            "domain": root_domain or "",
            "url": website or f"https://www.ycombinator.com/companies/{comp.get('slug', '')}",
            "signal_type": "YC_BATCH",
            "platform": platform,
            "score": score,
            "score_breakdown": breakdown,
            "detail": detail,
        })

    return leads


# ---------------------------------------------------------------------------
# State Persistence & Merge (Preserves first_seen, is_new, score_delta)
# ---------------------------------------------------------------------------

def merge_with_existing_state(new_leads: List[dict], data_path: Path) -> dict:
    today_str = datetime.date.today().isoformat()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    previous_by_key: Dict[str, dict] = {}
    if data_path.exists():
        try:
            existing = json.loads(data_path.read_text(encoding="utf-8"))
            for item in existing.get("leads", []):
                k = (item.get("url") or item.get("name") or "").rstrip("/").lower()
                if k:
                    previous_by_key[k] = item
        except Exception:
            pass

    deduped: Dict[str, dict] = {}
    for lead in new_leads:
        k = (lead.get("url") or lead.get("name") or "").rstrip("/").lower()
        if not k:
            continue
        if k not in deduped or lead["score"] > deduped[k]["score"]:
            deduped[k] = lead
