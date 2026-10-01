# Temporary: can Chrome on a GitHub runner load Cheapflights results, and what do they look like?
import json, time
from playwright.sync_api import sync_playwright

url = "https://www.cheapflights.com/flight-search/STL-EWR/2026-10-20/2026-10-24/2adults?sort=price_a&fs=stops=~1"
polls = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--disable-blink-features=AutomationControlled"])
    pg = b.new_page(locale="en-US", timezone_id="America/Chicago")
    pg.on("response", lambda r: polls.append(r) if "flights/poll" in r.url else None)
    pg.goto(url, timeout=60000)
    data = []
    for _ in range(45):
        time.sleep(1)
        data = []
        for r in polls:
            try:
                data.append(r.json())
            except Exception as e:
                data.append({"_err": str(e), "_status": r.status})
        if any(d.get("status") == "complete" for d in data):
            break
    print("title:", pg.title(), "| url:", pg.url, "| polls:", len(polls), [r.status for r in polls])
    last = data[-1] if data else {}
    print("top keys:", {k: type(v).__name__ for k, v in last.items()})
    res = last.get("results") or []
    print("results:", len(res))
    for r in res[:3]:
        print(json.dumps(r)[:2500])
        print("----")
    for k in ("legs", "segments", "airlines", "airports"):
        if k in last:
            v = last[k]
            print(k, json.dumps(v if not isinstance(v, dict) else dict(list(v.items())[:2]))[:1500])
    print("dom prices:", pg.locator("text=/\\$\\d{2,4}/").all_inner_texts()[:15])
    b.close()
