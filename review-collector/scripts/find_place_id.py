#!/usr/bin/env python3
"""Resolve a business name to its exact Google Place ID.

Uses Google Maps' own public search prefetch endpoint -- no API key, no cost.
Always confirm the printed street address before pasting an ID into .env, so a
similarly named dealership in another town can never be monitored by mistake.

    python scripts/find_place_id.py "BMW of Fort Walton Beach"
"""
from __future__ import annotations

import html
import re
import sys
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", "replace")


def search(query: str) -> str:
    page = fetch("https://www.google.com/maps/search/" + urllib.parse.quote_plus(query))
    match = re.search(r'<link href="(/search\?tbm=map[^"]+)"', page)
    if not match:
        raise SystemExit(
            "Google Maps did not return its search prefetch link. The page layout may have "
            "changed, or this network is being served a restricted version of Maps."
        )
    url = "https://www.google.com" + html.unescape(match.group(1))
    url = re.sub(r"gl=[a-z]{2}", "gl=us", url)
    return fetch(url)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)

    query = " ".join(sys.argv[1:])
    print(f"Searching Google Maps for: {query}\n")
    body = search(query)

    place_ids = sorted(set(re.findall(r"ChIJ[A-Za-z0-9_-]{20,}", body)))
    feature_ids = sorted(set(re.findall(r"0x[0-9a-f]{16}:0x[0-9a-f]{16}", body)))

    if not place_ids:
        raise SystemExit(
            "No Place ID found. Try a more specific query, e.g. include the city and state."
        )

    for feature_id in feature_ids:
        index = body.find(feature_id)
        window = body[max(0, index - 1600) : index + 200]
        addresses = re.findall(r'\["(\d[^"]{3,60})",\s*"([^"]{3,60})"\]', window)
        names = re.findall(r'"([A-Z][^"]{4,70})"', window)
        print(f"  feature id : {feature_id}")
        if addresses:
            print(f"  address    : {', '.join(addresses[-1])}")
        if names:
            print(f"  name-ish   : {names[-1]}")
        print()

    print("Place ID(s) found (paste the matching one into .env):")
    for place_id in place_ids:
        print(f"  {place_id}")
        print(f"    https://www.google.com/maps/place/?q=place_id:{place_id}")
    print("\nVERIFY the street address above before using an ID.")


if __name__ == "__main__":
    main()
