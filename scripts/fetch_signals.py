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
    "User-Agent": "DocsOpportunityScout/2.3",
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

        # CNAME points to a generic host/CDN; inspect HTML on that subdomain
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
    if NOISE_PATTERNS.search(full_name) or NOISE_PATTERNS
