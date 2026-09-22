"""
Flight deal scanner -- checks every destination in the watchlist below against
Google Flights' cheapest-dates-in-range data, using the free `fli` package
(no signup, no API key, no payment). Scores each result against a
percent-off-typical-price rule and an absolute price cap, writes results for
the dashboard, and emails when something new clears the bar.

For each destination it searches once per trip length in config.json
("trip_lengths"), and keeps the cheapest N date combinations it sees rather
than just the single best one. Keeping them costs nothing extra -- fli
already returns a price for every date in the range -- and it is what lets
the dashboard filter by date, season and trip length.

`fli` talks to Google Flights unofficially -- it's not a sanctioned API, so
treat occasional per-destination failures as normal background noise, not a
sign something's broken. Its JSON output is explicitly marked experimental
by its author, so parsing below is defensive: if a response doesn't match
what we expect, that one lookup is skipped and logged, not treated as fatal.

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
# this list stands in for it -- every one of these gets checked each scan,
# once per trip length. Add or remove freely, but remember the cost: each
# destination is (number of trip lengths) requests per scan. If failures
# start showing up in the Actions log, that's Google rate-limiting; trim
# this list or cut a trip length from config.json.
AIRPORTS = {
    # --- United States & territories ---
    "JFK": ("New York", "United States", True),
    "LGA": ("New York", "United States", True),
    "EWR": ("Newark", "United States", True),
    "BOS": ("Boston", "United States", True),
    "PHL": ("Philadelphia", "United States", True),
    "BWI": ("Baltimore", "United States", True),
    "DCA": ("Washington", "United States", True),
    "IAD": ("Washington Dulles", "United States", True),
    "RDU": ("Raleigh", "United States", True),
    "CLT": ("Charlotte", "United States", True),
    "RIC": ("Richmond", "United States", True),
    "ORF": ("Norfolk", "United States", True),
    "CHS": ("Charleston", "United States", True),
    "SAV": ("Savannah", "United States", True),
    "MYR": ("Myrtle Beach", "United States", True),
    "GSP": ("Greenville", "United States", True),
    "AVL": ("Asheville", "United States", True),
    "BHM": ("Birmingham", "United States", True),
    "BNA": ("Nashville", "United States", True),
    "MEM": ("Memphis", "United States", True),
    "MSY": ("New Orleans", "United States", True),
    "MIA": ("Miami", "United States", True),
    "FLL": ("Fort Lauderdale", "United States", True),
    "PBI": ("West Palm Beach", "United States", True),
    "MCO": ("Orlando", "United States", True),
    "TPA": ("Tampa", "United States", True),
    "RSW": ("Fort Myers", "United States", True),
    "JAX": ("Jacksonville", "United States", True),
    "PNS": ("Pensacola", "United States", True),
    "ORD": ("Chicago", "United States", True),
    "MDW": ("Chicago Midway", "United States", True),
    "DTW": ("Detroit", "United States", True),
    "CLE": ("Cleveland", "United States", True),
    "CVG": ("Cincinnati", "United States", True),
    "CMH": ("Columbus", "United States", True),
    "IND": ("Indianapolis", "United States", True),
    "PIT": ("Pittsburgh", "United States", True),
    "MKE": ("Milwaukee", "United States", True),
    "MSP": ("Minneapolis", "United States", True),
    "STL": ("St. Louis", "United States", True),
    "MCI": ("Kansas City", "United States", True),
    "OMA": ("Omaha", "United States", True),
    "DFW": ("Dallas", "United States", True),
    "IAH": ("Houston", "United States", True),
    "AUS": ("Austin", "United States", True),
    "SAT": ("San Antonio", "United States", True),
    "OKC": ("Oklahoma City", "United States", True),
    "DEN": ("Denver", "United States", True),
    "SLC": ("Salt Lake City", "United States", True),
    "ABQ": ("Albuquerque", "United States", True),
    "PHX": ("Phoenix", "United States", True),
    "TUS": ("Tucson", "United States", True),
    "LAS": ("Las Vegas", "United States", True),
    "RNO": ("Reno", "United States", True),
    "BOI": ("Boise", "United States", True),
    "LAX": ("Los Angeles", "United States", True),
    "SNA": ("Orange County", "United States", True),
    "SAN": ("San Diego", "United States", True),
    "SFO": ("San Francisco", "United States", True),
    "SJC": ("San Jose", "United States", True),
    "SMF": ("Sacramento", "United States", True),
    "PDX": ("Portland", "United States", True),
    "SEA": ("Seattle", "United States", True),
    "ANC": ("Anchorage", "United States", True),
    "HNL": ("Honolulu", "United States", True),
    "OGG": ("Maui", "United States", True),
    "KOA": ("Kona", "United States", True),
    "SJU": ("San Juan", "Puerto Rico", True),
    "STT": ("St. Thomas", "US Virgin Islands", True),
    # --- Canada ---
    "YYZ": ("Toronto", "Canada", False),
    "YUL": ("Montreal", "Canada", False),
    "YVR": ("Vancouver", "Canada", False),
    "YYC": ("Calgary", "Canada", False),
    # --- Mexico, Caribbean & Central America ---
    "CUN": ("Cancun", "Mexico", False),
    "MEX": ("Mexico City", "Mexico", False),
    "SJD": ("Los Cabos", "Mexico", False),
    "PVR": ("Puerto Vallarta", "Mexico", False),
    "GDL": ("Guadalajara", "Mexico", False),
    "MTY": ("Monterrey", "Mexico", False),
    "PUJ": ("Punta Cana", "Dominican Republic", False),
    "SDQ": ("Santo Domingo", "Dominican Republic", False),
    "NAS": ("Nassau", "Bahamas", False),
    "MBJ": ("Montego Bay", "Jamaica", False),
    "KIN": ("Kingston", "Jamaica", False),
    "AUA": ("Aruba", "Aruba", False),
    "CUR": ("Curacao", "Curacao", False),
    "SXM": ("St. Maarten", "Sint Maarten", False),
    "BGI": ("Bridgetown", "Barbados", False),
    "ANU": ("Antigua", "Antigua and Barbuda", False),
    "PLS": ("Providenciales", "Turks and Caicos", False),
    "GCM": ("Grand Cayman", "Cayman Islands", False),
    "BZE": ("Belize City", "Belize", False),
    "SJO": ("San Jose", "Costa Rica", False),
    "LIR": ("Liberia", "Costa Rica", False),
    "GUA": ("Guatemala City", "Guatemala", False),
    "PTY": ("Panama City", "Panama", False),
    # --- South America ---
    "BOG": ("Bogota", "Colombia", False),
    "MDE": ("Medellin", "Colombia", False),
    "CTG": ("Cartagena", "Colombia", False),
    "UIO": ("Quito", "Ecuador", False),
    "LIM": ("Lima", "Peru", False),
    "CUZ": ("Cusco", "Peru", False),
    "SCL": ("Santiago", "Chile", False),
    "EZE": ("Buenos Aires", "Argentina", False),
    "MVD": ("Montevideo", "Uruguay", False),
    "GRU": ("Sao Paulo", "Brazil", False),
    "GIG": ("Rio de Janeiro", "Brazil", False),
    # --- Europe ---
    "LHR": ("London", "United Kingdom", False),
    "LGW": ("London Gatwick", "United Kingdom", False),
    "MAN": ("Manchester", "United Kingdom", False),
    "EDI": ("Edinburgh", "United Kingdom", False),
    "DUB": ("Dublin", "Ireland", False),
    "CDG": ("Paris", "France", False),
    "NCE": ("Nice", "France", False),
    "AMS": ("Amsterdam", "Netherlands", False),
    "BRU": ("Brussels", "Belgium", False),
    "FRA": ("Frankfurt", "Germany", False),
    "MUC": ("Munich", "Germany", False),
    "BER": ("Berlin", "Germany", False),
    "ZRH": ("Zurich", "Switzerland", False),
    "VIE": ("Vienna", "Austria", False),
    "PRG": ("Prague", "Czechia", False),
    "BUD": ("Budapest", "Hungary", False),
    "WAW": ("Warsaw", "Poland", False),
    "CPH": ("Copenhagen", "Denmark", False),
    "ARN": ("Stockholm", "Sweden", False),
    "OSL": ("Oslo", "Norway", False),
    "HEL": ("Helsinki", "Finland", False),
    "KEF": ("Reykjavik", "Iceland", False),
    "LIS": ("Lisbon", "Portugal", False),
    "OPO": ("Porto", "Portugal", False),
    "MAD": ("Madrid", "Spain", False),
    "BCN": ("Barcelona", "Spain", False),
    "AGP": ("Malaga", "Spain", False),
    "PMI": ("Mallorca", "Spain", False),
    "FCO": ("Rome", "Italy", False),
    "MXP": ("Milan", "Italy", False),
    "VCE": ("Venice", "Italy", False),
    "NAP": ("Naples", "Italy", False),
    "ATH": ("Athens", "Greece", False),
    "IST": ("Istanbul", "Turkey", False),
    # --- Middle East & Africa ---
    "TLV": ("Tel Aviv", "Israel", False),
    "DXB": ("Dubai", "United Arab Emirates", False),
    "DOH": ("Doha", "Qatar", False),
    "AMM": ("Amman", "Jordan", False),
    "CAI": ("Cairo", "Egypt", False),
    "CMN": ("Casablanca", "Morocco", False),
    "NBO": ("Nairobi", "Kenya", False),
    "ACC": ("Accra", "Ghana", False),
    "LOS": ("Lagos", "Nigeria", False),
    "ADD": ("Addis Ababa", "Ethiopia", False),
    "JNB": ("Johannesburg", "South Africa", False),
    "CPT": ("Cape Town", "South Africa", False),
    # --- Asia & Pacific ---
    "NRT": ("Tokyo Narita", "Japan", False),
    "HND": ("Tokyo Haneda", "Japan", False),
    "KIX": ("Osaka", "Japan", False),
    "ICN": ("Seoul", "South Korea", False),
    "TPE": ("Taipei", "Taiwan", False),
    "HKG": ("Hong Kong", "Hong Kong", False),
    "SIN": ("Singapore", "Singapore", False),
    "BKK": ("Bangkok", "Thailand", False),
    "SGN": ("Ho Chi Minh City", "Vietnam", False),
    "HAN": ("Hanoi", "Vietnam", False),
    "MNL": ("Manila", "Philippines", False),
    "KUL": ("Kuala Lumpur", "Malaysia", False),
    "DEL": ("Delhi", "India", False),
    "BOM": ("Mumbai", "India", False),
    "SYD": ("Sydney", "Australia", False),
    "MEL": ("Melbourne", "Australia", False),
    "AKL": ("Auckland", "New Zealand", False),
    "NAN": ("Nadi", "Fiji", False),
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"))


def run_fli_dates(origin, destination, start_date, end_date, duration):
    cmd = [
        "fli", "dates", origin, destination,
        "--from", start_date,
        "--to", end_date,
        "--format", "json",
        "--round", "--duration", str(duration),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "timed out"

    # fli can exit non-zero while still printing a useful JSON error body
    # (e.g. a 403/rate-limit explanation), so try to parse stdout as JSON
    # before falling back to the raw exit code.
    data = None
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None

    if data is None:
        detail = result.stderr.strip() or result.stdout.strip()
        # Python tracebacks put the actual error on the LAST line.
        return None, f"exit code {result.returncode}: {detail[-300:]}"

    if not data.get("success", False):
        err = data.get("error", {}) or {}
        return None, err.get("message", "unknown error")

    return data, None


def extract_options(data, duration):
    """
    Pull every (price, departure date) pair out of an fli response and pair
    each with its return date. fli's JSON schema is marked experimental by
    its author, so this tries several plausible shapes rather than assuming
    one. Returns a list of dicts, possibly empty.
    """
    candidates = data.get("data")
    if candidates is None:
        candidates = data.get("results")
    if candidates is None:
        candidates = data.get("dates")
    if isinstance(candidates, dict):
        candidates = candidates.get("dates") or candidates.get("results") or []
    if not isinstance(candidates, list):
        return []

    out = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue

        price = entry.get("price")
        if price is None:
            price = entry.get("lowest_price")
        if price is None:
            price = entry.get("total_price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue

        date = entry.get("date") or entry.get("departure_date") or entry.get("travel_date")
        if not date:
            continue

        return_date = entry.get("return_date") or entry.get("date_return")
        if not return_date:
            try:
                return_date = (
                    datetime.strptime(date, "%Y-%m-%d") + timedelta(days=duration)
                ).strftime("%Y-%m-%d")
            except ValueError:
                return_date = None

        out.append({"price": round(price, 2), "dep": date, "ret": return_date, "days": duration})

    return out


def classify(code):
    info = AIRPORTS.get(code)
    if info:
        return info
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
    window_days = config.get("search_window_days", 180)
    trip_lengths = config.get("trip_lengths", [6]) or [6]
    max_options = config.get("max_options_per_destination", 40)
    pause = config.get("seconds_between_requests", 1.2)

    start_date = (datetime.now(timezone.utc) + timedelta(days=lead_days)).strftime("%Y-%m-%d")
    end_date = (datetime.now(timezone.utc) + timedelta(days=lead_days + window_days)).strftime("%Y-%m-%d")

    history = load_json(HISTORY_PATH, {})
    alerted = load_json(ALERTED_PATH, {})

    now = datetime.now(timezone.utc).isoformat()
    deals = []
    new_alerts = []
    errors = []

    destinations = [code for code in AIRPORTS if code != origin]
    total_lookups = len(destinations) * len(trip_lengths)
    print(f"Scanning {len(destinations)} destinations from {origin}, "
          f"trip lengths {trip_lengths} ({total_lookups} lookups), {start_date} to {end_date}")

    # Safety valve: if Google starts blocking us, every remaining lookup will
    # fail too. Stop early, keep whatever we already collected, and say so,
    # rather than hammering them for another 20 minutes.
    consecutive_failures = 0
    aborted = False

    for code in destinations:
        options = []
        for duration in trip_lengths:
            data, err = run_fli_dates(origin, code, start_date, end_date, duration)
            if err:
                errors.append(f"{code}/{duration}d: {err}")
                consecutive_failures += 1
            else:
                extracted = extract_options(data, duration)
                options.extend(extracted)
                if extracted:
                    consecutive_failures = 0
            time.sleep(pause)

        if consecutive_failures >= 15:
            aborted = True
            print(f"Stopping early: {consecutive_failures} lookups in a row failed. "
                  f"Most likely rate-limited. Keeping the {len(deals)} destinations collected so far.")
            break

        if not options:
            continue

        # Cheapest first, keep a manageable number for the dashboard.
        options.sort(key=lambda o: o["price"])
        options = options[:max_options]
        best = options[0]
        price = best["price"]

        result = evaluate(code, price, history, config)
        deals.append({
            "destination": code,
            "city": result["city"],
            "country": result["country"],
            "domestic": result["domestic"],
            "departure_date": best["dep"],
            "return_date": best["ret"],
            "days": best["days"],
            "price": price,
            "percent_off": result["percent_off"],
            "deal": result["deal"],
            "options": options,
            "scanned_at": now,
        })

        history.setdefault(code, []).append({"price": price, "date": now})
        history[code] = history[code][-30:]

        if result["deal"]:
            prev = alerted.get(code)
            if should_send_alert(prev, price, config):
                new_alerts.append(deals[-1])
                alerted[code] = {"price": price, "date": now}

    deals.sort(key=lambda d: (not d["deal"], d["price"]))

    save_json(DEALS_PATH, {
        "generated_at": now,
        "origin": origin,
        "trip_lengths": trip_lengths,
        "window": {"from": start_date, "to": end_date},
        "deals": deals,
        "checked": len(destinations),
        "failed_lookups": len(errors),
        "aborted_early": aborted,
    })
    save_json(HISTORY_PATH, history)
    save_json(ALERTED_PATH, alerted)

    print(f"{len(deals)} destinations with prices, {len(errors)} failed lookups, "
          f"{sum(d['deal'] for d in deals)} flagged, {len(new_alerts)} new alerts")
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
                + f", {d['days']}-day trip"
                + (f" ({d['percent_off']}% below typical)" if d["percent_off"] else "")
                for d in new_alerts
            ]
            body = f"New flight deals from {origin}:\n\n" + "\n".join(lines)
            body += "\n\nFull list: check the dashboard."
            send_email(f"{len(new_alerts)} new flight deal(s) from {origin}", body)


if __name__ == "__main__":
    main()
