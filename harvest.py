#!/usr/bin/env python3
"""FareHarbor availability harvester for Seattle Sailing Club.

Built on the Universal API Harvester pattern (discover once, call many times):
the FareHarbor embed API was mapped in
    ~/python_projects/Universal_API_Harvester/captures/fareharbor-com/
and this script drives the generated client
    ~/python_projects/Universal_API_Harvester/generated/fareharbor_com_client.py

It pulls all items and their monthly availability calendars, saves the raw
data to data/events.json, and generates a self-contained index.html browser
from template.html.

Usage:
    python3 harvest.py            # harvest 6 months ahead
    python3 harvest.py --months 9 # harvest 9 months ahead
    open index.html               # browse the result
"""

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UAH_DIR = Path.home() / "python_projects" / "Universal_API_Harvester"
sys.path.insert(0, str(UAH_DIR))

try:
    from generated.fareharbor_com_client import FareharborComClient, DEFAULT_FLOW
except ImportError:
    # Standalone fallback (e.g. on the deploy server): same interface as the
    # UAH-generated client, stdlib only. Endpoint map documented in
    # Universal_API_Harvester/captures/fareharbor-com/DISCOVERY_NOTES.md
    import urllib.request

    DEFAULT_FLOW = 1498464

    class FareharborComClient:
        BASE_URL = "https://fareharbor.com"

        @classmethod
        def connect(cls, company="seattlesailing"):
            c = cls()
            c.company = company
            return c

        def _get(self, path):
            req = urllib.request.Request(
                self.BASE_URL + path,
                headers={"User-Agent": "SSC-availability-viewer/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)

        def get_items(self, flow=DEFAULT_FLOW):
            return self._get(f"/api/v1/companies/{self.company}/items/"
                             f"?flow={flow}")["items"]

        def get_calendar(self, item_pk, year, month):
            return self._get(f"/api/v1/companies/{self.company}/items/{item_pk}"
                             f"/calendar/{year}/{month:02d}/")["calendar"]

        def get_availability(self, item_pk, availability_pk):
            return self._get(f"/api/v1/companies/{self.company}/items/{item_pk}"
                             f"/availabilities/{availability_pk}/")["availability"]

        def book_url(self, item_pk, availability_pk):
            return (f"{self.BASE_URL}/embeds/book/{self.company}/items/{item_pk}"
                    f"/availability/{availability_pk}/book/"
                    f"?full-items=yes&flow={DEFAULT_FLOW}")

        def close(self):
            pass

HERE = Path(__file__).resolve().parent

# purchasable items with daily "availability" slots that aren't real events
EXCLUDE_ITEMS = {"gift cards"}

CATEGORIES = [
    ("Waitlists", lambda n: "waitlist" in n.lower()),
    ("ASA Courses", lambda n: n.startswith("ASA")),
    ("Intro Lessons", lambda n: "intro to sailing" in n.lower()),
    ("Clinics & Practice", lambda n: any(k in n.lower() for k in ("clinic", "seminar", "continuing education", "practice"))),
    ("Community Programs", lambda n: any(k in n.lower() for k in ("women", "lgbtq"))),
    ("Social & Racing", lambda n: any(k in n.lower() for k in ("social", "race", "flotilla", "sailfest", "public sail"))),
]


def categorize(name):
    for cat, test in CATEGORIES:
        if test(name):
            return cat
    return "Other"


# Group-fit metadata: (name substring, min experience level, role).
# Levels: 0 = new to sailing, 1 = ASA 101, 2 = ASA 103, 3 = ASA 104+.
# Roles: "learn" = courses/lessons (you helm), "crew" = skippered sails
# (captain provided), "both" = open format. First match wins, so the
# combined-course patterns come before their component courses.
EXPERIENCE_RULES = [
    ("instructor qualification", 3, "learn"),
    ("asa 101", 0, "learn"),      # also catches 101/103/104 Beginner to Bareboat
    ("asa 103/104", 1, "learn"),  # Cruise N Learn requires 101
    ("asa 102", 1, "learn"),
    ("asa 103", 1, "learn"),
    ("asa 104", 2, "learn"),
    ("asa 105", 2, "learn"),
    ("asa 106", 3, "learn"),
    ("asa 111", 1, "learn"),
    ("asa 118", 1, "learn"),
    ("intro to sailing", 0, "learn"),
    ("classroom", 0, "learn"),
    ("seminar", 0, "learn"),
    ("continuing education", 1, "learn"),
    ("racing clinic", 1, "both"),
    ("on-the-water clinic", 1, "learn"),
    ("fun race", 0, "crew"),
    ("flotilla", 0, "crew"),
    ("social", 0, "crew"),
    ("sailfest", 0, "crew"),
    ("public sail", 0, "crew"),
    ("women", 0, "both"),
    ("lgbtq", 0, "both"),
]


def experience_role(name):
    n = name.lower()
    for pat, lvl, role in EXPERIENCE_RULES:
        if pat in n:
            return lvl, role
    return 0, "both"


def month_range(start, count):
    y, m = start.year, start.month
    for _ in range(count):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def fetch_calendar(client, item_pk, year, month):
    try:
        return item_pk, client.get_calendar(item_pk, year, month)
    except Exception as e:
        print(f"  ! item {item_pk} {year}-{month:02d}: {e}", file=sys.stderr)
        return item_pk, None


def fetch_spots(client, ev):
    """Per-customer-type seats (e.g. Skipper vs Crew) from availability detail."""
    try:
        av = client.get_availability(ev["item_pk"], ev["pk"])
    except Exception as e:
        print(f"  ! availability {ev['pk']}: {e}", file=sys.stderr)
        return ev["pk"], None
    # keep only seat ROLES (skipper/crew/students/instructor). The other
    # customer types (Club Members, Non-Members, Partner, Comp...) are pricing
    # tiers sharing the same seat pool — summing them double-counts capacity.
    spots = []
    for ctr in av.get("customer_type_rates") or []:
        label = (ctr.get("unicode") or "").strip()
        if not label or not any(k in label.lower() for k in
                                ("skipper", "crew", "student", "instructor")):
            continue
        spots.append([label, ctr.get("capacity_remaining"),
                      ctr.get("maximum_party_size")])
    return ev["pk"], spots or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6, help="months ahead to harvest (default 6)")
    ap.add_argument("--out", type=Path, default=HERE,
                    help="directory to write index.html and data/ into (default: repo dir)")
    args = ap.parse_args()
    out_dir = args.out.resolve()
    data_dir = out_dir / "data"

    client = FareharborComClient.connect()
    today = dt.date.today()

    print(f"Fetching item list for {client.company} (flow {DEFAULT_FLOW})...")
    items = client.get_items()
    item_info = {}
    for it in items:
        if it["name"].strip().lower() in EXCLUDE_ITEMS:
            continue
        imgs = it.get("images") or []
        lvl, role = experience_role(it["name"])
        item_info[it["pk"]] = {
            "pk": it["pk"],
            "name": it["name"],
            "category": categorize(it["name"]),
            "min_level": lvl,
            "role": role,
            "description": it.get("description", ""),
            "image": (imgs[0].get("image_cdn_url") if imgs else "") or it.get("image_cdn_url", "") or "",
            "sold_out_text": it.get("sold_out_text") or "Sold out",
        }
    print(f"  {len(items)} items")

    months = list(month_range(today, args.months))
    jobs = [(pk, y, m) for pk in item_info for (y, m) in months]
    print(f"Fetching {len(jobs)} item-month calendars ({args.months} months: "
          f"{months[0][0]}-{months[0][1]:02d} .. {months[-1][0]}-{months[-1][1]:02d})...")

    events = {}
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_calendar, client, pk, y, m) for pk, y, m in jobs]
        for fut in as_completed(futs):
            item_pk, cal = fut.result()
            done += 1
            if done % 40 == 0:
                print(f"  {done}/{len(jobs)}")
            if not cal:
                continue
            for week in cal["weeks"]:
                for day in week["days"]:
                    if day.get("month") != "current":
                        continue  # adjacent-month days duplicate across calendars
                    for av in day.get("availabilities") or []:
                        pk = av["pk"]
                        if pk in events:
                            continue  # multi-day events repeat on each day
                        end = av["end_at"][:10]
                        if end < today.isoformat():
                            continue
                        events[pk] = {
                            "pk": pk,
                            "item_pk": item_pk,
                            "start_at": av["start_at"],
                            "end_at": av["end_at"],
                            "headline": av.get("headline") or "",
                            "capacity": av.get("approximate_available_capacity"),
                            "sold_out": av.get("is_sold_out", False),
                            "bookable": av.get("is_bookable", False),
                            "phone_only": av.get("is_bookable_only_by_phone", False),
                            "book_url": client.book_url(item_pk, pk),
                        }

    print(f"Fetching seat-type detail for {len(events)} availabilities (skipper/crew spots)...")
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_spots, client, ev) for ev in events.values()]
        for fut in as_completed(futs):
            pk, spots = fut.result()
            done += 1
            if done % 80 == 0:
                print(f"  {done}/{len(events)}")
            events[pk]["spots"] = spots
    client.close()

    payload = {
        "company": "Seattle Sailing Club",
        "harvested_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "months_ahead": args.months,
        "items": item_info,
        "events": sorted(events.values(), key=lambda e: e["start_at"]),
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    out_json = data_dir / "events.json"
    out_json.write_text(json.dumps(payload, indent=1))
    print(f"Saved {len(events)} events -> {out_json}")

    template = (HERE / "template.html").read_text()
    marker = "window.__DATA__ = null;"
    if marker not in template:
        sys.exit("template.html is missing the data marker")
    html = template.replace(marker, "window.__DATA__ = " + json.dumps(payload) + ";")
    (out_dir / "index.html").write_text(html)
    print(f"Generated {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
