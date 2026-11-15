#!/usr/bin/env python3
"""
Craigslist SF apartment watcher -> free push notifications via ntfy.sh

Every run:
  1. Loads your filtered Craigslist search (newest first).
  2. Skips posts it has already seen (seen.json).
  3. Opens each NEW post and checks it against your must-haves
     (neighborhood, sq ft, in-unit laundry, real parking).
  4. Pushes matches to your phone, with flags for things to ask about
     (parking fee, no dishwasher, possible scam, over target price).

Usage:
  python cl_watch.py                 # normal run (used by GitHub Actions)
  python cl_watch.py --dry-run       # print matches, don't notify or save
  python cl_watch.py --test-notify   # send one test push to check your phone
  python cl_watch.py --notify-existing  # first run: alert on current listings too
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

# ============================================================
# YOUR SEARCH — edit these
# ============================================================
SEARCH_BASE = "https://sfbay.craigslist.org/search/sfc/apa"
# What counts as a match lives in criteria.json (shared with email_watch.py and
# Claude's hourly check). Edit that file, not these lines.
CRITERIA = json.loads(Path(__file__).with_name("criteria.json").read_text())
SEARCH_PARAMS = [
    ("min_bedrooms", str(min(CRITERIA["beds"]))),
    ("max_bedrooms", str(max(CRITERIA["beds"]))),
    ("max_price", str(CRITERIA["max_price"])),
    # No laundry/parking checkboxes: many landlords don't tick them. Each post's
    # text is checked instead (shared laundry / street-only parking are rejected).
    ("sort", "date"),        # newest first
]
TARGET_PRICE = CRITERIA["target_price"]   # flag anything above this
MIN_SQFT = CRITERIA["min_sqft"]           # reject if listed smaller; flag if not listed
NEIGHBORHOODS = CRITERIA["neighborhoods"]  # matched against label, title, opening text
AREA_BOXES = [tuple(b) for b in CRITERIA["area_boxes"]]  # (lat_min, lat_max, lon_min, lon_max)
REJECT_PHRASES = [p.lower() for p in CRITERIA.get("reject_phrases", [])]
GENERIC_HOODS = {"", "san francisco", "sf", "city of san francisco", "san francisco, ca"}

ALERT_AFTER_FAILURES = 4    # consecutive blocked runs (~1 hour) before alerting
MAX_DETAIL_FETCHES = 25      # per run, to stay polite; leftovers go next run
STATE_FILE = Path(__file__).with_name("seen.json")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
# ============================================================

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

IN_UNIT = ["w/d in unit", "washer/dryer in unit", "washer & dryer in unit",
           "washer and dryer in unit", "in-unit laundry", "in unit laundry",
           "laundry in unit", "in-unit washer", "in unit washer",
           "washer/dryer in the unit", "in-unit w/d", "in unit w/d"]
SHARED_LAUNDRY = ["laundry on site", "laundry on-site", "on-site laundry", "onsite laundry", "laundry onsite",
                  "laundry in bldg", "laundry in building", "shared laundry",
                  "coin laundry", "coin-op", "coin op", "laundry room"]
REAL_PARKING = ["attached garage", "detached garage", "off-street parking", "garage parking",
                "parking included", "1 car parking", "one-car parking", "one car parking",
                "parking space", "parking spot", "carport", "garage"]
DELETED = ["this posting has been deleted", "this posting has expired",
           "this posting has been flagged for removal", "posting not found"]
SCAM_PHRASES = ["wire transfer", "western union", "moneygram", "zelle", "cashapp", "cash app",
                "out of the country", "out of town", "out of state", "missionary",
                "deposit to hold", "deposit before", "before viewing", "before the viewing",
                "keys will be mailed", "keys mailed", "god bless", "airbnb will handle",
                "text me your phone", "text me your number", "your phone number", "text me at",
                "holding fee", "hold fee", "whatsapp", "google voice", "reply with your email",
                "send me your", "gift card", "crypto", "bitcoin"]
# Rent below these is effectively never real in these neighborhoods.
SCAM_PRICE_FLOOR = {int(k): v for k, v in CRITERIA["scam_price_floor"].items()}  # reject below
BELOW_MARKET = {int(k): v for k, v in CRITERIA["below_market_flag"].items()}    # flag "verify"
ADDRESS_RE = re.compile(
    r"\b\d{2,5}[A-Za-z]?\s+(?:[A-Z][a-z]+\s){0,3}(?:St|Street|Ave|Avenue|Blvd|Boulevard|"
    r"Way|Ter|Terrace|Pl|Place|Ct|Court|Dr|Drive|Ln|Lane|Rd|Road)\b")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(url, tries=2):
    for attempt in range(tries):
        try:
            r = SESSION.get(url, timeout=20)
            return r
        except requests.RequestException as e:
            if attempt == tries - 1:
                print(f"  ! request failed: {url}: {e}", file=sys.stderr)
            time.sleep(3)
    return None


def parse_price(text):
    m = re.search(r"\$\s?([\d,]{3,7})", text or "")
    return int(m.group(1).replace(",", "")) if m else None


def listing_key(url):
    last = url.split("?")[0].rstrip("/").split("/")[-1]
    return last[:-5] if last.endswith(".html") else last


# ---------------- search page ----------------
def parse_search(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    items, seen_urls = [], set()

    for li in soup.select("li.cl-static-search-result"):
        a = li.find("a", href=True)
        if not a:
            continue
        url = urljoin(base_url, a["href"])
        if url in seen_urls:
            continue
        seen_urls.add(url)
        price_el = li.select_one(".price")
        loc_el = li.select_one(".location")
        title = li.get("title") or (li.select_one(".title") or a).get_text(" ", strip=True)
        items.append({
            "url": url,
            "title": title.strip(),
            "price": parse_price(price_el.get_text() if price_el else ""),
            "hood": loc_el.get_text(" ", strip=True).lower() if loc_el else "",
        })

    if not items:  # fallback if Craigslist changes its markup
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if "/view/d/" in h or re.search(r"/apa/d/.+\.html", h):
                url = urljoin(base_url, h)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                text = a.get_text(" ", strip=True)
                items.append({"url": url, "title": text, "price": parse_price(text), "hood": ""})
    return items


# ---------------- listing page ----------------
def check_listing(url):
    """Return dict with details + verdict, or None if it couldn't be fetched."""
    r = get(url)
    if r is None or r.status_code >= 500:
        return None
    if r.status_code == 404:
        return {"gone": True}
    soup = BeautifulSoup(r.text, "html.parser")
    # Drop Craigslist's own boilerplate ("DO NOT wire funds (e.g. Western Union)",
    # safety notices, reply tips) so it isn't mistaken for the poster's words.
    for junk in soup.select(".avoid-scams, aside.tsb, .notices, .reply-info, "
                            "script, style, noscript"):
        junk.decompose()
    full = soup.get_text(" ", strip=True)
    low = full.lower()
    if any(p in low for p in DELETED):
        return {"gone": True}

    header_el = soup.select_one("h1") or soup.select_one(".postingtitle")
    header = header_el.get_text(" ", strip=True) if header_el else ""
    page_title = soup.title.get_text(strip=True) if soup.title else ""
    body_el = soup.select_one("#postingbody") or soup.select_one(".body")
    body = body_el.get_text(" ", strip=True) if body_el else ""
    body = body.replace("QR Code Link to This Post", "").strip()
    attrs = " ".join(e.get_text(" ", strip=True)
                     for e in soup.select(".attrgroup, .mapAndAttrs, .attr, .attrs"))
    addr_el = soup.select_one(".mapaddress") or soup.select_one(".address")
    address = addr_el.get_text(" ", strip=True) if addr_el else ""

    hood = ""
    m = re.search(r"\(([^()]{3,60})\)\s*$", header) or re.search(r"\(([^()]{3,60})\)", header)
    if m:
        hood = m.group(1).strip().lower()

    sqft = None
    m = re.search(r"(\d{3,5})\s*(?:ft2|ft²|sq\.?\s?ft|sqft|square\s+feet)", (header + " " + attrs + " " + body).lower())
    if m:
        sqft = int(m.group(1))

    beds = None
    m = re.search(r"(\d)\s*(?:br|bd|bed)", (header + " " + attrs).lower())
    if m:
        beds = int(m.group(1))

    lat = lon = None
    geo = soup.select_one("[data-latitude][data-longitude]")
    if geo:
        try:
            lat, lon = float(geo["data-latitude"]), float(geo["data-longitude"])
        except ValueError:
            pass
    if lat is None:
        m = re.search(r'"latitude"\s*:\s*(-?\d+\.\d+).{0,80}?"longitude"\s*:\s*(-?\d+\.\d+)', r.text, re.S)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))

    return {
        "gone": False,
        "lat": lat, "lon": lon,
        "header": header or page_title,
        "hood": hood,
        "price": parse_price(header) or parse_price(page_title),
        "sqft": sqft,
        "beds": beds,
        "body": body,
        "attrs": attrs.lower(),
        "address": address,
        "text": low,
    }


DATE_CTX_RE = re.compile(
    r"(open house|showing|available|move[- ]in)[^.\n]{0,40}?\b(\d{1,2})/(\d{1,2})\b")


def stale_dates(text, today=None):
    """True if an open-house / availability date in the post can't be current."""
    today = today or datetime.now().date()
    for m in DATE_CTX_RE.finditer(text):
        kind, mo, day = m.group(1), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= day <= 31):
            continue
        deltas = []
        for yr in (today.year - 1, today.year, today.year + 1):
            try:
                deltas.append((today.replace(year=yr, month=mo, day=day) - today).days)
            except ValueError:
                pass
        if not deltas:
            continue
        # Open houses happen within weeks; move-in dates can be months out.
        lo, hi = (-14, 45) if kind in ("open house", "showing") else (-60, 200)
        if not any(lo <= dl <= hi for dl in deltas):
            return True
    return False


RC_YEAR_RE = re.compile(r"built in (1[89]\d\d)")


def is_rent_controlled(text):
    """True if the post says it's rent-controlled or was built before mid-1979."""
    if any(k in text for k in ("rent ordinance", "rent control", "rent-controlled",
                               "rent stabiliz")):
        return True
    m = RC_YEAR_RE.search(text)
    return bool(m and int(m.group(1)) < 1979)


def evaluate(item, d):
    """Return (keep: bool, reasons_rejected: list, flags: list)."""
    reject, flags = [], []
    text = d["text"]
    body_low = d["body"].lower()
    hood = d["hood"] or item.get("hood", "")
    price = d["price"] or item.get("price")

    # Neighborhood: label match, or the post's map pin is inside a target box.
    lat, lon = d.get("lat"), d.get("lon")
    in_box = lat is not None and any(a <= lat <= b and c <= lon <= e for a, b, c, e in AREA_BOXES)
    if any(n in hood for n in NEIGHBORHOODS) or in_box:
        if in_box and not any(n in hood for n in NEIGHBORHOODS):
            flags.append("in your area by map pin")
    elif hood.strip() in GENERIC_HOODS or not hood:
        blob = (item.get("title", "") + " " + d["header"] + " " + body_low[:400]).lower()
        if any(n in blob for n in NEIGHBORHOODS):
            flags.append("neighborhood from description; confirm")
        else:
            reject.append(f"neighborhood '{hood or 'unknown'}'")
    else:
        reject.append(f"neighborhood '{hood}'")

    # Size
    if d["sqft"] is not None and d["sqft"] < MIN_SQFT:
        reject.append(f"{d['sqft']} sq ft")
    elif d["sqft"] is None:
        flags.append("sq ft not listed")

    # Laundry: in-unit is a must-have, unless the unit is rent-controlled and
    # criteria.json allows shared laundry for rent-controlled units.
    rent_controlled = is_rent_controlled(text)
    if any(k in text for k in IN_UNIT):
        pass
    elif any(k in text for k in SHARED_LAUNDRY):
        if rent_controlled and CRITERIA.get("shared_laundry_ok_if_rent_controlled"):
            flags.append("near-miss: shared laundry (rent-controlled)")
        else:
            reject.append("shared laundry")
    else:
        flags.append("laundry unclear")

    # Parking (real parking is a must-have)
    has_parking = any(k in text for k in REAL_PARKING)
    street_only = "street parking" in text and not has_parking
    if street_only or ("no parking" in text and not has_parking):
        reject.append("street parking only")
    elif not has_parking:
        flags.append("parking unclear")
    if re.search(r"parking[^.\n]{0,60}\$\s?\d{2,3}\b", text) or \
       re.search(r"\$\s?\d{2,3}\b[^.\n]{0,40}parking", text) or \
       re.search(r"parking[^.\n]{0,40}(additional|extra|separate)", text):
        flags.append("parking may cost extra")

    # Nice-to-haves / context
    if "dishwasher" not in text:
        flags.append("no dishwasher mentioned")
    if rent_controlled:
        flags.append("rent-controlled ✅")
    if price and price > TARGET_PRICE:
        flags.append(f"over ${TARGET_PRICE:,} target")

    # Scam signals. Strong signals reject outright; weak ones are flagged.
    beds = d["beds"] if d["beds"] is not None else 1
    bkey = min(beds, 2)
    if price and price < SCAM_PRICE_FLOOR[bkey]:
        reject.append(f"scam: ${price:,} is far below market")
    if item.get("copy_of"):
        reject.append(f"scam: copy of a ${item['copy_of']:,} listing with the same title")
    # Post text only: the full page includes Craigslist's own safety tips
    # ("never use western union...", "your phone number"), which flagged real posts.
    post_low = (d["header"] + " " + d["body"]).lower()
    hits = [p for p in SCAM_PHRASES if p in post_low]
    if hits:
        reject.append("scam: mentions " + ", ".join(hits[:3]))
    nope = [p for p in REJECT_PHRASES if p in text]
    if nope:
        reject.append("your dealbreaker: " + ", ".join(nope[:3]))
    weak = []
    if len(d["body"]) < 200:
        weak.append("very short description")
    if not d["address"] and not ADDRESS_RE.search(d["header"] + " " + d["body"]):
        weak.append("no address")
    below = bool(price and price < BELOW_MARKET[bkey])
    if below:
        weak.append("well below market price")
    if stale_dates(post_low):
        # Old open-house/availability dates = text copied from an old ad.
        if below:
            reject.append("scam: old ad text (stale dates) at a below-market price")
        else:
            weak.append("dates in the ad look old (may be copied)")
    if weak:
        flags.append("⚠️ verify before paying: " + "; ".join(weak))

    return (not reject), reject, flags


# ---------------- notifications ----------------
def notify(topic, title, message, url=None, tags=None, priority=4, label="Open listing"):
    payload = {"topic": topic, "title": title[:250], "message": message,
               "priority": priority, "tags": tags or []}
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": label, "url": url}]
    r = requests.post(NTFY_SERVER, json=payload, timeout=15)
    r.raise_for_status()


def send(args, topic, title, message, url=None, tags=None, priority=4, label="Open listing"):
    if args.dry_run or not topic:
        print(f"\n[NOTIFY] {title}\n{message}\n{url or ''}")
        return
    try:
        notify(topic, title, message, url, tags, priority, label)
    except requests.RequestException as e:
        print(f"  ! ntfy failed: {e}", file=sys.stderr)


# ---------------- state ----------------
def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
        s.setdefault("seen", {})
        s.setdefault("blocked_alerted", False)
        s.setdefault("fail_streak", 0)
        return s
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "blocked_alerted": False, "fail_streak": 0}


def save_state(state):
    seen = state["seen"]
    if len(seen) > 3000:  # keep the newest 3000
        keep = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:3000]
        state["seen"] = dict(keep)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--notify-existing", action="store_true")
    args = ap.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and not args.dry_run:
        print("NTFY_TOPIC is not set (add it as a GitHub secret). Use --dry-run to test locally.")
        sys.exit(1)

    if args.test_notify:
        send(args, topic, "Craigslist watcher test",
             "If you can read this, notifications work. You'll get new matching SF listings here.",
             tags=["house"])
        return

    state = load_state()
    first_run = not state["seen"]

    search_url = f"{SEARCH_BASE}?{urlencode(SEARCH_PARAMS)}"
    print(f"Searching: {search_url}")
    r = None
    for attempt, wait in enumerate([0, 20, 60]):
        if wait:
            print(f"  retrying search in {wait}s (HTTP {r.status_code if r is not None else 'none'})")
            time.sleep(wait)
        r = get(search_url)
        if r is not None and r.status_code == 200:
            break
    items = parse_search(r.text, r.url) if (r is not None and r.status_code == 200) else []

    if not items:
        status = r.status_code if r is not None else "no response"
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        print(f"No results parsed (HTTP {status}); failure streak {state['fail_streak']}.")
        if state["fail_streak"] >= ALERT_AFTER_FAILURES and not state["blocked_alerted"]:
            send(args, topic, "Craigslist watcher blocked for ~1 hour",
                 f"The last {state['fail_streak']} checks got HTTP {status}. Craigslist is blocking "
                 "GitHub's servers right now. It keeps retrying and will tell you when it recovers.",
                 url=search_url, tags=["warning"], priority=3, label="Open search")
            state["blocked_alerted"] = True
        if not args.dry_run:
            save_state(state)
        return
    if state["blocked_alerted"]:
        send(args, topic, "Craigslist watcher recovered",
             "Back to checking every 15 minutes.", tags=["white_check_mark"], priority=2)
    state["blocked_alerted"] = False
    state["fail_streak"] = 0
    print(f"Found {len(items)} posts on page 1.")

    new = [it for it in items if listing_key(it["url"]) not in state["seen"]]
    print(f"{len(new)} new since last run.")
    kept = []
    for it in new:
        h = it.get("hood", "").strip()
        # Only skip clearly out-of-area labels ("oakland, ca", "mission district").
        # Vague SF labels like "richmond district" go on to the map-pin check.
        vague = any(v in h for v in ("richmond", "pacific", "presidio", "heights", "hill"))
        if h and h not in GENERIC_HOODS and not vague and not any(n in h for n in NEIGHBORHOODS):
            state["seen"][listing_key(it["url"])] = now_iso()
            print(f"  - skip (neighborhood '{h}'): {it['title']}")
        else:
            kept.append(it)
    new = kept

    if first_run and not args.notify_existing:
        for it in items:
            state["seen"][listing_key(it["url"])] = now_iso()
        send(args, topic, "Craigslist watcher is live",
             f"Tracking {len(items)} current posts. You'll get a push when a new one matches "
             f"(1–2 bd, ≤${TARGET_PRICE:,} target, {MIN_SQFT}+ sq ft, in-unit W/D, parking).",
             tags=["house"], priority=3)
        if not args.dry_run:
            save_state(state)
        return

    # Scammers repost real listings at a fake low price with the same title.
    def norm_title(t):
        return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()
    top_price = {}
    for it in items:
        k = norm_title(it["title"])
        if it.get("price") and k:
            top_price[k] = max(top_price.get(k, 0), it["price"])
    for it in new:
        hi = top_price.get(norm_title(it["title"]), 0)
        if it.get("price") and hi > 1.5 * it["price"]:
            it["copy_of"] = hi

    matches = 0
    for i, it in enumerate(new[:MAX_DETAIL_FETCHES]):
        if i:
            time.sleep(random.uniform(1.5, 3.5))
        key = listing_key(it["url"])
        d = check_listing(it["url"])
        if d is None:
            print(f"  ? couldn't open {it['url']} (will retry next run)")
            continue
        state["seen"][key] = now_iso()
        if d["gone"]:
            print(f"  - gone: {it['title']}")
            continue

        keep, rejected, flags = evaluate(it, d)
        if not keep:
            print(f"  - skip ({'; '.join(rejected)}): {it['title']}")
            continue

        matches += 1
        price = d["price"] or it.get("price")
        bits = [f"${price:,}" if price else "price?",
                f"{d['beds']} bd" if d["beds"] else None,
                f"{d['sqft']:,} sq ft" if d["sqft"] else None,
                d["hood"] or it.get("hood") or None]
        line1 = " · ".join(b for b in bits if b)
        msg = line1
        if d["address"]:
            msg += f"\n📍 {d['address']}"
        if flags:
            msg += "\n" + "\n".join(f"• {f}" for f in flags)
        scammy = any("possible scam" in f for f in flags)
        send(args, topic, it["title"] or d["header"], msg, url=it["url"],
             tags=["warning"] if scammy else ["house"], priority=3 if scammy else 4)
        print(f"  + MATCH: {it['title']} | {price or '?'} | {it['url']}")

    print(f"Done. {matches} match(es) sent.")
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
