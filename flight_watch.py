"""
FlightWatch - watches Google Flights for STL -> EWR (2 adults, max 1 stop) and reports to Telegram.

Each run (every 15 min via Task Scheduler):
  1. Searches every outbound option (Tue any time / Wed morning) as a round trip and as
     two one-ways, for the home country plus one rotating other country (and optional VPN configs).
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
import time
from datetime import date, datetime
from pathlib import Path

import requests
from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.parser import parse
from primp import Client

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


def pause():
    time.sleep(random.uniform(2, 5))


# --------------------------------------------------------------------------- search

def fetch(query, country):
    """Fetch and parse a Google Flights results page. country='' lets Google use the IP."""
    client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    params = query.params()
    if country:
        params["gl"] = country
    resp = client.get(GOOGLE_URL, params=params)
    page = resp.text
    try:
        flights = [f for f in parse(page) if f.price and f.price > 0]
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

    def add(label, day, kind, where, fare, ground, out, back, url):
        details = f"OUT {describe(out)}" + (f" + BACK {describe(back)}" if back else "")
        if ground:
            details += f" (fare ${fare} + ~${ground} ground to NJ)"
        # `price` is the effective cost used for ranking and alerts.
        results.append(dict(option=label, day=day, kind=kind, via=via, country=where, price=fare + ground,
                            fare=fare, ground=ground, out=summary(out),
                            back=summary(back) if back else None, details=details, url=url))

    for country in countries:
        where = country or "ip"
        # Alternate NYC airports are searched on the first country only, to limit requests.
        dests = [home] + (list(alt_dests) if country == countries[0] else [])
        backs = {}  # return one-ways, shared by both outbound options

        for opt in c["outbound_options"]:
            day = f"{date.fromisoformat(opt['date']):%a %b} {date.fromisoformat(opt['date']).day}"
            # Round trip. Google may price this as two separate tickets (out + back),
            # which is fine: there is no connection between them to miss.
            for dest in dests:
                label = opt["label"] if dest == home else f"{opt['label']} → {dest}"
                q = query([leg(opt["date"], c["origin"], dest, opt),
                           leg(c["return_date"], dest, c["origin"])], "round-trip", False)
                try:
                    for f in usable(fetch(q, country), opt)[:3]:
                        add(label, day, "round trip", where, f.price, 2 * extra.get(dest, 0),
                            f, None, q.url())
                except Exception as e:
                    log(f"  round trip {label} [{via}/{where}] failed: {e}")
                pause()

            if not c.get("check_split_one_ways"):
                continue
            # Two one-ways, mixing airports (e.g. into LGA, home from EWR).
            # Self-transfer itineraries (missed-connection risk) are hidden.
            outs = {}
            for dest in dests:
                try:
                    q_out = query([leg(opt["date"], c["origin"], dest, opt)], "one-way", True)
                    found = usable(fetch(q_out, country), opt)
                    if found:
                        outs[dest] = (found[0], q_out.url())
                    pause()
                    if dest not in backs:
                        q_ret = query([leg(c["return_date"], dest, c["origin"])], "one-way", True)
                        found = usable(fetch(q_ret, country), {})
                        backs[dest] = found[0] if found else None
                        pause()
                except Exception as e:
                    log(f"  one-ways {opt['label']} {dest} [{via}/{where}] failed: {e}")
            for od, (o, url) in outs.items():
                for rd, b in backs.items():
                    if not b:
                        continue
                    airports = "" if od == rd == home else f" → {od}, back from {rd}"
                    add(opt["label"] + airports, day, "2 one-ways", where, o.price + b.price,
                        extra.get(od, 0) + extra.get(rd, 0), o, b, url)
    return results


# --------------------------------------------------------------------------- VPN (optional)

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def vpn_searches():
    v = CONFIG["vpn"]
    if not v.get("enabled"):
        return []
    wg = v["wireguard_exe"]
    confs = sorted((HERE / v["configs_dir"]).glob("*.conf"))
    if not Path(wg).exists() or not confs:
        log("  VPN enabled but WireGuard or .conf files not found - skipping VPN pass")
        return []
    if not is_admin():
        log("  VPN enabled but not running as administrator - skipping VPN pass")
        return []

    results = []
    for conf in confs:
        name = conf.stem
        log(f"  VPN: connecting {name}")
        try:
            subprocess.run([wg, "/installtunnelservice", str(conf)], check=True, timeout=60)
            time.sleep(8)
            results += search_all(f"vpn:{name}", [""])
        except Exception as e:
            log(f"  VPN {name} failed: {e}")
        finally:
            subprocess.run([wg, "/uninstalltunnelservice", name], timeout=60)
            time.sleep(3)
    return results


# --------------------------------------------------------------------------- links

def site_links(out_date):
    c = CONFIG
    o, d, r, n = c["origin"], c["destination"], c["return_date"], c["adults"]
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
        "Cheapflights": f"https://www.cheapflights.com/flight-search/{o}-{d}/{out_date}/{r}/{n}adults?sort=price_a",
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

AIRPORT_NAMES = {"EWR": "Newark", "LGA": "LaGuardia", "JFK": "JFK", "STL": "St. Louis"}


def grouped(results):
    """Merge rows that are the same deal except for departure time (e.g. United 7:30 or 9:30)."""
    groups = {}
    for r in sorted(results, key=lambda r: r["price"]):
        b = r["back"] or {}
        key = (r["day"], r["kind"], r["price"], r["out"]["airline"], r["out"]["stops"], r["out"]["to"],
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
    ret = date.fromisoformat(c["return_date"])
    ret_day = f"{ret:%a %b} {ret.day}"

    deals = grouped(results)
    best, others = deals[0], deals[1:1 + c.get("alternatives", 2)]
    o, b = best["out"], best["back"]
    times = " or ".join(best["times"][:2])

    L = []
    if alerts:
        L += [f"\U0001F6A8 <b>{html.escape(' | '.join(alerts))}</b>", ""]
    L += [f"✈️ <b>St. Louis → Newark, NJ</b> · {n} people",
          f"<i>Checked {t12((datetime.now().hour, datetime.now().minute))} · back {ret_day}</i>",
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

    if others:
        L += ["", "<b>Next best</b>"]
        for i, g in enumerate(others, 2):
            go, gb = g["out"], g["back"]
            extra = f"back on {gb['airline']}" if gb else "round trip"
            if g["ground"]:
                extra += ", incl. travel to NJ"
            L.append(f"{i}. <b>{each(g['price'])} each</b> · {g['day'][:3]} {g['times'][0]} → "
                     f"{AIRPORT_NAMES.get(go['to'], go['to'])}")
            L.append(f"      {html.escape(go['airline'])}, {go['stops']} · {html.escape(extra)}")

    L += ["", "<b>Cheapest by day</b>"]
    for opt in c["outbound_options"]:
        mine = [r for r in results if r["option"].startswith(opt["label"])]
        if mine:
            cheapest_day = min(mine, key=lambda r: r["price"])
            L.append(f"{cheapest_day['day']}: {each(cheapest_day['price'])} each")

    best_date = next(o["date"] for o in c["outbound_options"] if best["option"].startswith(o["label"]))
    links = site_links(best_date)
    L += ["", "\U0001F50E Compare: " + " · ".join(
        f'<a href="{html.escape(links[k])}">{k}</a>' for k in ("Kayak", "Skyscanner", "Expedia", "Momondo"))]
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
    results = search_all("direct", countries, alt) + vpn_searches()
    if not results:
        log("No results this run (Google may have rate-limited us). Will retry next run.")
        STATE_FILE.write_text(json.dumps(state, indent=2))
        return
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
