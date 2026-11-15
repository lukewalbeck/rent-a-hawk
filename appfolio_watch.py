#!/usr/bin/env python3
"""Watch SF landlords' own AppFolio listing pages for units that match criteria.json.

Many SF property managers publish vacancies at <name>.appfolio.com/listings. Every
site uses the same template, so one scraper covers all of them. Only the public
listings index is read (AppFolio's robots.txt disallows the detail pages); each card
there already carries the rent, bed/bath, size, address, description and amenities.

Cards are judged with cl_watch.evaluate, so the rules match Craigslist exactly.
Neighborhood comes from the ZIP code (or a map pin, when the page has one).
New matches are pushed to ntfy. The first run for each site seeds silently.

Add a landlord: append its AppFolio subdomain to SITES below.

Usage:
  python appfolio_watch.py              # normal run (GitHub Actions)
  python appfolio_watch.py --dry-run    # print everything, don't notify or save
"""
import argparse
import os
import re
import sys
import time

from bs4 import BeautifulSoup

import cl_watch as cw

# subdomain -> display name. Add more SF landlords here.
SITES = {
    "chandlerproperties": "Chandler Properties",
    "ggproperties": "Golden Gate Properties",
    "thehousingguild": "The Housing Guild",
}

# ZIP -> neighborhood label fed to cl_watch.evaluate. 94115 and 94109 also cover
# areas you didn't pick (Western Addition, Nob Hill, Tenderloin), so those get a flag.
ZIP_HOOD = {
    "94123": "marina / cow hollow",
    "94118": "inner richmond / laurel hts / presidio hts",
    "94133": "north beach / telegraph hill / russian hill",
    "94115": "pacific heights",
    "94109": "russian hill",
}
ZIP_SPANS_OTHER_AREAS = {"94115", "94109"}

UUID_RE = re.compile(r"/listings/detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
ZIP_RE = re.compile(r"\b(94\d{3})(?:-\d{4})?\b")
SQFT_RE = re.compile(r"square\s*feet\s*:?\s*([\d,]{3,6})|([\d,]{3,6})\s*(?:sq\.?\s?ft|sqft|square feet)", re.I)
BEDS_RE = re.compile(r"(?<![\d,$.])(\d{1,2})\s*(?:bd|beds?|br)\b", re.I)
ADDR_RE = re.compile(r"\d{1,5}[^,]{2,60},\s*San Francisco,?\s*(?:CA)?\s*94\d{3}(?:-\d{4})?", re.I)
COORD_RE = re.compile(r'"?lat(?:itude)?"?\s*[:=]\s*"?(3[78]\.\d+)"?\s*,\s*"?(?:lng|lon|longitude)"?\s*[:=]\s*"?(-12[23]\.\d+)')


def fetch_index(site):
    url = f"https://{site}.appfolio.com/listings"
    r = cw.get(url)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code if r is not None else 'none'}")
    return r.text, url


def cards(html):
    """Yield (uuid, card_soup) for each listing, without relying on CSS class names."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["style", "noscript"]):
        t.decompose()
    first_anchor = {}
    for a in soup.find_all("a", href=True):
        m = UUID_RE.search(a["href"])
        if m and m.group(1) not in first_anchor:
            first_anchor[m.group(1)] = a
    for uid, a in first_anchor.items():
        # Climb while the container still refers to this listing only.
        node = a
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            ids = set(UUID_RE.findall(str(node.parent)))
            if ids - {uid}:
                break
            node = node.parent
        yield uid, node


def parse_card(uid, node, html):
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
    low = text.lower()
    title_el = node.find(["h2", "h3"])
    title = title_el.get_text(" ", strip=True) if title_el else ""
    addr_el = node.select_one("[class*=address]")
    address = addr_el.get_text(" ", strip=True) if addr_el else ""
    if not address:
        m = ADDR_RE.search(text)
        address = m.group(0) if m else ""
    m = re.search(r"rent\s*\$\s?([\d,]{3,7})", text, re.I) or re.search(r"\$\s?([\d,]{3,7})", text)
    price = int(m.group(1).replace(",", "")) if m else None
    m = SQFT_RE.search(text)
    sqft = int((m.group(1) or m.group(2)).replace(",", "")) if m else None
    m = BEDS_RE.search(text)
    beds = int(m.group(1)) if m else (0 if "studio" in low else None)
    m = ZIP_RE.search(address) or ZIP_RE.search(text)
    zip_ = m.group(1) if m else ""
    lat = lon = None
    i = html.find(uid)
    while i != -1 and lat is None:  # a map marker near this listing's id, if the page has one
        m = COORD_RE.search(html[max(0, i - 600): i + 600])
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
        i = html.find(uid, i + 1)
    return {
        "gone": False, "lat": lat, "lon": lon,
        "header": title, "hood": ZIP_HOOD.get(zip_, f"zip {zip_}" if zip_ else ""),
        "price": price, "sqft": sqft, "beds": beds, "body": text, "attrs": "",
        "address": address, "text": low, "zip": zip_,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify-existing", action="store_true",
                    help="judge current listings on a site's first run instead of seeding silently")
    args = ap.parse_args()
    topic = os.environ.get("NTFY_TOPIC", "").strip()

    state = cw.load_state()
    st = state.setdefault("appfolio", {})
    sent = 0
    for site, name in SITES.items():
        s = st.setdefault(site, {"seen": {}, "fail_streak": 0, "alerted": False})
        first_run = not s["seen"] and not args.notify_existing
        try:
            html, url = fetch_index(site)
            listings = [(uid, parse_card(uid, node, html)) for uid, node in cards(html)]
            if not listings and "no vacancies" not in html.lower():
                raise RuntimeError("no listings parsed (page layout may have changed)")
        except Exception as e:  # noqa: BLE001
            s["fail_streak"] = s.get("fail_streak", 0) + 1
            print(f"{name}: failed ({e}); streak {s['fail_streak']}.")
            if s["fail_streak"] >= 12 and not s.get("alerted"):
                cw.send(args, topic, f"rent-a-hawk can't read {name}",
                        f"Their AppFolio listings page has failed for ~4 hours: {e}. "
                        "Other sources are unaffected.", tags=["warning"], priority=3)
                s["alerted"] = True
            continue
        s["fail_streak"], s["alerted"] = 0, False
        print(f"{name}: {len(listings)} listings.")

        for uid, d in listings:
            detail = f"https://{site}.appfolio.com/listings/detail/{uid}"
            key = str(d["price"])
            if s["seen"].get(uid) == key:
                continue
            s["seen"][uid] = key          # new listing or a price change
            label = d["header"] or d["address"] or uid
            if first_run:
                if args.dry_run:
                    print(f"  (seed) {label} | {d['price']} | {d['beds']} bd | {d['sqft']} | {d['hood']}")
                continue
            if d["beds"] is not None and d["beds"] not in cw.CRITERIA["beds"]:
                print(f"  - skip ({d['beds']} bd): {label}")
                continue
            if d["price"] and d["price"] > cw.CRITERIA["max_price"]:
                print(f"  - skip (${d['price']:,}): {label}")
                continue
            keep, rejected, flags = cw.evaluate({"title": d["header"], "hood": d["hood"],
                                                 "price": d["price"]}, d)
            if not keep:
                print(f"  - skip ({'; '.join(rejected)}): {label}")
                continue
            if d["zip"] in ZIP_SPANS_OTHER_AREAS and d["lat"] is None:
                flags.append(f"ZIP {d['zip']} also covers areas you skipped; check the block")
            bits = [f"${d['price']:,}" if d["price"] else "price?",
                    f"{d['beds']} bd" if d["beds"] is not None else None,
                    f"{d['sqft']:,} sq ft" if d["sqft"] else None, d["hood"] or None]
            msg = " · ".join(b for b in bits if b)
            if d["address"]:
                msg += f"\n📍 {d['address']}"
            if flags:
                msg += "\n" + "\n".join(f"• {f}" for f in flags)
            msg += f"\n{name} (landlord's own site)"
            print(f"  + MATCH: {label} | {d['price'] or '?'} | {detail}")
            cw.send(args, topic, label, msg, url=detail, tags=["house"])
            sent += 1
        if first_run:
            print(f"  First run for {name}: seeded {len(listings)} listings without notifying.")
        time.sleep(2)

    if not args.dry_run:
        cw.save_state(state)
    print(f"Done. {sent} AppFolio match(es) sent.")


if __name__ == "__main__":
    sys.exit(main())
