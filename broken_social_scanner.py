#!/usr/bin/env python3

import asyncio
import aiohttp
import json
import re
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse
from pathlib import Path
from bs4 import BeautifulSoup

# =========================
# CONFIG
# =========================

MAX_PAGES = 100
CONCURRENCY = 10
TIMEOUT = 10

SOCIAL_PATTERNS = {
    "Facebook": r"(?:https?://)?(?:www\.)?facebook\.com/[^\s\"'<>]+",
    "Instagram": r"(?:https?://)?(?:www\.)?instagram\.com/[^\s\"'<>]+",
    "X/Twitter": r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/[^\s\"'<>]+",
    "YouTube": r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s\"'<>]+",
    "TikTok": r"(?:https?://)?(?:www\.)?tiktok\.com/[^\s\"'<>]+",
    "LinkedIn": r"(?:https?://)?(?:www\.)?linkedin\.com/[^\s\"'<>]+",
}

OUTPUT_DIR = Path("results")
OUTPUT_DIR.mkdir(exist_ok=True)

# =========================
# HELPERS
# =========================

def normalize_url(url):
    url = url.strip()

    if not url:
        return None

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if not parsed.netloc:
        return None

    return url.rstrip("/")


def same_domain(base, target):
    return urlparse(base).netloc.lower() == urlparse(target).netloc.lower()


def detect_platform(url):
    for platform, pattern in SOCIAL_PATTERNS.items():
        if re.search(pattern, url, re.I):
            return platform

    return None


def clean_url(url):
    url = url.strip()

    # Remove trailing HTML punctuation
    url = url.rstrip(".,;:!?)]}")

    return url


# =========================
# HTTP
# =========================

async def fetch(session, url):

    try:
        async with session.get(
            url,
            timeout=TIMEOUT,
            allow_redirects=True,
            ssl=False,
            headers={
                "User-Agent":
                    "Mozilla/5.0 (BrokenSocialScanner/1.0)"
            }
        ) as response:

            body = await response.text(errors="ignore")

            return {
                "status": response.status,
                "final_url": str(response.url),
                "body": body,
            }

    except Exception as e:

        return {
            "status": 0,
            "final_url": url,
            "body": "",
            "error": str(e),
        }


# =========================
# SOCIAL EXTRACTION
# =========================

def extract_social_links(html):

    found = {}

    soup = BeautifulSoup(html, "html.parser")

    # href
    for tag in soup.find_all("a", href=True):

        url = clean_url(tag["href"])

        platform = detect_platform(url)

        if platform:
            found[url] = platform

    # Raw text / source
    for platform, pattern in SOCIAL_PATTERNS.items():

        matches = re.findall(pattern, html, re.I)

        for match in matches:

            url = clean_url(match)

            if not url.startswith("http"):
                url = "https://" + url

            found[url] = platform

    return found


# =========================
# INTERNAL LINKS
# =========================

def extract_internal_links(base_url, html):

    soup = BeautifulSoup(html, "html.parser")

    links = set()

    for tag in soup.find_all("a", href=True):

        href = tag["href"].strip()

        if not href:
            continue

        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        full = urljoin(base_url, href)

        parsed = urlparse(full)

        if parsed.scheme not in ("http", "https"):
            continue

        full = full.split("#")[0]

        if same_domain(base_url, full):
            links.add(full)

    return links


# =========================
# CHECK SOCIAL URL
# =========================

async def check_social(session, source, platform, social_url):

    result = await fetch(session, social_url)

    status = result["status"]

    # 404/410 = strongest indication of broken resource
    if status in (404, 410):

        state = "BROKEN"

    elif status in (401, 403):

        state = "INACCESSIBLE"

    elif status in (200, 301, 302, 307, 308):

        state = "ACTIVE"

    elif status == 0:

        state = "ERROR"

    else:

        state = "UNKNOWN"

    return {
        "source": source,
        "platform": platform,
        "social_url": social_url,
        "status": status,
        "state": state,
        "final_url": result["final_url"],
    }


# =========================
# SCAN TARGET
# =========================

async def scan_target(session, target):

    print(f"\n{'=' * 60}")
    print(f"TARGET: {target}")
    print(f"{'=' * 60}")

    queue = asyncio.Queue()
    await queue.put(target)

    visited = set()
    social_links = {}

    while not queue.empty() and len(visited) < MAX_PAGES:

        current = await queue.get()

        if current in visited:
            continue

        visited.add(current)

        print(
            f"\r[+] Crawling: {len(visited)}/{MAX_PAGES}",
            end="",
            flush=True
        )

        result = await fetch(session, current)

        if result["status"] == 0:
            continue

        html = result["body"]

        # Social links
        found_social = extract_social_links(html)

        for social_url, platform in found_social.items():

            social_links[social_url] = {
                "platform": platform,
                "source": current
            }

        # Internal links
        internal = extract_internal_links(
            target,
            html
        )

        for link in internal:

            if link not in visited:
                await queue.put(link)

    print()

    print(
        f"[+] Pages crawled : {len(visited)}"
    )

    print(
        f"[+] Social links  : {len(social_links)}"
    )

    # Check social URLs
    tasks = []

    for social_url, info in social_links.items():

        tasks.append(
            check_social(
                session,
                info["source"],
                info["platform"],
                social_url
            )
        )

    results = await asyncio.gather(*tasks)

    for result in results:

        if result["state"] == "BROKEN":

            print(
                f"\n[!] POTENTIAL BROKEN LINK"
            )

            print(
                f"    Platform : {result['platform']}"
            )

            print(
                f"    Source   : {result['source']}"
            )

            print(
                f"    URL      : {result['social_url']}"
            )

            print(
                f"    Status   : {result['status']}"
            )

    return {
        "target": target,
        "pages_crawled": len(visited),
        "social_links": len(social_links),
        "results": results
    }


# =========================
# SAVE RESULTS
# =========================

def save_results(all_results):

    timestamp = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    json_file = OUTPUT_DIR / f"scan_{timestamp}.json"
    txt_file = OUTPUT_DIR / f"scan_{timestamp}.txt"

    # JSON
    with open(json_file, "w", encoding="utf-8") as f:

        json.dump(
            all_results,
            f,
            indent=4,
            ensure_ascii=False
        )

    # TXT
    with open(txt_file, "w", encoding="utf-8") as f:

        f.write("=" * 70 + "\n")
        f.write("BROKEN SOCIAL LINK SCANNER\n")
        f.write("=" * 70 + "\n\n")

        for target in all_results:

            f.write(
                f"TARGET: {target['target']}\n"
            )

            f.write(
                f"PAGES: {target['pages_crawled']}\n"
            )

            f.write(
                f"SOCIAL LINKS: {target['social_links']}\n"
            )

            f.write("-" * 70 + "\n")

            broken = [
                x for x in target["results"]
                if x["state"] == "BROKEN"
            ]

            if not broken:

                f.write(
                    "No broken social links detected.\n\n"
                )

                continue

            for item in broken:

                f.write(
                    f"[!] {item['platform']}\n"
                )

                f.write(
                    f"Source : {item['source']}\n"
                )

                f.write(
                    f"URL    : {item['social_url']}\n"
                )

                f.write(
                    f"Status : {item['status']}\n\n"
                )

            f.write("\n")

    print("\n" + "=" * 70)
    print("SCAN SELESAI")
    print("=" * 70)
    print(f"TXT  : {txt_file}")
    print(f"JSON : {json_file}")

                                                                                 # =========================
# MAIN
# =========================

async def main():

    if len(sys.argv) < 2:

        print(
            f"Usage:\n"
            f"  python3 {sys.argv[0]} targets.txt"
        )

        sys.exit(1)

    target_file = Path(sys.argv[1])

    if not target_file.exists():

        print(
            f"[!] File tidak ditemukan: {target_file}"
        )

        sys.exit(1)

    targets = []

    with open(
        target_file,
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            url = normalize_url(line)

            if url and url not in targets:
                targets.append(url)

    if not targets:

        print("[!] Tidak ada target.")
        sys.exit(1)

    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY
    )

    async with aiohttp.ClientSession(
        connector=connector
    ) as session:

        all_results = []

        for target in targets:

            try:

                result = await scan_target(
                    session,
                    target
                )

                all_results.append(result)

            except KeyboardInterrupt:

                print(
                    "\n[!] Scan dihentikan."
                )

                break

            except Exception as e:

                print(
                    f"\n[!] Error {target}: {e}"
                )

    save_results(all_results)


if __name__ == "__main__":

    asyncio.run(main())
