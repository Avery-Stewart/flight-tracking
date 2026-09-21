# ATL Flight Deal Tracker

A free, self-hosted alternative to Airclub for you and one other person:
a scheduled scan checks a watchlist of destinations from ATL against
Google Flights, a dashboard on GitHub Pages lets you both browse and
filter the results, and an automatic email fires when something clears
your "great deal" bar. No signup, no API key, no payment anywhere in
this setup.

**How it works:** GitHub Actions runs `scripts/scan_deals.py` twice a day.
For each destination in a built-in watchlist (~70 places), it uses the
free `fli` tool to ask Google Flights for the cheapest dates to fly there
over the next couple months. It scores each result, writes everything to
`docs/data/deals.json`, and commits that file back to the repo. GitHub
Pages serves `docs/index.html`, which reads that JSON and renders it —
no server, no cost, no account anywhere in the pipeline.

A deal is flagged when **either** condition is true:
- the price is **40%+ below** that destination's own recent average (needs
  a handful of prior scans of that destination to kick in)
- the price is under **$150 for domestic** or **$400 for international**

All of these numbers, plus the destination list itself, live in
`config.json` and `scripts/scan_deals.py` — edit either any time, no
special tools needed.

## Why this is different from the original plan

The first version of this used Amadeus's free developer API. Amadeus shut
that program down entirely in July 2026 — there's no free signup path
left there anymore. This version replaces it with `fli`, a free tool that
talks to Google Flights directly. The trade-off worth knowing:

- **No cost, no signup, no card** — genuinely free, matching the actual
  goal here (not paying an Airclub-style fee)
- **No "any destination" search** — Google Flights doesn't offer that, so
  instead of true open-ended discovery, this checks a fixed (but large
  and editable) list of destinations every scan
- **Unofficial** — `fli` isn't a sanctioned API, so occasional failures
  for individual destinations are normal, not a sign something's broken.
  The script is written to skip a failed destination and keep going
  rather than let one bad response break the whole scan

## 1. Create the repo

Push everything in this folder to a new GitHub repo (public is fine —
nothing in it is sensitive, it's just flight prices and code).

## 2. Set up email sending

This is the only account you need to create. Easiest option is a Gmail
account (a new one just for this, or your own) with an **App Password**
(not your regular password):

1. Turn on 2-Step Verification on the Google account
2. Go to Google Account → Security → App Passwords, create one for "Mail"
3. Use `smtp.gmail.com`, port `587`, and that app password

Any other SMTP provider works the same way if you'd rather use something
else.

## 3. Add repo secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add all of these:

| Secret | Example |
|---|---|
| `SMTP_SERVER` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_SENDER` | the Gmail address sending the alerts |
| `SMTP_APP_PASSWORD` | the app password from step 2 |
| `ALERT_RECIPIENTS` | both email addresses, comma-separated |

## 4. Turn on GitHub Pages

**Settings → Pages** → Source: "Deploy from a branch" → Branch: `main`,
folder: `/docs` → Save. GitHub gives you a URL like
`https://yourusername.github.io/repo-name/` — that's the dashboard link
to bookmark and share with the other person.

## 5. Run it

The workflow runs automatically twice a day, but for the first run (so
the dashboard isn't empty), go to the **Actions** tab → "Scan flight
deals" → **Run workflow**. It checks ~70 destinations one at a time, so
it'll take a few minutes — that's normal.

## Customizing

- **Thresholds**: edit `config.json` — `percent_off_threshold`,
  `domestic_price_cap`, `international_price_cap`
- **Destination list**: edit the `AIRPORTS` dictionary at the top of
  `scripts/scan_deals.py` — add or remove any airport code, city, country
- **Search window**: `search_lead_days` / `search_window_days` in
  `config.json` control how far out it looks for cheap dates
- **Trip length**: `round_trip` and `trip_duration_days` in `config.json`
- **Scan frequency**: edit the `cron` line in
  `.github/workflows/scan-deals.yml`

## Troubleshooting

- **A run finishes but most/all destinations show as failed**: open the
  run in the Actions tab and check the "Run scanner" step's log — it
  prints the actual error message for each failed destination (rate
  limiting looks different from a malformed request, for instance). If
  you're not sure what it means, paste that log back to me.
- **Everything fails at once, consistently**: `fli`'s JSON output is
  explicitly marked experimental by its author, so it's possible Google
  changed something and the response shape needs a small update in
  `extract_cheapest()` in `scripts/scan_deals.py`. Again — paste the
  error log and I can help adjust it.
- **Too many failures / feels like it's getting blocked**: trim the
  `AIRPORTS` list down to fewer destinations, or reduce scan frequency
  in the workflow's `cron` line.

## Limitations, honestly

- This uses an unofficial method of reading Google Flights' data, not a
  supported API — there's no SLA, no guarantee, and it can break if
  Google changes something on their end
- No "any destination" discovery — you're seeing prices for the
  destinations in the watchlist, not literally everywhere
- No nonstop/cabin-class filtering surfaced in the dashboard yet, though
  `fli` itself supports both if you want to add that filtering into the
  script later
