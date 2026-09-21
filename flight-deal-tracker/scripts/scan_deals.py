"""
Flight deal scanner -- checks every destination in the watchlist below against
Google Flights' cheapest-dates-in-range data, using the free `fli` package
(no signup, no API key, no payment -- pip install flights, run the `fli`
CLI). Scores each result against a percent-off-typical-price rule and an
absolute price cap, writes results for the dashboard, and emails when
something new clears the bar.

`fli` talks to Google Flights unofficially -- it's not a sanctioned API, so
treat occasional per-destination failures as normal background noise, not a
sign something's broken. Its JSON output is explicitly marked experimental
by its author, so parsing below is defensive: if a destination's response
doesn't match what we expect, that one destination is skipped and logged,
not treated as a fatal error.

Runs on a schedule via .github/workflows/scan-deals.yml. To test by hand:

    pip install -r requirements.txt
    python scripts/scan_deals.py

Requires these environment variables for email (see README.md):
    SMTP_SERVER, SMTP_PORT, SMTP_SENDER, SMTP_APP_PASSWORD, ALERT_RECIPIENTS
"""

import json
import os
import smtplib
import ssl
import subprocess
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "docs", "data")
DEALS_PATH = os.path.join(DATA_DIR, "deals.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
ALERTED_PATH = os.path.join(DATA_DIR, "alerted.json")

# The destination watchlist. Google Flights has no "anywhere" search, so
# this list stands in for it -- every one of these gets checked each scan.
# Add or remove freely. If you start seeing lots of errors in the Actions
# log, that's Google rate-limiting a list this size; trim it down.
AIRPORTS = {
    "JFK": ("New York", "United States", True),
    "LGA": ("New York", "United States", True),
    "EWR": ("Newark", "United States", True),
    "LAX": ("Los Angeles", "United States", True),
    "ORD": ("Chicago", "United States", True),
    "DFW": ("Dallas", "United States", True),
    "SFO": ("San Francisco", "United States", True),
    "SEA": ("Seattle", "United States", True),
    "DEN": ("Denver", "United States", True),
    "MIA": ("Miami", "United States", True),
    "BOS": ("Boston", "United States", True),
    "LAS": ("Las Vegas", "United States", True),
    "PHX": ("Phoenix", "United States", True),
    "MCO": ("Orlando", "United States", True),
    "IAH": ("Houston", "United States", True),
    "CLT": ("Charlotte", "United States", True),
    "MSP": ("Minneapolis", "United States", True),
    "DTW": ("Detroit", "United States", True),
    "PHL": ("Philadelphia", "United States", True),
    "BWI": ("Baltimore", "United States", True),
    "SAN": ("San Diego", "United States", True),
    "TPA": ("Tampa", "United States", True),
    "AUS": ("Austin", "United States", True),
    "BNA": ("Nashville", "United States", True),
    "RDU": ("Raleigh", "United States", True),
    "SAT": ("San Antonio", "United States", True),
    "MCI": ("Kansas City", "United States", True),
    "CLE": ("Cleveland", "United States", True),
    "PIT": ("Pittsburgh", "United States", True),
    "CVG": ("Cincinnati", "United States", True),
    "SLC": ("Salt Lake City", "United States", True),
    "PDX": ("Portland", "United States", True),
    "HNL": ("Honolulu", "United States", True),
    "OGG": ("Maui", "United States", True),
    "SJU": ("San Juan", "Puerto Rico", True),
    "LHR": ("London", "United Kingdom", False),
    "CDG": ("Paris", "France", False),
    "FCO": ("Rome", "Italy", False),
    "MAD": ("Madrid", "Spain", False),
    "BCN": ("Barcelona", "Spain", False),
    "ATH": ("Athens", "Greece", False),
    "AMS": ("Amsterdam", "Netherlands", False),
    "FRA": ("Frankfurt", "Germany", False),
    "MUC": ("Munich", "Germany", False),
    "ZRH": ("Zurich", "Switzerland", False),
    "DUB": ("Dublin", "Ireland", False),
    "LIS": ("Lisbon", "Portugal", False),
    "CUN": ("Cancun", "Mexico", False),
    "MEX": ("Mexico City", "Mexico", False),
    "PUJ": ("Punta Cana", "Dominican Republic", False),
    "NAS": ("Nassau", "Bahamas", False),
    "MBJ": ("Montego Bay", "Jamaica", False),
    "PTY": ("Panama City", "Panama", False),
    "BOG": ("Bogota", "Colombia", False),
    "LIM": ("Lima", "Peru", False),
    "GRU": ("Sao Paulo", "Brazil", False),
    "EZE": ("Buenos Aires", "Argentina", False),
    "SCL": ("Santiago", "Chile", False),
    "YYZ": ("Toronto", "Canada", False),
    "YVR": ("Vancouver", "Canada", False),
    "NRT": ("Tokyo", "Japan", False),
    "ICN": ("Seoul", "South Korea", False),
    "HKG": ("Hong Kong", "Hong Kong", False),
    "SIN": ("Singapore", "Singapore", False),
    "DXB": ("Dubai", "United Arab Emirates", False),
    "DOH": ("Doha", "Qatar", False),
    "TLV": ("Tel Aviv", "Israel", False),
    "CPT": ("Cape Town", "South Africa", False),
    "JNB": ("Johannesburg", "South Africa", False),
    "IST": ("Istanbul", "Turkey", False),
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def run_fli_dates(origin, destination, start_date, end_date, config):
    cmd = [
        "fli", "dates", origin, destination,
        "--from", start_date,
        "--to", end_date,
        "--format", "json",
    ]
    if config.get("round_trip", True):
        cmd.append("--round")
        duration = config.get("trip_duration_days")
        if duration:
            cmd += ["--duration", str(duration)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None, "timed out"

    # fli can exit non-zero while still printing a useful JSON error body
    # (e.g. a 403/rate-limit explanation), so try to parse stdout as JSON
    # before falling back to the raw exit code -- the JSON message is far
    # more useful for figuring out what actually went wrong.
    data = None
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None

    if data is None:
        detail = result.stderr.strip() or result.stdout.strip()
        return None, f"exit code {result.returncode}: {detail[:200]}"

    if not data.get("success", False):
        err = data.get("error", {}) or {}
        return None, err.get("message", "unknown error")

    return data, None


def extract_cheapest(data, duration_days):
    """
    Defensive parsing: fli's JSON schema is marked experimental by its
    author, so this tries a few plausible shapes rather than assuming one
    exact structure. Returns (price, departure_date, return_date) or None.
    """
    candidates = data.get("data")
    if candidates is None:
        candidates = data.get("results")
    if candidates is None:
        candidates = data.get("dates")
    if isinstance(candidates, dict):
        candidates = candidates.get("dates") or candidates.get("results") or []
    if not isinstance(candidates, list):
        return None

    best = None
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        price = entry.get("price")
        if price is None:
            price = entry.get("lowest_price")
        if price is None:
            price = entry.get("total_price")
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue

        date = entry.get("date") or entry.get("departure_date") or entry.get("travel_date")
        return_date = entry.get("return_date") or entry.get("date_return")

        if best is None or price < best[0]:
            best = (price, date, return_date)

    if best is None:
        return None

    price, date, return_date = best
    if return_date is None and date and duration_days:
        try:
            return_date = (
                datetime.strptime(date, "%Y-%m-%d") + timedelta(days=duration_days)
            ).strftime("%Y-%m-%d")
        except ValueError:
            return_date = None

    return price, date, return_date


def classify(code):
    info = AIRPORTS.get(code)
    if info:
        city, country, domestic = info
        return city, country, domestic
    return code, "Unknown", False


def evaluate(code, price, history, config):
    city, country, domestic = classify(code)
    cap = config["domestic_price_cap"] if domestic else config["international_price_cap"]
    hits_cap = price <= cap

    past_prices = [h["price"] for h in history.get(code, [])]
    percent_off = None
    hits_percent = False
    if len(past_prices) >= config.get("min_history_points", 5):
        avg = sum(past_prices) / len(past_prices)
        if avg > 0:
            percent_off = round((1 - price / avg) * 100, 1)
            hits_percent = percent_off >= config["percent_off_threshold"]

    return {
        "city": city,
        "country": country,
        "domestic": domestic,
        "hits_cap": hits_cap,
        "hits_percent": hits_percent,
        "percent_off": percent_off,
        "deal": hits_cap or hits_percent,
    }


def should_send_alert(prev, price, config):
    if prev is None:
        return True
    prev_date = datetime.fromisoformat(prev["date"])
    days_since = (datetime.now(timezone.utc) - prev_date).days
    price_dropped_further = price < prev["price"] * config.get("re_alert_price_drop_ratio", 0.95)
    stale = days_since >= config.get("re_alert_after_days", 14)
    return price_dropped_further or stale


def send_email(subject, body):
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    sender = os.environ["SMTP_SENDER"]
    password = os.environ["SMTP_APP_PASSWORD"]
    recipients = [r.strip() for r in os.environ["ALERT_RECIPIENTS"].split(",") if r.strip()]
    if not recipients:
        print("No ALERT_RECIPIENTS configured, skipping email.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls(context=context)
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"Sent alert email to {recipients}")


def main():
    config = load_json(CONFIG_PATH, {})
    origin = config.get("origin", "ATL")
    lead_days = config.get("search_lead_days", 3)
    window_days = config.get("search_window_days", 60)
    duration_days = config.get("trip_duration_days", 6)
    pause = config.get("seconds_between_requests", 1.5)

    start_date = (datetime.now(timezone.utc) + timedelta(days=lead_days)).strftime("%Y-%m-%d")
    end_date = (datetime.now(timezone.utc) + timedelta(days=lead_days + window_days)).strftime("%Y-%m-%d")

    history = load_json(HISTORY_PATH, {})
    alerted = load_json(ALERTED_PATH, {})

    now = datetime.now(timezone.utc).isoformat()
    deals = []
    new_alerts = []
    errors = []

    destinations = [code for code in AIRPORTS if code != origin]
    print(f"Scanning {len(destinations)} destinations from {origin} ({start_date} to {end_date})")

    for code in destinations:
        data, err = run_fli_dates(origin, code, start_date, end_date, config)
        if err:
            errors.append(f"{code}: {err}")
            time.sleep(pause)
            continue

        parsed = extract_cheapest(data, duration_days)
        if parsed is None:
            errors.append(f"{code}: no usable price data in response")
            time.sleep(pause)
            continue

        price, departure_date, return_date = parsed
        result = evaluate(code, price, history, config)

        record = {
            "destination": code,
            "city": result["city"],
            "country": result["country"],
            "domestic": result["domestic"],
            "departure_date": departure_date,
            "return_date": return_date,
            "price": price,
            "percent_off": result["percent_off"],
            "deal": result["deal"],
            "scanned_at": now,
        }
        deals.append(record)

        history.setdefault(code, []).append({"price": price, "date": now})
        history[code] = history[code][-30:]

        if result["deal"]:
            prev = alerted.get(code)
            if should_send_alert(prev, price, config):
                new_alerts.append(record)
                alerted[code] = {"price": price, "date": now}

        time.sleep(pause)

    deals.sort(key=lambda d: (not d["deal"], d["price"]))

    save_json(DEALS_PATH, {
        "generated_at": now,
        "origin": origin,
        "deals": deals,
        "checked": len(destinations),
        "failed": len(errors),
    })
    save_json(HISTORY_PATH, history)
    save_json(ALERTED_PATH, alerted)

    print(f"{len(deals)} OK, {len(errors)} failed, {sum(d['deal'] for d in deals)} flagged, {len(new_alerts)} new alerts")
    if errors:
        print("Failures (first 10):", "; ".join(errors[:10]))

    if new_alerts:
        required_env = ["SMTP_SERVER", "SMTP_SENDER", "SMTP_APP_PASSWORD", "ALERT_RECIPIENTS"]
        missing = [v for v in required_env if not os.environ.get(v)]
        if missing:
            print(
                f"Email not configured yet (missing {', '.join(missing)}) -- "
                f"skipping alert email. {len(new_alerts)} deal(s) found; check the dashboard."
            )
        else:
            lines = [
                f"{d['city']} ({d['destination']}) -- ${d['price']:.0f}, "
                f"{d['departure_date']}" + (f" to {d['return_date']}" if d["return_date"] else "")
                + (f" ({d['percent_off']}% below typical)" if d["percent_off"] else "")
                for d in new_alerts
            ]
            body = f"New flight deals from {origin}:\n\n" + "\n".join(lines)
            body += "\n\nFull list: check the dashboard."
            send_email(f"{len(new_alerts)} new flight deal(s) from {origin}", body)


if __name__ == "__main__":
    main()
