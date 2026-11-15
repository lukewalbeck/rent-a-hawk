#!/usr/bin/env python3
"""
Zillow / Trulia / Apartments.com saved-search alert emails -> ntfy pushes.

Reads new alert emails over Gmail IMAP (read-only: never marks mail read,
moves or deletes anything), pulls each listing out of the email, drops ones
that fail the must-haves, de-dupes across sites, and pushes the rest to ntfy.

Needs repo secrets GMAIL_USER and GMAIL_APP_PASSWORD (a Gmail *app password*,
not your normal password). Without them it exits quietly.

Usage:
  python email_watch.py              # normal run
  python email_watch.py --dry-run    # print what it would send; don't save
  python email_watch.py --notify-existing   # first run: also alert on alerts already in the inbox
"""
import argparse
import email
import imaplib
import os
import re
import sys
from email.header import decode_header, make_header
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

import cl_watch as cw  # shared: notify/send, state, settings

# ============================================================
SEARCH = ('from:(zillow.com OR trulia.com OR apartments.com) '
          '-from:comet.zillow.com newer_than:3d')   # comet.zillow.com = your rent payments
MAX_PRICE = 5800
TARGET_PRICE = cw.TARGET_PRICE
MIN_SQFT = cw.MIN_SQFT
BEDS = (1, 2)
# ============================================================

LISTING_RE = re.compile(
    r"https?://(?:www\.)?(?:zillow\.com/(?:homedetails|apartments|b|rental-manager/listings)/"
    r"|trulia\.com/(?:home|p|building|rental|rental-community)/"
    r"|apartments\.com/[a-z0-9-]+/[a-z0-9]+/?$)", re.I)
SITE_HOSTS = ("zillow.com", "trulia.com", "apartments.com")
PRICE_RE = re.compile(r"\$\s?(\d{1,2},?\d{3})(?:\s*[-–]\s*\$?\s?(\d{1,2},?\d{3}))?")
BEDS_RE = re.compile(r"(\d)\s*(?:bd|bds|beds?|br)\b", re.I)
STUDIO_RE = re.compile(r"\bstudio\b", re.I)
SQFT_RE = re.compile(r"([\d,]{3,6})\s*(?:sq\.?\s?ft|sqft|ft2|ft²)", re.I)
ADDR_RE = re.compile(
    r"\b\d{1,5}[A-Za-z]?(?:-\d{1,5})?\s+(?:[A-Z0-9][\w.'-]*\s){0,4}"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Way|Ter|Terrace|Pl|Place|Ct|Court|Dr|Drive|"
    r"Ln|Lane|Rd|Road|Aly|Alley)\b[^,\n]{0,25}(?:,\s*San Francisco)?(?:,\s*CA)?(?:\s*\d{5})?")
SHARED_LAUNDRY = cw.SHARED_LAUNDRY
NO_PARKING = ["street parking only", "no parking"]


def decode(s):
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme or "https", p.netloc.lower().replace("www.", ""), p.path.rstrip("/"), "", ""))


def resolve(url, cache={}):
    """Turn an email click-tracking link into the real listing URL (follows redirects
    only until it reaches the listing site; never loads the listing page itself)."""
    if url in cache:
        return cache[url]
    if LISTING_RE.search(url):
        cache[url] = url
        return url
    cur = url
    for _ in range(6):
        try:
            r = requests.get(cur, allow_redirects=False, timeout=10,
                             headers={"User-Agent": cw.UA})
        except requests.RequestException:
            break
        loc = r.headers.get("Location")
        if not loc:
            break
        cur = requests.compat.urljoin(cur, loc)
        if LISTING_RE.search(cur):
            cache[url] = cur
            return cur
        if any(h in urlsplit(cur).netloc for h in SITE_HOSTS):
            break
    cache[url] = None
    return None


def norm_addr(a):
    a = (a or "").lower()
    a = re.sub(r"\b(street)\b", "st", a)
    a = re.sub(r"\b(avenue)\b", "ave", a)
    a = re.sub(r"(,?\s*san francisco.*$)|[#.,]", " ", a)
    a = re.sub(r"\b(apt|unit|ste)\b", " ", a)
    return re.sub(r"\s+", " ", a).strip()


def card_text(a_tag):
    """Text of the smallest ancestor that looks like one listing card."""
    node, best = a_tag, a_tag.get_text(" ", strip=True)
    for _ in range(8):
        node = node.parent
        if node is None:
            break
        t = node.get_text(" ", strip=True)
        if len(t) > 700:
            break
        best = t
        if PRICE_RE.search(t) and (ADDR_RE.search(t) or BEDS_RE.search(t)):
            break
    return best


def parse_email(msg):
    html = text = ""
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ct == "text/html" and not html:
            html = payload
        elif ct == "text/plain" and not text:
            text = payload

    listings = {}
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http"):
                continue
            host = urlsplit(href).netloc.lower()
            # skip obvious non-listing links (unsubscribe, settings, app stores…)
            if any(k in href.lower() for k in ("unsubscribe", "preferences", "settings", "apps.apple",
                                                "play.google", "privacy", "terms", "help")):
                continue
            url = resolve(href) if (LISTING_RE.search(href) or "click" in host or "link" in host
                                    or "email" in host or "mail" in host or "trk" in host) else None
            if not url:
                continue
            key = canonical(url)
            blob = card_text(a)
            cur = listings.get(key)
            if cur is None or (len(blob) > len(cur["blob"]) and len(blob) < 700):
                listings[key] = {"url": url, "blob": blob}
    elif text:
        for m in re.finditer(r"https?://\S+", text):
            url = resolve(m.group(0).rstrip(">)."))
            if url:
                s = max(0, m.start() - 300)
                listings[canonical(url)] = {"url": url, "blob": text[s:m.start()]}

    out = []
    for key, li in listings.items():
        b = li["blob"]
        pm = PRICE_RE.search(b)
        price = int(pm.group(1).replace(",", "")) if pm else None
        bm = BEDS_RE.search(b)
        beds = 0 if STUDIO_RE.search(b) else (int(bm.group(1)) if bm else None)
        sm = SQFT_RE.search(b)
        sqft = int(sm.group(1).replace(",", "")) if sm else None
        am = ADDR_RE.search(b)
        out.append({"key": key, "url": li["url"], "price": price, "beds": beds, "sqft": sqft,
                    "address": am.group(0).strip() if am else "", "blob": b})
    return out


def evaluate(li):
    reject, flags = [], []
    if li["price"] and li["price"] > MAX_PRICE:
        reject.append(f"${li['price']:,}")
    if li["beds"] is not None and not (BEDS[0] <= li["beds"] <= BEDS[1]):
        reject.append(f"{li['beds']} bd")
    if li["sqft"] and li["sqft"] < MIN_SQFT:
        reject.append(f"{li['sqft']} sq ft")
    low = li["blob"].lower()
    if any(k in low for k in SHARED_LAUNDRY):
        reject.append("shared laundry")
    if any(k in low for k in NO_PARKING):
        reject.append("no parking")
    if li["price"] and li["price"] > TARGET_PRICE:
        flags.append(f"over ${TARGET_PRICE:,} target")
    if li["sqft"] is None:
        flags.append("sq ft not in email")
    if not li["price"]:
        flags.append("price not in email")
    return not reject, reject, flags


def site_of(url):
    h = urlsplit(url).netloc
    return "Zillow" if "zillow" in h else "Trulia" if "trulia" in h else "Apartments.com" if "apartments" in h else "Listing"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify-existing", action="store_true")
    args = ap.parse_args()

    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not (user and pw):
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set; skipping email check.")
        return

    state = cw.load_state()
    est = state.setdefault("email", {"msgs": {}, "listings": {}, "addrs": {}, "auth_alerted": False})
    first_run = not est["msgs"]

    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pw)
    except imaplib.IMAP4.error as e:
        print(f"Gmail login failed: {e}")
        if not est.get("auth_alerted"):
            cw.send(args, topic, "rent-a-hawk can't log in to Gmail",
                    "The Gmail app password was rejected. Create a new one and update the "
                    "GMAIL_APP_PASSWORD secret in the repo.", tags=["warning"], priority=3)
            est["auth_alerted"] = True
            if not args.dry_run:
                cw.save_state(state)
        return
    est["auth_alerted"] = False

    M.select('"[Gmail]/All Mail"', readonly=True)
    typ, data = M.search(None, "X-GM-RAW", f'"{SEARCH}"')
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    print(f"{len(ids)} alert emails in the last 3 days.")

    new_msgs = []
    for i in ids:
        typ, hdr = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM)])")
        h = email.message_from_bytes(hdr[0][1]) if typ == "OK" else None
        mid = (h.get("Message-ID") if h else None) or i.decode()
        if mid not in est["msgs"]:
            new_msgs.append((i, mid, decode(h.get("Subject", "")) if h else ""))
    print(f"{len(new_msgs)} new.")

    sent = 0
    for i, mid, subject in new_msgs:
        typ, raw = M.fetch(i, "(BODY.PEEK[])")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(raw[0][1])
        listings = parse_email(msg)
        print(f"  email: {subject!r} -> {len(listings)} listing link(s)")
        for li in listings:
            akey = norm_addr(li["address"])
            if li["key"] in est["listings"] or (akey and akey in est["addrs"]):
                print(f"    = already seen: {li['address'] or li['url']}")
                continue
            est["listings"][li["key"]] = cw.now_iso()
            if akey:
                est["addrs"][akey] = cw.now_iso()
            if first_run and not args.notify_existing:
                continue
            keep, rejected, flags = evaluate(li)
            if not keep:
                print(f"    - skip ({'; '.join(rejected)}): {li['address'] or li['url']}")
                continue
            bits = [f"${li['price']:,}" if li["price"] else None,
                    f"{li['beds']} bd" if li["beds"] else None,
                    f"{li['sqft']:,} sq ft" if li["sqft"] else None, site_of(li["url"])]
            body = " · ".join(b for b in bits if b)
            if flags:
                body += "\n" + "\n".join(f"• {f}" for f in flags)
            title = li["address"] or f"New {site_of(li['url'])} listing"
            cw.send(args, topic, title, body, url=li["url"], tags=["house"], priority=4)
            sent += 1
            print(f"    + MATCH: {title} | {li['price'] or '?'} | {li['url']}")
        est["msgs"][mid] = cw.now_iso()

    M.logout()
    for k in ("msgs", "listings", "addrs"):  # keep state small
        if len(est[k]) > 2000:
            est[k] = dict(sorted(est[k].items(), key=lambda kv: kv[1], reverse=True)[:2000])
    if first_run and not args.notify_existing:
        print("First email run: recorded existing alerts without notifying.")
    print(f"Done. {sent} email match(es) sent.")
    if not args.dry_run:
        cw.save_state(state)


if __name__ == "__main__":
    main()
