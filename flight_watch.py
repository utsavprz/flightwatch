"""
FlightWatch - watches Google Flights for STL -> EWR (2 adults, max 1 stop) and reports to Telegram.

Each run (every 15 min via Task Scheduler or GitHub Actions):
  1. Searches every outbound option (Tue any time / Wed morning) as a round trip and as
     two one-ways, for the home country plus one rotating other country, and through each WireGuard VPN config.
  2. Sends a silent ranked price list to Telegram, with links to Kayak, Momondo, Skyscanner, etc.
  3. Alerts with sound on a new low / under target; CRITICAL (3 messages + alarm toast)
     under the "great" price or on a sudden big drop.

Run:  python flight_watch.py                  normal run
      python flight_watch.py --test           send a test message and exit
      python flight_watch.py --telegram-setup find your chat id after you message the bot
"""

import base64
import csv
import ctypes
import html
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import requests
from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.exceptions import FlightsNotFound
from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight
from fast_flights.parser import _parse_time
from primp import Client
from selectolax.lexbor import LexborHTMLParser

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
CONFIG = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

# In the cloud (GitHub Actions) the Telegram secrets come from environment variables,
# and there is no Windows desktop to show pop-ups on.
if os.environ.get("TELEGRAM_BOT_TOKEN"):
    CONFIG.setdefault("telegram", {})["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
if os.environ.get("TELEGRAM_CHAT_ID"):
    CONFIG.setdefault("telegram", {})["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
if os.environ.get("GITHUB_ACTIONS"):
    CONFIG["notify"]["windows_toast"] = False
STATE_FILE = HERE / "state.json"
CSV_FILE = HERE / "prices.csv"
LOG_FILE = HERE / "flight_watch.log"
GOOGLE_URL = "https://www.google.com/travel/flights"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------- search

def parse_all(page):
    """Parse BOTH of Google's result lists.

    fast-flights only reads payload[3] ("other flights"); payload[2] ("top flights")
    often holds the cheapest fares, e.g. the United nonstops.
    """
    js = LexborHTMLParser(page).css_first(r"script.ds\:1")
    if js is None:
        raise FlightsNotFound("no results script in page")
    data = js.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found; received error")
    payload = json.loads(data)

    flights = []
    for block in (payload[2], payload[3]):
        if not block or not block[0]:
            continue
        for k in block[0]:
            try:
                f = k[0]
                segs = []
                for s in f[2]:
                    segs.append(SingleFlight(
                        from_airport=Airport(code=s[3], name=s[4]), to_airport=Airport(code=s[6], name=s[5]),
                        departure=SimpleDatetime(date=tuple(s[20]), time=_parse_time(s[8])),
                        arrival=SimpleDatetime(date=tuple(s[21]), time=_parse_time(s[10])),
                        duration=s[11], plane_type=s[17]))
                flights.append(Flights(type=f[0], price=k[1][0][1], airlines=f[1], flights=segs,
                                       carbon=CarbonEmission(typical_on_route=f[22][8], emission=f[22][7])))
            except (IndexError, TypeError):
                continue  # entries without a price or with an unexpected shape
    return flights


def fetch(query, country):
    """Fetch and parse a Google Flights results page. country='' lets Google use the IP."""
    client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    # Pre-accepted cookie consent, so VPN exits in Europe get results instead of the consent page.
    client.set_cookies("https://www.google.com", {"SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"})
    params = query.params()
    if country:
        params["gl"] = country
    resp = client.get(GOOGLE_URL, params=params)
    page = resp.text
    try:
        flights = [f for f in parse_all(page) if f.price and f.price > 0]
    except Exception as e:
        log(f"    google [{country or 'ip'}] HTTP {resp.status_code}, {len(page)} bytes, unparseable: {e}")
        raise
    if not flights:
        log(f"    google [{country or 'ip'}] HTTP {resp.status_code}, {len(page)} bytes, 0 priced flights")
    elif os.environ.get("FW_DEBUG"):
        legs = " / ".join(f"{d.from_airport.airport}-{d.to_airport.airport} {d.date}" for d in query.flight_data)
        log(f"    google [{country or 'ip'}] {legs}: {sorted(f.price for f in flights)[:5]}")
    return flights


def leg(d, frm, to, opt=None):
    opt = opt or {}
    return FlightQuery(
        date=d, from_airport=frm, to_airport=to,
        earliest_departure_hour=opt.get("earliest_hour"),
        latest_departure_hour=opt.get("latest_hour"),
        max_stops=CONFIG.get("max_stops"),
    )


def query(legs, trip, hide_self_transfer):
    return create_query(flights=legs, trip=trip, passengers=Passengers(adults=CONFIG["adults"]),
                        currency="USD", language="en",
                        hide_separate_and_self_transfer=hide_self_transfer)


def usable(flights, opt):
    """Departure-hour window and stop limit, re-checked locally; cheapest first."""
    lo, hi = opt.get("earliest_hour", 0), opt.get("latest_hour", 23)
    max_segs = CONFIG.get("max_stops", 9) + 1
    ok = [f for f in flights
          if lo <= f.flights[0].departure.time[0] <= hi and len(f.flights) <= max_segs]
    return sorted(ok, key=lambda f: f.price)


def short_day(iso):
    d = date.fromisoformat(iso)
    return f"{d:%a %b} {d.day}"


def t12(hm):
    h, m = hm
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def summary(flight):
    """Plain-dict summary of one direction (JSON-safe, used for messages and state)."""
    segs = flight.flights
    return dict(airline=" / ".join(flight.airlines), frm=segs[0].from_airport.code,
                to=segs[-1].to_airport.code, dep=t12(segs[0].departure.time),
                arr=t12(segs[-1].arrival.time),
                stops="nonstop" if len(segs) == 1 else f"1 stop in {segs[0].to_airport.code}"
                if len(segs) == 2 else f"{len(segs) - 1} stops")


def describe(flight):
    s = summary(flight)
    return f"{s['airline']} {s['frm']} {s['dep']} -> {s['to']} {s['arr']} ({s['stops']})"


def search_all(via, countries, alt_dests=()):
    """Run every search once. `via` labels the network path (direct / VPN config name)."""
    c = CONFIG
    home = c["destination"]
    extra = c.get("airport_extra_cost", {})  # ground cost to get between that airport and NJ, per direction
    results = []

    def add(label, day, ret, kind, where, fare, ground, out, back, url):
        details = f"OUT {describe(out)}" + (f" + BACK {describe(back)}" if back else "") + f" | back {ret}"
        if ground:
            details += f" (fare ${fare} + ~${ground} ground to NJ)"
        # `price` is the effective cost used for ranking and alerts.
        results.append(dict(option=label, day=day, ret=ret, ret_day=short_day(ret), kind=kind, via=via,
                            country=where, price=fare + ground, fare=fare, ground=ground,
                            out=summary(out), back=summary(back) if back else None,
                            details=details, url=url))

    # 1. Build every search up front.
    #    The first country gets the full search (all airports, round trips and one-ways);
    #    the other (rotating) country only re-checks Newark round trips, because country
    #    settings have never changed a price on this domestic route.
    jobs = {}  # key -> (query, departure-hour window)
    for country in countries:
        full = country == countries[0]
        dests = [home] + (list(alt_dests) if full else [])
        for i, opt in enumerate(c["outbound_options"]):
            for ret in c["return_dates"]:
                for dest in dests:
                    # Google may price a round trip as two separate tickets (out + back), which
                    # is fine: there is no connection between them to miss.
                    jobs[("rt", country, i, ret, dest)] = (
                        query([leg(opt["date"], c["origin"], dest, opt), leg(ret, dest, c["origin"])],
                              "round-trip", False), opt)
            if full and c.get("check_split_one_ways"):
                # One-ways hide self-transfer itineraries (missed-connection risk).
                for dest in dests:
                    jobs[("out", country, i, dest)] = (
                        query([leg(opt["date"], c["origin"], dest, opt)], "one-way", True), opt)
        if full and c.get("check_split_one_ways"):
            for ret in c["return_dates"]:
                for dest in dests:
                    jobs[("back", country, ret, dest)] = (
                        query([leg(ret, dest, c["origin"])], "one-way", True), {})

    # 2. Run them a few at a time, retrying once on failure.
    def run(key):
        q, opt = jobs[key]
        for attempt in (1, 2):
            try:
                time.sleep(random.uniform(0, 1.5))
                return key, usable(fetch(q, key[1]), opt)
            except Exception as e:
                if attempt == 2:
                    log(f"  search {key} [{via}] failed: {e}")
                time.sleep(3)
        return key, []

    with ThreadPoolExecutor(max_workers=c.get("parallel_searches", 4)) as pool:
        found = dict(pool.map(run, jobs))

    # 3. Turn search results into priced options.
    for key, flights in found.items():
        if key[0] != "rt" or not flights:
            continue
        _, country, i, ret, dest = key
        opt = c["outbound_options"][i]
        label = opt["label"] if dest == home else f"{opt['label']} → {dest}"
        for f in flights[:3]:
            add(label, short_day(opt["date"]), ret, "round trip", country or "ip", f.price,
                2 * extra.get(dest, 0), f, None, jobs[key][0].url())

    # Two one-ways, mixing airports (e.g. into LGA, home from EWR).
    for key, outs in found.items():
        if key[0] != "out" or not outs:
            continue
        _, country, i, od = key
        opt, o = c["outbound_options"][i], outs[0]
        for bkey, backs in found.items():
            if bkey[0] != "back" or bkey[1] != country or not backs:
                continue
            ret, rd, b = bkey[2], bkey[3], backs[0]
            airports = "" if od == rd == home else f" → {od}, back from {rd}"
            add(opt["label"] + airports, short_day(opt["date"]), ret, "2 one-ways", country or "ip",
                o.price + b.price, extra.get(od, 0) + extra.get(rd, 0), o, b, jobs[key][0].url())
    return results


# --------------------------------------------------------------------------- VPN

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def vpn_configs():
    """(name, text) of every WireGuard config: .conf files in configs_dir, plus the VPN_CONFIGS
    environment variable (a GitHub secret). That variable can hold several configs, each
    starting at its [Interface] line, optionally preceded by a "### name" line."""
    confs = [(p.stem, p.read_text()) for p in sorted((HERE / CONFIG["vpn"]["configs_dir"]).glob("*.conf"))]
    name, lines = None, []

    def flush():
        if any(l.strip() == "[Interface]" for l in lines):
            confs.append((name or f"vpn{len(confs) + 1}", "\n".join(lines) + "\n"))

    for line in os.environ.get("VPN_CONFIGS", "").splitlines():
        s = line.strip()
        if s.startswith("###"):
            flush()
            name, lines = s.lstrip("#").strip(), []
        elif s == "[Interface]" and any(l.strip() == "[Interface]" for l in lines):
            flush()
            name, lines = None, [line]
        else:
            lines.append(line)
    flush()
    return confs


def sh(*cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(p.stderr or p.stdout).strip()[-300:]}")


@contextmanager
def windows_tunnel(i, name, text):
    wg = CONFIG["vpn"]["wireguard_exe"]
    if not Path(wg).exists():
        raise RuntimeError("WireGuard not found")
    if not is_admin():
        raise RuntimeError("not running as administrator")
    conf = Path(tempfile.gettempdir()) / f"{name}.conf"
    conf.write_text(text)
    try:
        sh(wg, "/installtunnelservice", str(conf))
        time.sleep(8)
        yield
    finally:
        subprocess.run([wg, "/uninstalltunnelservice", name], timeout=60)
        time.sleep(3)


@contextmanager
def linux_tunnel(i, name, text):
    """wg-quick on Linux (GitHub Actions runner). The runner keeps its own DNS resolver, routed
    outside the tunnel, so the config needs no resolvconf; web traffic goes through the VPN."""
    sudo = [] if os.geteuid() == 0 else ["sudo"]
    no_v6 = Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").read_text().strip() == "1"
    out = []
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key.strip().lower() == "dns":
            continue
        if no_v6 and key.strip().lower() in ("address", "allowedips"):
            line = f"{key.strip()} = " + ", ".join(a.strip() for a in val.split(",") if ":" not in a)
        out.append(line)
    conf = Path(tempfile.gettempdir()) / f"fwvpn{i}.conf"
    conf.write_text("\n".join(out) + "\n")
    conf.chmod(0o600)

    resolvers = []
    for f in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        if Path(f).exists():
            resolvers += [l.split()[1] for l in Path(f).read_text().splitlines()
                          if l.startswith("nameserver") and not l.split()[1].startswith("127.")]
    resolvers = sorted(set(r for r in resolvers if ":" not in r))
    for r in resolvers:
        subprocess.run(sudo + ["ip", "rule", "add", "to", r, "lookup", "main", "pref", "100"], timeout=10)
    try:
        sh(*sudo, "wg-quick", "up", str(conf))
        try:
            time.sleep(2)
            yield
        finally:
            subprocess.run(sudo + ["wg-quick", "down", str(conf)], capture_output=True, timeout=60)
    finally:
        for r in resolvers:
            subprocess.run(sudo + ["ip", "rule", "del", "to", r, "lookup", "main", "pref", "100"], timeout=10)
        conf.unlink(missing_ok=True)


def exit_location():
    """Public IP and country the outside world sees, to confirm the tunnel is up."""
    trace = dict(l.split("=", 1) for l in
                 requests.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=15).text.splitlines() if "=" in l)
    return f"{trace.get('ip')} ({trace.get('loc')})"


def vpn_searches():
    if not CONFIG["vpn"].get("enabled"):
        return []
    confs = vpn_configs()
    if not confs:
        log("  VPN enabled but no WireGuard configs found - skipping VPN pass")
        return []
    tunnel = windows_tunnel if os.name == "nt" else linux_tunnel
    results = []
    for i, (name, text) in enumerate(confs):
        log(f"  VPN: connecting {name}")
        try:
            with tunnel(i, name, text):
                log(f"  VPN {name}: exit {exit_location()}")
                found = search_all(f"vpn:{name}", [""])
                log(f"  VPN {name}: {len(found)} prices")
                results += found
        except Exception as e:
            log(f"  VPN {name} failed: {e}")
    return results


# --------------------------------------------------------------------------- links

def site_links(out_date, r):
    c = CONFIG
    o, d, n = c["origin"], c["destination"], c["adults"]
    ymd = lambda s: s[2:].replace("-", "")
    mdy = lambda s: f"{s[5:7]}/{s[8:10]}/{s[:4]}"
    days = (date.fromisoformat(r) - date.fromisoformat(out_date)).days
    ita = {"type": "round-trip",
           "slices": [{"origin": [o], "dest": [d], "dates": {
               "searchDateType": "calendar", "departureDate": out_date, "departureDateType": "depart",
               "departureDateModifier": "0", "departureDatePreferredTimes": [], "duration": str(days),
               "returnDateType": "depart", "returnDateModifier": "0", "returnDatePreferredTimes": []}}],
           "options": {"cabin": "COACH", "stops": "-1", "extraStops": "1",
                       "allowAirportChanges": "true", "showOnlyAvailable": "true"},
           "pax": {"adults": str(n)}}
    ita_q = base64.b64encode(json.dumps(ita, separators=(",", ":")).encode()).decode()
    return {
        "Google": f"https://www.google.com/travel/flights?q=Flights%20from%20{o}%20to%20{d}%20on%20{out_date}"
                  f"%20through%20{r}%20for%20{n}%20adults&curr=USD",
        "United": f"https://www.united.com/en/us/fsr/choose-flights?f={o}&t={d}&d={out_date}&r={r}&px={n}&taxng=1",
        "Priceline": f"https://www.priceline.com/m/fly/search/{o}-{d}-{out_date.replace('-', '')}/"
                     f"{d}-{o}-{r.replace('-', '')}/?num-adults={n}",
        "Trip.com": f"https://www.trip.com/flights/showfarefirst?dcity={o.lower()}&acity={d.lower()}"
                    f"&ddate={out_date}&rdate={r}&triptype=rt&class=y&quantity={n}",
        "Kayak": f"https://www.kayak.com/flights/{o}-{d}/{out_date}/{r}/{n}adults?sort=price_a&fs=stops=~1",
        "Momondo": f"https://www.momondo.com/flight-search/{o}-{d}/{out_date}/{r}/{n}adults?sort=price_a",
        "Cheapflights": f"https://www.cheapflights.com/flight-search/{o}-{d}/{out_date}/{r}/{n}adults?sort=price_a&fs=stops=~1",
        "Skyscanner": f"https://www.skyscanner.com/transport/flights/{o.lower()}/{d.lower()}/"
                      f"{ymd(out_date)}/{ymd(r)}/?adultsv2={n}&rtn=1",
        "Expedia": f"https://www.expedia.com/Flights-Search?trip=roundtrip&leg1=from:{o},to:{d},"
                   f"departure:{mdy(out_date)}TANYT&leg2=from:{d},to:{o},departure:{mdy(r)}TANYT"
                   f"&passengers=adults:{n}&mode=search",
        "ITA Matrix": f"https://matrix.itasoftware.com/calendar?search={ita_q}",
    }


# --------------------------------------------------------------------------- notify

def telegram(text, silent=False):
    t = CONFIG.get("telegram", {})
    if t.get("bot_token") and not t.get("chat_id"):
        telegram_setup(quiet=True)  # picks up the chat once you've messaged the bot
    if not (t.get("bot_token") and t.get("chat_id")):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", timeout=20,
                          json={"chat_id": t["chat_id"], "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True, "disable_notification": silent})
        if not r.ok:
            log(f"  telegram failed: {r.status_code} {r.text[:200]}")
        return r.ok
    except Exception as e:
        log(f"  telegram failed: {e}")
        return False


def toast(title, body, url=None, critical=False):
    if not CONFIG["notify"].get("windows_toast"):
        return
    try:
        from win11toast import toast as show
        if critical:
            # Alarm scenario: stays on screen with a looping sound until dismissed.
            show(title, body, on_click=url or "", scenario="alarm",
                 audio={"src": "ms-winsoundevent:Notification.Looping.Alarm", "loop": "true"})
        else:
            show(title, body, on_click=url or "", duration="long")
    except Exception as e:
        log(f"  toast failed: {e}")


def telegram_setup(quiet=False):
    token = CONFIG.get("telegram", {}).get("bot_token")
    if not token:
        print("Put your bot token in config.json -> telegram.bot_token first.")
        return
    try:
        updates = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    except Exception as e:
        log(f"  telegram setup failed: {e}")
        return
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in updates.get("result", []) if "message" in u}
    if not chats:
        if not quiet:
            print("No messages found. Open your bot in Telegram, press Start / send 'hi', then run this again.")
        return
    chat_id = list(chats)[-1]
    CONFIG["telegram"]["chat_id"] = chat_id
    CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
    log(f"  Telegram connected (chat {chats[chat_id]})")
    telegram("✅ FlightWatch is connected. You'll get a ranked price update every 15 minutes.")


# --------------------------------------------------------------------------- report

AIRPORT_NAMES = {"EWR": "Newark", "LGA": "LaGuardia", "JFK": "JFK", "PHL": "Philadelphia",
                 "TTN": "Trenton", "STL": "St. Louis"}


def grouped(results):
    """Merge rows that are the same deal except for departure time (e.g. United 7:30 or 9:30)."""
    groups = {}
    for r in sorted(results, key=lambda r: r["price"]):
        b = r["back"] or {}
        key = (r["day"], r["ret"], r["kind"], r["price"], r["out"]["airline"], r["out"]["stops"], r["out"]["to"],
               b.get("airline"), b.get("frm"))
        if key not in groups:
            groups[key] = dict(r, times=[])
        if r["out"]["dep"] not in groups[key]["times"]:
            groups[key]["times"].append(r["out"]["dep"])
    return sorted(groups.values(), key=lambda g: g["price"])


def build_message(results, alerts, state, prev_best):
    c = CONFIG
    n = c["adults"]
    each = lambda total: f"${total / n:.0f}"
    name = lambda code: f"{AIRPORT_NAMES.get(code, code)} ({code})" if code in AIRPORT_NAMES else code

    deals = grouped(results)
    best = deals[0]
    o, b = best["out"], best["back"]
    times = " or ".join(best["times"][:2])
    ret_day = best["ret_day"]

    L = []
    if alerts:
        L += [f"\U0001F6A8 <b>{html.escape(' | '.join(alerts))}</b>", ""]
    L += [f"✈️ <b>St. Louis → Newark / LaGuardia</b> · {n} people",
          f"<i>Checked {t12((datetime.now().hour, datetime.now().minute))}</i>",
          "",
          f"\U0001F4B0 <b>BEST: {each(best['price'])} each</b>  (${best['price']} total)",
          f"\U0001F6EB {best['day']} · <b>{times}</b> → {name(o['to'])}",
          f"      {html.escape(o['airline'])}, {o['stops']}"]
    if b:
        L += [f"\U0001F6EC {ret_day} · <b>{b['dep']}</b> from {name(b['frm'])}",
              f"      {html.escape(b['airline'])}, {b['stops']}",
              "      <i>2 separate one-way tickets</i>"]
    else:
        L += [f"\U0001F6EC {ret_day} · return included, pick the time on Google"]
    if best["ground"]:
        L.append(f"      <i>includes ~{each(best['ground'])} each to get to NJ</i>")
    L.append(f"\U0001F449 <a href=\"{html.escape(best['url'])}\">Open this flight</a>")

    L.append("")
    goal = c["target_total_price"]
    if best["price"] <= goal:
        L.append(f"✅ <b>Under your {each(goal)} goal - book it!</b>")
    else:
        L.append(f"\U0001F3AF Goal {each(goal)} each · {each(best['price'] - goal)} to go")
    if prev_best is not None:
        diff = best["price"] - prev_best
        L.append("➖ Same as last check" if diff == 0 else
                 f"\U0001F4C9 Down {each(-diff)} each since last check" if diff < 0 else
                 f"\U0001F4C8 Up {each(diff)} each since last check")

    # Separate round-trip rankings for the main airports (same airport both ways, ticket price only).
    for code in c.get("ranked_airports", ["EWR", "LGA"]):
        rows, seen = [], set()
        for g in deals:
            gb = g["back"]
            if g["out"]["to"] != code or (gb and gb["frm"] != code):
                continue
            # The same flights booked as a round trip or as two one-ways count once.
            key = (g["fare"], g["out"]["airline"], g["ret"], gb["airline"] if gb else g["out"]["airline"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(g)
        rows.sort(key=lambda g: g["fare"])
        if not rows:
            continue
        note = f" <i>(+~{each(2 * c['airport_extra_cost'][code])} each to NJ)</i>" \
            if code in c.get("airport_extra_cost", {}) else ""
        L += ["", f"\U0001F3C6 <b>{AIRPORT_NAMES.get(code, code)} ({code}) round trips</b>{note}"]
        for i, g in enumerate(rows[: c.get("ranking_per_airport", 3)], 1):
            go, gb = g["out"], g["back"]
            line2 = f"{html.escape(go['airline'])}, {go['stops']}"
            if gb:
                line2 += (" · 2 one-way tickets" if gb["airline"] == go["airline"]
                          else f" · back on {html.escape(gb['airline'])}, {gb['stops']}")
            L.append(f"{i}. <b>{each(g['fare'])} each</b> · {g['day'][:3]} {' or '.join(g['times'][:2])}"
                     f" · back {g['ret_day'][:3]}")
            L.append(f"      {line2}")

    L += ["", "<b>Cheapest by dates</b> (per person)"]
    for opt in c["outbound_options"]:
        cells = []
        for ret in c["return_dates"]:
            mine = [r for r in results if r["option"].startswith(opt["label"]) and r["ret"] == ret]
            if mine:
                p = min(r["price"] for r in mine)
                cell = f"back {short_day(ret)[:3]} {each(p)}"
                cells.append(f"<b>{cell}</b>" if p == best["price"] else cell)
        if cells:
            L.append(f"{short_day(opt['date'])[:3]} out: " + " · ".join(cells))

    # Cheapest whole trip per airline (same airline both ways, any airport/dates).
    by_airline = {}  # airline -> (ticket price, airport)
    for r in results:
        a, to = r["out"]["airline"], r["out"]["to"]
        if "/" in a or (r["back"] and (r["back"]["airline"] != a or r["back"]["frm"] != to)):
            continue
        if a not in by_airline or r["fare"] < by_airline[a][0]:
            by_airline[a] = (r["fare"], to)
    if by_airline:
        L += ["", "<b>By airline</b> (per person, round trip)"]
        L.append(" · ".join(f"{a} {each(p)} {to}"
                                 for a, (p, to) in sorted(by_airline.items(), key=lambda x: x[1][0])))

    best_date = next(o["date"] for o in c["outbound_options"] if best["option"].startswith(o["label"]))
    links = site_links(best_date, best["ret"])
    sw = (f"https://www.southwest.com/air/booking/select-depart.html?adultPassengersCount={n}"
          f"&departureDate={best_date}&destinationAirportCode=LGA&originationAirportCode={c['origin']}"
          f"&returnDate={best['ret']}&tripType=roundtrip&fareType=USD&passengerType=ADULT")
    shown = {"Southwest": sw, **{k: links[k] for k in ("United", "Cheapflights", "Kayak", "Expedia", "Skyscanner")}}
    L += ["", "\U0001F50E Check: " + " · ".join(
        f'<a href="{html.escape(u)}">{k}</a>' for k, u in shown.items())]
    return "\n".join(L)


def save_csv(rows):
    new = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["time", "option", "kind", "via", "country", "price", "details", "url"],
                           extrasaction="ignore")
        if new:
            w.writeheader()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for r in rows:
            w.writerow({"time": stamp, **r})


def main():
    if "--telegram-setup" in sys.argv:
        telegram_setup()
        return
    if "--test" in sys.argv:
        ok = telegram("✅ FlightWatch test message")
        toast("FlightWatch test", "Notifications are working.")
        print("Telegram:", "sent" if ok else "not configured / failed")
        return

    today = date.today()
    if today > date.fromisoformat(CONFIG["stop_after"]):
        log("Trip window has passed - nothing to do. You can delete the scheduled task.")
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    # Home country every run + one other country, rotating, to keep request volume low.
    home, *others = CONFIG["google_countries"]
    run_no = state.get("run_no", 0)
    countries = [home] + ([others[run_no % len(others)]] if others else [])
    state["run_no"] = run_no + 1
    log(f"Run started (countries: {', '.join(countries)})")

    alt = CONFIG.get("alt_destinations", []) if run_no % CONFIG.get("alt_every_n_runs", 4) == 0 else []
    vpn = vpn_searches()
    if CONFIG["vpn"].get("enabled"):
        state["vpn_fail_streak"] = 0 if vpn else state.get("vpn_fail_streak", 0) + 1
        if state["vpn_fail_streak"] == 3:  # warn once, not every run
            telegram("⚠️ <b>FlightWatch:</b> the VPN checks found nothing for the last 3 runs "
                     "(VPN down or not set up). Direct checks continue.", silent=True)
    results = search_all("direct", countries, alt) + vpn
    if not results:
        log("No results this run (Google may have rate-limited us). Will retry next run.")
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        if state["fail_streak"] == 3:  # warn once, not every run
            telegram("⚠️ <b>FlightWatch problem:</b> the last 3 checks found no flights "
                     "(Google may be blocking it). I'll keep trying; check prices manually meanwhile.")
        STATE_FILE.write_text(json.dumps(state, indent=2))
        return
    if state.get("fail_streak", 0) >= 3:
        telegram("✅ FlightWatch is working again.", silent=True)
    state["fail_streak"] = 0
    save_csv(results)

    best = min(results, key=lambda r: r["price"])
    pp = best["price"] / CONFIG["adults"]
    log(f"Best: ${best['price']} total (${pp:.0f}/person) - {best['option']} - {best['kind']} - {best['details']}")

    low = state.get("lowest")
    prev_best = state.get("last_best", {}).get("price")
    alerts, critical = [], False
    if low is not None and best["price"] <= low - CONFIG["min_drop_to_alert"]:
        alerts.append(f"NEW LOW (was ${low})")
    if prev_best is not None and prev_best - best["price"] >= CONFIG["crazy_drop"]:
        alerts.append(f"PRICE CRASHED ${prev_best - best['price']} in one check")
        critical = True
    if best["price"] <= CONFIG["target_total_price"] and state.get("target_alerted_at") != best["price"]:
        if best["price"] <= CONFIG["great_total_price"]:
            alerts.append(f"CRAZY DEAL ${pp:.0f}/person - BOOK NOW")
            critical = True
        else:
            alerts.append(f"${pp:.0f}/person - under your $%d target, worth booking"
                          % (CONFIG["target_total_price"] / CONFIG["adults"]))
        state["target_alerted_at"] = best["price"]

    state["lowest"] = best["price"] if low is None else min(low, best["price"])
    msg = build_message(results, alerts, state, prev_best)

    if critical:
        log(f"  CRITICAL: {' | '.join(alerts)}")
        telegram(msg)
        for _ in range(CONFIG.get("critical_repeats", 3) - 1):
            time.sleep(20)
            telegram(f"\U0001F6A8\U0001F6A8 <b>CRITICAL: ${best['price']} for 2 (${pp:.0f}/person)</b> "
                     f"— {html.escape(best['option'])}. Book now: "
                     f"<a href=\"{html.escape(best['url'])}\">open flight</a>")
    elif alerts:
        log(f"  ALERT: {' | '.join(alerts)}")
        telegram(msg)
    elif CONFIG.get("telegram_every_run", True):
        telegram(msg, silent=True)

    state["last_best"] = best
    state["last_run"] = datetime.now().isoformat(timespec="minutes")
    STATE_FILE.write_text(json.dumps(state, indent=2))
    log("Run finished")

    # Last, because the toast blocks until it's dismissed or times out.
    if alerts:
        toast(f"${best['price']} for 2 (${pp:.0f}/person)", " | ".join(alerts) + "\n" + best["details"],
              best["url"], critical=critical)


if __name__ == "__main__":
    main()
