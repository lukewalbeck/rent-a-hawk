# rent-a-hawk 🦅

Watches SF rental listings like a hawk. Starting with Craigslist; Trulia/Zillow email alerts coming later.

## Zillow / Trulia / Apartments.com alerts (email_watch.py)

Your saved searches on those sites email you instantly. Each run, rent-a-hawk reads new
alert emails over Gmail IMAP (**read-only** — it never marks mail read, moves or deletes
anything), pulls out each listing, drops ones over budget / under 750 sq ft / wrong bed
count / shared laundry / no parking, de-dupes the same place across sites, and pushes the
rest to ntfy with a link straight to the listing.

Setup: add repo secrets `GMAIL_USER` (your Gmail address) and `GMAIL_APP_PASSWORD`
(create one at https://myaccount.google.com/apppasswords — needs 2-Step Verification).
The first run records alerts already in your inbox without pushing them.

## Craigslist watcher

Checks Craigslist every 20 minutes (6am–11pm Pacific), opens every **new** post, and
pushes the ones that pass your must-haves to your phone. Free: GitHub Actions + ntfy.

**Must-haves (reject if they fail):** Marina / Cow Hollow / Pac Heights / Presidio Heights /
Laurel Heights / Inner Richmond / Russian Hill / North Beach / Telegraph Hill · 1–2 bd ·
≤ $5,800 · 750+ sq ft (when listed) · in-unit W/D (not shared) · real parking (not street-only). Laundry and parking are read from each post's text, not Craigslist checkboxes.

**Flags in the notification:** over $5,500 target · parking may cost extra · no dishwasher
mentioned · rent-controlled ✅ · sq ft / laundry / parking unclear · ⚠️ possible scam
(short description, no address, very low price, "wire money / out of town" language).

## Setup (about 10 minutes, from a computer)

1. **Phone:** install **ntfy** from the App Store. Tap **+** and subscribe to a topic with a
   hard-to-guess name, e.g. `walbeck_rent_a_hawk`. (Anyone who knows the name can read it.)
2. **GitHub:** create a new **private** repo (`rent-a-hawk`) and upload these files,
   keeping the `.github/workflows/watch.yml` path:
   ```bash
   cd rent-a-hawk
   git init && git add . && git commit -m "Craigslist watcher"
   git branch -M main
   git remote add origin git@github.com:lukewalbeck/rent-a-hawk.git
   git push -u origin main
   ```
3. **Secret:** repo **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `NTFY_TOPIC` — Value: your topic name from step 1.
4. **Test:** **Actions** tab → *craigslist-watch* → **Run workflow** → mode `test-notify`.
   You should get a push within a few seconds.
5. **Start:** run it once more with mode `normal`. The first run just records what's already
   posted and sends "watcher is live" (use `notify-existing` instead if you want the current
   matches too). After that it runs on its own.

## Tweaking

Edit the block at the top of `cl_watch.py`: `max_price`, `TARGET_PRICE`, `MIN_SQFT`,
`NEIGHBORHOODS`. Commit and push — the next run uses the new settings.

## Cost / limits

- Private repos get 2,000 free Actions minutes/month; this uses about 1,600. A **public**
  repo has no limit (the only thing visible would be `seen.json`; your topic stays secret).
- GitHub's scheduler can run a few minutes late at busy times.
- Times are in UTC, so in summer (daylight time) the window shifts an hour later.

## If you get "⚠️ Craigslist watcher can't read results"

Craigslist sometimes blocks cloud servers. Run it from your Mac instead (same alerts):
```bash
cd ~/rent-a-hawk && pip3 install -r requirements.txt
NTFY_TOPIC=your-topic python3 cl_watch.py --dry-run   # check it works
crontab -e
# add this line (every 15 min, 6am–11pm):
*/15 6-23 * * * cd ~/rent-a-hawk && NTFY_TOPIC=your-topic /usr/bin/python3 cl_watch.py >> watch.log 2>&1
```
(Only runs while the Mac is awake.) Then disable the GitHub workflow.

## Local commands

```bash
python cl_watch.py --dry-run          # print what it would send; doesn't save
python cl_watch.py --test-notify      # one test push (needs NTFY_TOPIC)
```
