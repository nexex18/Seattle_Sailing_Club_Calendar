# Seattle Sailing Club — Event & Availability Browser

Harvests every upcoming event from Seattle Sailing Club's FareHarbor booking
system and renders it as a single, filterable HTML page.

Built on the **Universal API Harvester** pattern (discover once, call many
times). No browser, no auth — FareHarbor's embed API is public JSON.

## Usage

```bash
python3 harvest.py            # pull 6 months of availability (~260 fast API calls)
python3 harvest.py --months 9 # pull further ahead
open index.html               # browse it
```

## What you get

- `index.html` — self-contained agenda browser (data embedded): grouped by
  month/day, search, category chips (ASA Courses, Intro Lessons, Clinics,
  Social & Racing, Community Programs), "open spots only" and waitlist
  toggles, spots-remaining / sold-out status, and direct **Book** links into
  FareHarbor checkout.
- **Plan for my group** panel — set party size, least-experienced sailor
  (any / new / ASA 101 / 103 / 104+), and role (learn-or-captain vs crew with
  captain provided). The agenda narrows to sailings your whole group can
  actually book. Program experience requirements are tagged in
  `EXPERIENCE_RULES` in `harvest.py` — adjust there if SSC changes prereqs.
- **Skipper / crew seat counts** — each availability's `customer_type_rates`
  detail is harvested, so social sails and races show per-role seats
  ("skipper 1 · crew 2") and courses show student seats. Membership pricing
  tiers (Club Members / Non-Members / Partner) share one seat pool and are
  deliberately excluded from the math.
- `data/events.json` — the raw harvested data if you want it elsewhere.

## Architecture

| Piece | Where |
|---|---|
| API discovery notes + endpoint map | `~/python_projects/Universal_API_Harvester/captures/fareharbor-com/` |
| Generated API client (httpx) | `~/python_projects/Universal_API_Harvester/generated/fareharbor_com_client.py` |
| Harvest + page build | `harvest.py` (this repo) |
| UI template | `template.html` → rendered to `index.html` with data injected |

Key endpoint: `GET /api/v1/companies/seattlesailing/items/{pk}/calendar/{year}/{month}/`
— one call per item-month returns full availability objects (capacity,
sold-out, bookable, book URL). Multi-day events are deduped by availability pk.
