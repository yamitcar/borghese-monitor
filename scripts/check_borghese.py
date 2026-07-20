#!/usr/bin/env python3
"""
Galleria Borghese ticket availability monitor.

Checks two sources for tickets on TARGET_DATE with afternoon slots for PARTY_SIZE:
  1. Official: tosc.it (TicketOne Sistemi Culturali / Eventim network)
  2. Backup:   GetYourGuide reseller product

Prints "AVAILABLE" and writes availability_details.txt when something is found,
otherwise prints "NOT_AVAILABLE". Always exits 0 (the workflow decides based on
stdout, and a scraping failure must not look like availability).

Calibrated to OVER-alert: if the target date is bookable at all but the exact
afternoon slot can't be confirmed, it alerts anyway.

--------------------------------------------------------------------------------
SELECTOR NOTES (discovered by driving the real sites with Playwright, 2026-07-20)
--------------------------------------------------------------------------------

tosc.it — two layers:

  (a) Eventim public search API (NOT blocked by Akamai, works from plain HTTPS):
        GET https://public-api.eventim.com/websearch/search/api/exploration/v1/
            products?webId=web__ticketone-it&language=en&product_group_id=2253937
      Returns one "product" per museum day:
        product["typeAttributes"]["liveEntertainment"]["startDate"]  -> "2026-07-24T00:00:00+02:00"
        product["productId"]  -> 21872491 (day event page id)
        product["status"]     -> "Available" for every released day (not granular)
      A day being PRESENT in this list == the date has been released for sale.
      CAVEAT (first CI run, 2026-07-20): the API answers plain curl from
      residential IPs but returns 403 Forbidden to GitHub Actions runners,
      so the monitor tries in order:
        1. urllib with browser-ish headers,
        2. the same URL opened inside headless Chromium (page.goto),
        3. the artist-page DOM (see below).

  (b) tosc.it DOM (works in a headed browser; headless usually gets Akamai
      "Access Denied" — kept as best-effort enrichment):
      Day event page /en/event/galleria-borghese-galleria-borghese-<productId>/:
        - one card per time slot:  div[data-qa="price-category"]
        - slot title lives in the form's data-qa:
              form[data-qa="pc-list-number-IN 3 pm-OUT 5 pm"]
          (titles seen: "IN 9 am-OUT 11 am" ... "IN 5 pm-OUT 7 pm",
           "IN 18:00-OUT 20:00", guided-tour variants — the site mixes locales)
        - SOLD-OUT ticket rows carry:
              .ticket-type-unavailable-sec[data-qa="ticket-type-availability-hint"]
          with text "Not available"
        - AVAILABLE slots instead render a quantity stepper:
              .btn-group.btn-stepper.js-stepper with [data-qa="more-tickets"] /
              hidden inputs input.js-stepper-amount
          => "slot available" == card contains a .js-stepper, not text matching.
      Artist page /en/artist/galleria-borghese/galleria-borghese-2253937/:
        - event list: article[data-qa="event-listing-item"] per day, date in
          time[data-qa="event-date-day"] @datetime (ISO), day event id on the
          date box div[data-qa="event-date"] @data-event-id, availability pill
          div[data-qa^="pill-available"] ("Available"/"Limited"/"Few" =
          availability-indicator-{green,yellow,red}). NOTE: the list paginates
          (20 of 36 rows server-rendered), so absence from the list is NOT
          absence from sale — the month calendar is the authority:
        - calendar: [data-qa="calendar-component"], current month label
          #calendar-month ("July 2026"), nav buttons
          [data-qa="calendar-go-next"] / [data-qa="calendar-go-previous"]
          (disabled attr at the range edges), day cells .cal-month-day
          (.cal-day-inmonth for days of the shown month, "with-event" class on
          days that have an event), day number in .day-number, and per-day
          status either on .day-number or on .event-time-pill chips via
          cal-event-status-available / cal-event-status-unavailable.

GetYourGuide — headless-friendly (with a non-headless user agent):

  - URL params preselect everything: ?_pc=1,4&date_from=2026-07-21
    (_pc=1,N => N adults). CAVEAT found in testing: if date_from is beyond
    GYG's current booking horizon the whole page 404s ("Lost your way?"),
    so the monitor loads the page with _pc only and drives the date picker
    instead of trusting date_from with the target date.
  - cookie banner: usercentrics dialog, accept via button text "Let's go" /
    "Accept all" (banner overlays the booking widget, must be dismissed)
  - "Check availability" button: button.js-check-availability — NOTE: when
    date_from points at a not-yet-bookable date GYG doesn't render this button
    at all, and when the date IS valid the options often auto-load without a
    click; both cases are handled
  - date picker opener chip (label changes once a date is preselected, so
    match by class, not label): button.gtm-trigger__adp-date-picker-interaction
  - month navigation: button.c-datepicker-month__arrow (prev/next pair; next
    carries an .i-arrow-right icon)
  - date picker days: .c-datepicker-day__container with
        aria-label="Friday, September 25, 2026" and aria-disabled="true|false";
    unavailable days also carry class c-datepicker-day--disabled
  - after checking, per-option cards render start-time chips:
        .starting-times__layout  (section)
        .starting-time-chip-wrapper button.c-chip  (one per start time,
         label like "2:00 PM" inside .c-chip__label)
  - scarcity badge: .badge-label with text "Only 7 spots left"
--------------------------------------------------------------------------------
"""

import datetime
import json
import re
import sys
import traceback
import urllib.request

# ----------------------------- CONFIG (editable) -----------------------------
TARGET_DATE = "2026-09-25"                  # YYYY-MM-DD
TARGET_AFTERNOON_SLOTS = ["15:00", "17:00"]  # entry times we actually want
PARTY_SIZE = 4

TOSC_PRODUCT_GROUP_ID = "2253937"           # "Borghese Gallery Museum" on tosc.it
TOSC_ARTIST_URL = "https://www.tosc.it/en/artist/galleria-borghese/galleria-borghese-2253937/"
EVENTIM_API = (
    "https://public-api.eventim.com/websearch/search/api/exploration/v1/products"
    "?webId=web__ticketone-it&language=en&product_group_id={pg}&page_size=100"
)
GYG_URL = "https://www.getyourguide.com/rome-l33/borghese-gallery-entry-ticket-and-audioguide-app-t468068/"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
DETAILS_FILE = "availability_details.txt"
# -----------------------------------------------------------------------------


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def parse_slot_time(text):
    """Normalize slot labels from either site to 'HH:MM' 24h.

    Handles tosc.it titles ('IN 3 pm-OUT 5 pm', 'IN 18:00-OUT 20:00') and
    GYG chips ('2:00 PM'). Returns None if no time found.
    """
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text.strip(), re.I)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def is_afternoon(hhmm):
    return hhmm is not None and hhmm >= "14:00"


# ----------------------------- source 1: tosc.it -----------------------------

def _parse_api_days(data):
    days = {}
    for prod in data.get("products", []):
        start = (prod.get("typeAttributes", {}).get("liveEntertainment", {}) or {}).get("startDate", "")
        if start[:10]:
            days[start[:10]] = {
                "productId": prod.get("productId"),
                "status": prod.get("status"),
            }
    return days


def tosc_api_released_days():
    """Days currently on sale according to the Eventim public API (urllib)."""
    url = EVENTIM_API.format(pg=TOSC_PRODUCT_GROUP_ID)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.tosc.it",
        "Referer": "https://www.tosc.it/",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _parse_api_days(json.load(resp))


def tosc_api_days_via_browser(pw):
    """Same API but fetched from inside headless Chromium (real browser TLS).

    The API 403s plain HTTP clients on datacenter IPs (seen from GitHub
    Actions); going through the browser sometimes passes that filter.
    """
    url = EVENTIM_API.format(pg=TOSC_PRODUCT_GROUP_ID)
    browser = pw.chromium.launch(headless=True, channel="chromium")
    try:
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = ctx.new_page()
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        body = page.evaluate("document.body.innerText")
        return _parse_api_days(json.loads(body))
    finally:
        browser.close()


def tosc_artist_dom_days(pw):
    """Last-resort official source: scrape the artist page itself.

    Returns (days, target_released) where days comes from the server-rendered
    event list (paginated, so possibly partial) and target_released is the
    verdict of walking the month calendar to TARGET_DATE (None if the
    calendar could not answer).
    """
    target_dt = datetime.date.fromisoformat(TARGET_DATE)
    browser = pw.chromium.launch(headless=True, channel="chromium")
    try:
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                  viewport={"width": 1440, "height": 1200})
        page = ctx.new_page()
        for attempt in (1, 2):  # Akamai lets headless through intermittently
            page.goto(TOSC_ARTIST_URL, timeout=45000, wait_until="domcontentloaded")
            title = page.title() or ""
            if "access denied" in title.lower():
                raise RuntimeError("tosc.it artist page served 'Access Denied' (Akamai)")
            try:
                page.wait_for_selector(
                    "[data-qa='calendar-component'], article[data-qa='event-listing-item']",
                    timeout=20000, state="attached")
                break
            except Exception:
                log(f"tosc.it artist DOM attempt {attempt}: no calendar/list rendered "
                    f"(title={title!r}, body={len(page.content())} bytes)")
                if attempt == 2:
                    raise RuntimeError("artist page loaded but calendar/list never rendered "
                                       "(soft bot-block or markup change)")
        try:  # cookie banner ("Accept All Cookies") can overlay the calendar nav
            page.locator("button:has-text('Accept All Cookies')").first.click(timeout=3000)
        except Exception:
            pass

        days = {}
        for art in page.locator("article[data-qa='event-listing-item']").all():
            t = art.locator("time[data-qa='event-date-day']").first
            if t.count() == 0:
                continue
            iso = (t.get_attribute("datetime") or "")[:10]
            if not iso:
                continue
            box = art.locator("[data-qa='event-date']").first
            pid = box.get_attribute("data-event-id") if box.count() else None
            pill = art.locator("[data-qa^='pill-available']").first
            status = pill.inner_text().strip() if pill.count() else "listed"
            days[iso] = {"productId": pid, "status": status}
        log(f"tosc.it artist DOM: {len(days)} days in the (paginated) event list")

        # walk the calendar to the target month
        target_released = None
        month_label = f"{target_dt.strftime('%B')} {target_dt.year}"
        for _ in range(14):
            current = page.locator("#calendar-month").first.inner_text().strip()
            if current == month_label:
                break
            nxt = page.locator("[data-qa='calendar-go-next']").first
            if nxt.count() == 0 or nxt.get_attribute("disabled") is not None:
                log(f"tosc.it calendar: cannot advance past {current!r} — "
                    f"{month_label!r} not published yet")
                return days, False
            nxt.click(timeout=4000)
            page.wait_for_timeout(700)
        else:
            return days, None
        # found the month: inspect the day cell
        for cell in page.locator(".cal-month-day.cal-day-inmonth").all():
            num = cell.locator(".day-number").first
            if num.count() and num.inner_text().strip() == str(target_dt.day):
                classes = cell.get_attribute("class") or ""
                has_event = "with-event" in classes or cell.locator(".event-time-pill").count() > 0
                available = cell.locator(".cal-event-status-available").count() > 0
                log(f"tosc.it calendar {TARGET_DATE}: with_event={has_event} "
                    f"available_marker={available}")
                # over-alert: any event on the day counts as released
                target_released = has_event
                break
        return days, target_released
    finally:
        browser.close()


def get_tosc_days(pw):
    """Layered official-source lookup. Returns (days, how, target_released_hint)."""
    try:
        return tosc_api_released_days(), "api", None
    except Exception as e:
        log(f"tosc.it API via urllib failed ({e}); trying through the browser")
    try:
        return tosc_api_days_via_browser(pw), "api-browser", None
    except Exception as e:
        log(f"tosc.it API via browser failed ({str(e)[:150]}); trying artist page DOM")
    days, target_released = tosc_artist_dom_days(pw)
    return days, "artist-dom", target_released


def tosc_dom_slots(pw, product_id):
    """Best-effort: read per-slot availability from the day event page.

    Akamai usually serves 'Access Denied' to headless browsers, so this is
    expected to fail in CI — the API result above is the load-bearing signal.
    """
    url = f"https://www.tosc.it/en/event/galleria-borghese-galleria-borghese-{product_id}/"
    browser = pw.chromium.launch(headless=True, channel="chromium")
    try:
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                  viewport={"width": 1440, "height": 1200})
        page = ctx.new_page()
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        if "access denied" in (page.title() or "").lower():
            raise RuntimeError("tosc.it served 'Access Denied' to the headless browser (Akamai)")
        # slot cards render server-side; wait for them (they can also arrive late via JS)
        page.wait_for_selector("[data-qa='price-category']", timeout=20000)
        slots = []
        for card in page.locator("[data-qa='price-category']").all():
            form = card.locator("form[data-qa^='pc-list-number-']")
            if form.count() == 0:
                continue
            title = form.first.get_attribute("data-qa").removeprefix("pc-list-number-")
            available = card.locator(".js-stepper").count() > 0
            slots.append({"title": title, "time": parse_slot_time(title), "available": available})
        return slots
    finally:
        browser.close()


def check_tosc(pw):
    result = {"source": "tosc.it (oficial)", "link": TOSC_ARTIST_URL,
              "date_released": False, "slots": None, "notes": []}
    days, how, target_hint = get_tosc_days(pw)
    if days:
        first, last = min(days), max(days)
        log(f"tosc.it [{how}]: {len(days)} days on sale ({first} .. {last})")
    elif target_hint is None:
        log(f"tosc.it [{how}]: returned no days — treating as source error")
        raise RuntimeError("official source returned an empty day list")

    if target_hint:
        # calendar fallback says the day exists even though the (paginated)
        # list didn't include it
        result["date_released"] = True
        result["notes"].append("Detectado vía calendario de tosc.it (sin detalle de turnos).")
        log(f"tosc.it: TARGET DATE {TARGET_DATE} present in the artist calendar")
        result["days_seen"] = days
        return result

    result["days_seen"] = days
    if TARGET_DATE not in days:
        last = max(days) if days else "ninguno visible"
        result["notes"].append(f"La fecha {TARGET_DATE} aún no aparece en la venta oficial "
                               f"(último día liberado: {last}).")
        # Selector self-test on a date that IS on sale, so we know the slot
        # detection would work the day the target date appears.
        probe = min(days) if days else None
        if probe and days[probe]["productId"]:
            log(f"tosc.it selftest: probing slot selectors on released day {probe} "
                f"(productId {days[probe]['productId']})")
            try:
                slots = tosc_dom_slots(pw, days[probe]["productId"])
                avail = [s for s in slots if s["available"]]
                log(f"tosc.it selftest OK: {len(slots)} slot cards parsed, {len(avail)} available "
                    f"({[s['title'] for s in avail][:5]})")
            except Exception as e:
                log(f"tosc.it selftest: DOM not reachable ({e}) — expected in CI; "
                    f"date-level detection remains functional")
        return result

    result["date_released"] = True
    info = days[TARGET_DATE]
    result["link"] = (f"https://www.tosc.it/en/event/galleria-borghese-galleria-borghese-"
                      f"{info['productId']}/")
    log(f"tosc.it: TARGET DATE {TARGET_DATE} IS ON SALE (productId {info['productId']}, "
        f"status {info['status']})")
    try:
        slots = tosc_dom_slots(pw, info["productId"])
        result["slots"] = slots
        log(f"tosc.it: slot detail parsed: {[(s['title'], s['available']) for s in slots]}")
    except Exception as e:
        result["notes"].append("No se pudo leer el detalle de turnos (bloqueo anti-bot); "
                               "se avisa igualmente por estar la fecha a la venta.")
        log(f"tosc.it: slot detail unavailable: {e}")
    return result


# --------------------------- source 2: GetYourGuide ---------------------------

TIME_RE = re.compile(r"\d{1,2}:\d{2}")


def gyg_collect_slot_chips(page, out, date_iso):
    """Read start-time chips + scarcity badges once options are on screen."""
    # chip wrappers vary between layouts, so scope loosely and filter by a
    # time-looking label ("2:00 PM" — sometimes with a narrow no-break space)
    for chip in page.locator("button.c-chip").all():
        t = chip.inner_text().replace(" ", " ").strip()
        if TIME_RE.search(t):
            out["chips"].append(t)
    out["spots_badges"] = [b.inner_text().strip()
                           for b in page.locator(".badge-label").all()
                           if "spot" in b.inner_text().lower()]
    log(f"GYG: start-time chips for {date_iso}: {out['chips']} "
        f"badges={out['spots_badges']}")


def gyg_check_date(pw, date_iso, party_size):
    """Retry wrapper: GYG intermittently serves challenges/variants to
    datacenter IPs — a fresh page usually recovers."""
    last_err = None
    for attempt in (1, 2):
        try:
            return _gyg_check_date_once(pw, date_iso, party_size)
        except Exception as e:
            last_err = e
            log(f"GYG attempt {attempt} failed: {str(e)[:150]}")
    raise last_err


def _gyg_check_date_once(pw, date_iso, party_size):
    """Drive the GYG product page for a specific date and party size.

    Loads the page WITHOUT date_from (an out-of-horizon date_from 404s the
    whole page), decides via the date picker, and only if the day is enabled
    clicks it to load the start-time options.
    """
    url = f"{GYG_URL}?_pc=1%2C{party_size}"
    browser = pw.chromium.launch(headless=True, channel="chromium")
    try:
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                  viewport={"width": 1440, "height": 1400})
        page = ctx.new_page()
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        # usercentrics cookie dialog overlays the booking widget
        for sel in ["button:has-text(\"Let's go\")", "button:has-text('Accept all')",
                    "#onetrust-accept-btn-handler"]:
            try:
                page.locator(sel).first.click(timeout=3000)
                log(f"GYG: cookie banner dismissed via {sel}")
                break
            except Exception:
                pass
        title = page.title() or ""
        if "error" in title.lower() or "access denied" in title.lower():
            raise RuntimeError(f"GYG served an error page: {title!r}")

        out = {"date_selectable": None, "chips": [], "spots_badges": []}

        # 1) find the target day in the date picker
        picker = page.locator("button.gtm-trigger__adp-date-picker-interaction")
        try:
            picker.first.wait_for(state="attached", timeout=15000)
        except Exception:
            raise RuntimeError(f"GYG booking widget missing (title={page.title()!r}) — "
                               f"challenge page or layout variant")
        picker.first.click(timeout=8000)
        page.wait_for_selector(".c-datepicker-day__container", timeout=10000)
        target_dt = datetime.date.fromisoformat(date_iso)
        # aria-label like "Friday, September 25, 2026"
        label_frag = f"{target_dt.strftime('%B')} {target_dt.day}, {target_dt.year}"
        day = page.locator(f".c-datepicker-day__container[aria-label*='{label_frag}']")
        for _ in range(12):  # flip months until the target is rendered
            if day.count() > 0:
                break
            nxt = page.locator("button.c-datepicker-month__arrow").last
            if nxt.count() == 0:
                break
            nxt.click(timeout=3000)
            page.wait_for_timeout(500)
        if day.count() == 0:
            log(f"GYG: month with {date_iso} never rendered — GYG's booking "
                f"horizon doesn't reach it yet")
            return out
        out["date_selectable"] = day.first.get_attribute("aria-disabled") == "false"
        log(f"GYG: date picker says {date_iso} selectable={out['date_selectable']}")
        if not out["date_selectable"]:
            return out

        # 2) day is bookable: click it and load the start-time options
        day.first.click(timeout=5000)
        page.wait_for_timeout(1500)
        try:
            if page.locator(".starting-times__layout").count() == 0:
                btn = page.locator("button.js-check-availability")
                if btn.count() > 0:
                    btn.first.click(timeout=8000)
            page.wait_for_selector(".starting-times__layout", timeout=25000)
            gyg_collect_slot_chips(page, out, date_iso)
        except Exception as e:
            log(f"GYG: day is selectable but options didn't render ({str(e)[:120]})")
        return out
    finally:
        browser.close()


def check_gyg(pw, selftest_date=None):
    result = {"source": "GetYourGuide (respaldo)",
              "link": f"{GYG_URL}?_pc=1%2C{PARTY_SIZE}&date_from={TARGET_DATE}",
              "date_released": False, "slots": None, "notes": []}
    probe = gyg_check_date(pw, TARGET_DATE, PARTY_SIZE)
    chips = [{"title": c, "time": parse_slot_time(c), "available": True} for c in probe["chips"]]
    if probe["date_selectable"]:
        result["date_released"] = True
        result["slots"] = chips or None
        if not chips:
            result["notes"].append("La fecha aparece seleccionable en GYG pero no se pudieron "
                                   "leer los horarios; se avisa igualmente.")
    elif chips:
        # belt and braces: options rendered even though the picker said no
        result["date_released"] = True
        result["slots"] = chips
    else:
        result["notes"].append(f"GYG aún no ofrece {TARGET_DATE}.")
        if selftest_date:
            log(f"GYG selftest: probing a date that IS on sale officially: {selftest_date}")
            st = gyg_check_date(pw, selftest_date, PARTY_SIZE)
            if st["chips"] or st["date_selectable"]:
                log(f"GYG selftest OK: date_selectable={st['date_selectable']} "
                    f"chips={st['chips']}")
            else:
                log("GYG selftest WARNING: no availability signal for a day that the "
                    "official site sells — GYG selectors may have changed")
    if probe["spots_badges"]:
        result["notes"].append("Aviso de escasez en GYG: " + "; ".join(probe["spots_badges"]))
    return result


# --------------------------------- decision -----------------------------------

def summarize(results):
    lines = []
    alert = False
    for r in results:
        if r is None:
            continue
        lines.append(f"## Fuente: {r['source']}")
        lines.append(f"Link de compra: {r['link']}")
        if not r["date_released"]:
            lines.append(f"- {TARGET_DATE}: sin señal de venta todavía.")
        else:
            alert = True
            lines.append(f"- ¡{TARGET_DATE} está a la venta!")
            if r["slots"] is None:
                lines.append(f"- Turnos: no confirmables automáticamente — revisa a mano "
                             f"(objetivo: {', '.join(TARGET_AFTERNOON_SLOTS)}, "
                             f"{PARTY_SIZE} personas).")
            else:
                afternoon = [s for s in r["slots"]
                             if s.get("available") and is_afternoon(s.get("time"))]
                target_hits = [s for s in afternoon
                               if s.get("time") in TARGET_AFTERNOON_SLOTS]
                lines.append("- Turnos vistos: " +
                             ", ".join(f"{s['title']}{'' if s.get('available') else ' (agotado)'}"
                                       for s in r["slots"]))
                if target_hits:
                    lines.append("- ✅ Turno objetivo disponible: " +
                                 ", ".join(s["title"] for s in target_hits))
                elif afternoon:
                    lines.append("- ⚠️ Hay turnos de tarde (no exactamente 15:00/17:00): " +
                                 ", ".join(s["title"] for s in afternoon))
                else:
                    lines.append("- ⚠️ La fecha está a la venta pero no se vieron turnos de "
                                 "tarde libres; revisa a mano por si acaso.")
        for n in r["notes"]:
            lines.append(f"- {n}")
        lines.append("")
    return alert, "\n".join(lines)


def main():
    log(f"Monitor Galleria Borghese — objetivo {TARGET_DATE} "
        f"{TARGET_AFTERNOON_SLOTS} x{PARTY_SIZE}")
    from playwright.sync_api import sync_playwright

    results = []
    failed_sources = []
    selftest_date = None
    with sync_playwright() as pw:
        try:
            r_tosc = check_tosc(pw)
            results.append(r_tosc)
            days = r_tosc.get("days_seen") or {}
            selftest_date = min(days) if days else None
        except Exception:
            log("tosc.it source failed:\n" + traceback.format_exc())
            results.append(None)
            failed_sources.append("tosc.it (oficial)")
        try:
            results.append(check_gyg(pw, selftest_date=selftest_date))
        except Exception:
            log("GetYourGuide source failed:\n" + traceback.format_exc())
            results.append(None)
            failed_sources.append("GetYourGuide (respaldo)")

    alert, details = summarize(results)
    if failed_sources:
        details += ("\n⚠️ Fuentes que fallaron en esta corrida (revisa los logs): "
                    + ", ".join(failed_sources) + "\n")
    if alert:
        header = (f"Fecha objetivo: {TARGET_DATE} — turnos deseados: "
                  f"{', '.join(TARGET_AFTERNOON_SLOTS)} — {PARTY_SIZE} personas\n"
                  f"Generado: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n\n")
        with open(DETAILS_FILE, "w") as f:
            f.write(header + details)
        log("Details written to " + DETAILS_FILE)
        print("AVAILABLE")
    else:
        print("NOT_AVAILABLE")
    sys.exit(0)


if __name__ == "__main__":
    main()
