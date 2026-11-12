"""Read-only smoke probe of public NB-Bet pages. Never writes to database or S3."""
import json
import sys
from datetime import UTC, datetime, timedelta

import requests

from tennis_predictor.ingestion.nbbet.details import fetch_match_page, parse_match_page

LISTING = "https://app.nb-bet.com/v1/tennis/results/page"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru",
    "Origin": "https://nb-bet.com",
    "Referer": "https://nb-bet.com/",
}


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    checked, parsed, recognized = 0, 0, 0
    examples = []
    for offset in range(4):
        day = datetime.now(UTC).date() - timedelta(days=offset)
        timestamp = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        try:
            response = session.get(LISTING, params={"timestamp": timestamp}, timeout=15)
            response.raise_for_status()
            leagues = response.json().get("data", {}).get("leagues", [])
        except (requests.RequestException, ValueError) as exc:
            print(f"listing {day}: {type(exc).__name__}: {exc}")
            continue
        for league in leagues:
            for match in league.get("4", []):
                slug = match.get("3")
                if not isinstance(slug, str) or not slug or not slug.split("-", 1)[0].isdigit():
                    continue
                if checked >= 8:
                    break
                checked += 1
                try:
                    html = fetch_match_page(session, slug)
                    if not html:
                        continue
                    d = parse_match_page(html, int(slug.split("-", 1)[0]))
                    parsed += 1
                    title = (d.raw_page_tennis_event.get("match", {}).get("1") or {}).get("1")
                    examples.append({"surface": d.surface, "title": str(title)[:160]})
                    recognized += bool(d.surface)
                except Exception as exc:
                    print(f"page failure: {type(exc).__name__}: {exc}")
            if checked >= 8:
                break
        if checked >= 8:
            break
    print(json.dumps({"checked": checked, "parsed": parsed, "recognized": recognized,
                      "examples": examples}, ensure_ascii=False, indent=2))
    if parsed == 0:
        print("INCONCLUSIVE: no real pages parsed", file=sys.stderr)
        return 2
    if recognized == 0:
        print("INCONCLUSIVE: no surface recognized on real pages", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
