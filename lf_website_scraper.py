#!/usr/bin/env python3
"""
lf_website_scraper.py - Playwright-Based Company Website Scraper
================================================================
Scrapes company team/about pages using Playwright for JS-rendered content.
Finds executive names, titles, and LinkedIn URLs.
"""

import asyncio
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Error as PlaywrightError

BASE_DIR = Path(__file__).parent


async def _fetch_page(url: str, timeout: int = 15000) -> Optional[str]:
    """Fetch a URL with Playwright, return HTML text."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=timeout)
            content = await page.content()
            await browser.close()
            return content
    except PlaywrightError as e:
        print(f"[scraper] Playwright error fetching {url}: {e}")
        return None


def scrape_team_page_sync(website: str, paths_to_try: list = None) -> list[dict]:
    """
    Synchronous wrapper for Playwright team page scraping.
    Returns list of {full_name, title, source_url, confidence_score}.
    """
    if not website:
        return []
    if not website.startswith("http"):
        website = "https://" + website

    default_paths = [
        "/about", "/about-us/team", "/team", "/leadership",
        "/leadership-team", "/company/team", "/company/about",
        "/pages/team", "/people", "/our-team",
    ]
    if paths_to_try is None:
        paths_to_try = default_paths

    executives = []
    seen = set()

    for path in paths_to_try:
        url = website.rstrip("/") + path
        html = asyncio.run(_fetch_page(url))
        if not html:
            continue

        # Extract executive data from HTML
        people = _extract_from_html(html, url)
        for person in people:
            name = person.get("full_name", "")
            if name and name not in seen:
                seen.add(name)
                executives.append(person)

    return executives


def _extract_from_html(html: str, source_url: str) -> list[dict]:
    """
    Parse HTML for person cards/name+title combinations.
    Looks for common structural patterns: h3/h4 near job titles, LinkedIn links, etc.
    """
    import re
    results = []

    title_kw = [
        "CEO", "CFO", "COO", "CTO", "President", "Vice President", "VP",
        "Director", "General Manager", "Founder", "Owner", "Chief",
        "Operations", "Engineering", "Sales", "Marketing",
    ]

    # LinkedIn in href
    linkedin_pattern = re.compile(r'href=["\']([^"\']*linkedin\.com/in/[^"\']*)["\']', re.IGNORECASE)
    linkedin_urls = linkedin_pattern.findall(html)

    # Find name patterns near title keywords
    for kw in title_kw:
        # Pattern 1: <strong>CEO</strong> John Smith  or  <span class="title">CEO</span>
        p1 = re.compile(
            rf'(?:<[^>]*>(?:{kw})[^<]*</[^>]*>)\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
            re.IGNORECASE
        )
        # Pattern 2: Name\nTitle context
        p2 = re.compile(
            rf'([A-Z][a-z]+ [A-Z][a-z]+)\s*\n?\s*(?:{kw})',
            re.IGNORECASE
        )
        for mp in [p1, p2]:
            for match in mp.finditer(html):
                name = match.group(1).strip()
                if len(name) > 3 and name.count(" ") >= 1:
                    results.append({
                        "full_name": name,
                        "title": kw,
                        "source_url": source_url,
                        "confidence_score": 0.6,
                        "data_provenance": f"WEBSITE_SCRAPE_PLAYWRIGHT: {source_url}",
                    })

    # LinkedIn URLs: extract name from slug if present
    for li_url in linkedin_urls[:10]:
        name = _name_from_linkedin_url(li_url)
        if name:
            results.append({
                "full_name": name,
                "title": "",
                "source_url": li_url,
                "confidence_score": 0.7,
                "data_provenance": f"WEBSITE_SCRAPE_LINKEDIN: {li_url}",
            })

    return results


def _name_from_linkedin_url(url: str) -> str:
    """Extract a display name from a LinkedIn URL slug."""
    import re
    match = re.search(r"/in/([^/\?]+)", url)
    if not match:
        return ""
    slug = match.group(1)
    name = re.sub(r"-\d+$", "", slug).replace("-", " ").title()
    return name


if __name__ == "__main__":
    # Test
    import sys
    results = scrape_team_page_sync("https://www.ppg.com")
    for r in results:
        print(r)
