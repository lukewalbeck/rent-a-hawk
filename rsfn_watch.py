#!/usr/bin/env python3
"""Watch RentSFNow (a large SF landlord) for units that match criteria.json.

RentSFNow lists ~100 SF units through the same JSON endpoint its search page
uses. Each run pulls every SF unit, drops wrong beds/price/neighborhood, then
opens the remaining unit pages (plain HTML) to check size, in-unit laundry and
parking. New matches are pushed to ntfy. The first run seeds silently.
"""
import argparse
import html
import os
import re
import sys

import cl_watch as cw

BASE = "https://www.rentsfnow.com"
AJAX = BASE + "/wp-admin/admin-ajax.php"
C = cw.CRITERIA
MAX_PAGES = 20

BEDS_RE = re.compile(r"(\d+)\s*Bed", re.I)
PRICE_RE = re.compile(r"\$\s?([\d,]{4,6})")
SQFT_RE = re.compile(r"(\d{3,4})\s*sqft", re.I)


def search_page(page):
    data = {
        "neighborhood": "", "city": "san-francisco", "price": f"1500,{C['max_price']}",
        "bedrooms": "", "bathrooms": "", "withdishwasher": "", "in_unit_laundry": "",
        "furnished": "", "pets_cats_only": "", "pets_dogs_only": "",
        "minprice": "1500", "maxprice": str(C["max_price"]), "sort": "priority_value-desc",
        "view": "map", "comingsoon": "", "comingsoononly": "", "action": "wpas_ajax_load",
        "page": str(page), "latN": "37.86", "latS": "37.68", "lonE": "-122.35",
        "lonW": "-122.54", "type": "json",
    }
    r = cw.SESSION.post(AJAX, data=data, timeout=20,
                        headers={"Referer": BASE + "/apartments/sf/",
                                 "X-Requested-With": "XMLHttpRequest"})
    r.raise_for_status()
    return r.json()


def all_units():
    units, page = [], 1
    while page <= MAX_PAGES:
        j = search_page(page)
        batch = j.get("units") or []
        units += batch
        if not batch or page >= int(j.get("last_page") or 1):
            break
        page += 1
    return units


def summary(u):
    crit = html.unescape(u.get("criteria", ""))
    m = BEDS_RE.search(crit)
    beds = int(m.group(1)) if m else (0 if "studio" in crit.lower() else None)
    m = PRICE_RE.search(crit)
    price = int(m.group(1).replace(",", "")) if m else None
    return beds, price


def in_area(hood):
    h = (hood or "").lower()
    return any(n in h for n in C["neighborhoods"])


def unit_details(url):
    r = cw.get(url)
    if r is None or r.status_code != 200:
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    m = SQFT_RE.search(text)
    return {"text": text, "low": text.lower(), "sqft": int(m.group(1)) if m else None}


def evaluate(d):
    low, reject, flags = d["low"], [], []
    if d["sqft"] and d["sqft"] < C["min_sqft"]:
        reject.append(f"{d['sqft']} sq ft")
    rc = cw.is_rent_controlled(low)
    if "washer/dryer in apartment" not in low and not any(k in low for k in cw.IN_UNIT):
        if rc and C.get("shared_laundry_ok_if_rent_controlled") and "washer/dryer in building" in low:
            flags.append("near-miss: shared laundry (rent-controlled)")
        else:
            reject.append("no in-unit laundry")
    if "street parking only" in low or "parking" not in low:
        reject.append("no parking")
    nope = [p for p in cw.REJECT_PHRASES if p in low]
    if nope:
        reject.append("dealbreaker: " + ", ".join(nope[:3]))
    if "dishwasher" not in low:
        flags.append("no dishwasher")
    if rc:
        flags.append("rent-controlled ✅")
    return reject, flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    topic = os.environ.get("NTFY_TOPIC", "").strip()

    state = cw.load_state()
    st = state.setdefault("rsfn", {"seen": {}, "fail_streak": 0, "alerted": False})
    first_run = not st["seen"]

    try:
        units = all_units()
    except Exception as e:  # noqa: BLE001
        st["fail_streak"] = st.get("fail_streak", 0) + 1
        print(f"RentSFNow search failed ({e}); streak {st['fail_streak']}.")
        if st["fail_streak"] >= 8 and not st.get("alerted"):
            cw.send(args, topic, "rent-a-hawk can't read RentSFNow",
                    "Their listings endpoint has failed for ~2 hours. Craigslist and the "
                    "email alerts are unaffected.", tags=["warning"], priority=3)
            st["alerted"] = True
        if not args.dry_run:
            cw.save_state(state)
        return
    st["fail_streak"], st["alerted"] = 0, False
    print(f"{len(units)} RentSFNow SF units.")

    sent = 0
    for u in units:
        url = BASE + u["url"]
        beds, price = summary(u)
        key = f"{price}"
        if st["seen"].get(url) == key:
            continue
        st["seen"][url] = key          # new unit or a price change
        if first_run:
            continue
        if beds is None or beds not in C["beds"] or not price or price > C["max_price"]:
            continue
        if not in_area(u.get("neightborhood")):
            continue
        d = unit_details(url)
        if d is None:
            del st["seen"][url]        # retry next run
            continue
        reject, flags = evaluate(d)
        if reject:
            print(f"  - {u['post_title']}: {', '.join(reject)}")
            continue
        if price > C["target_price"]:
            flags.append(f"over ${C['target_price']:,} target")
        size = f"{d['sqft']} sq ft" if d["sqft"] else "size n/a"
        laundry = ("shared laundry" if any("shared laundry" in f for f in flags)
                   else "✓ in-unit W/D")
        msg = (f"${price:,} · {beds} bd · {size} · {u.get('neightborhood')}\n"
               + laundry + " · parking listed"
               + (" · " + " · ".join(flags) if flags else "")
               + "\nRentSFNow (landlord's own site)")
        print(f"+ MATCH: {u['post_title']} | ${price:,} | {url}")
        cw.send(args, topic, u["post_title"], msg, url, tags=["house"])
        sent += 1

    if first_run:
        print("First run: seeded current units without notifying.")
    if not args.dry_run:
        cw.save_state(state)
    print(f"Done. {sent} RentSFNow match(es) sent.")


if __name__ == "__main__":
    sys.exit(main())
