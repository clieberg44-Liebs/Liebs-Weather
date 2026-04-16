# Liebs Weather · Foxen Canyon Dashboard

A live dashboard for the Foxen Canyon personal weather station, powered by the WeatherLink v2 API and refreshed every 15 minutes by a GitHub Action.

**Live site:** [clieberg44-liebs.github.io/Liebs-Weather](https://clieberg44-liebs.github.io/Liebs-Weather/)

---

## How it works

```
┌────────────────────┐    every 15 min     ┌──────────────────────────┐
│  WeatherLink v2    │ ─────── API ──────▶ │  GitHub Action           │
│  (api.weather      │                     │  scripts/fetch_weather   │
│   link.com/v2)     │                     │  link.py                 │
└────────────────────┘                     └────────────┬─────────────┘
                                                        │ commits
                                                        ▼
                                           ┌──────────────────────────┐
                                           │  data/weather.json       │
                                           └────────────┬─────────────┘
                                                        │ fetched on page load
                                                        ▼
                                           ┌──────────────────────────┐
                                           │  index.html (dashboard)  │
                                           │  served by GitHub Pages  │
                                           └──────────────────────────┘
```

Your API key **never touches the browser** — it lives only in GitHub Secrets. The browser only ever fetches the pre-built `data/weather.json` file.

---

## One-time setup

### 1. Get your WeatherLink v2 credentials

1. Log in at [weatherlink.com](https://www.weatherlink.com/account).
2. Go to **Account**.
3. Under **API Key v2**, click **Generate v2 Key** if you don't already have one.
4. Copy the **API Key** and **API Secret** somewhere safe. You'll paste them into GitHub in a moment.
5. Note your **Station ID**: at [weatherlink.com](https://www.weatherlink.com), open your station's live page — the URL contains a UUID (or a numeric ID).

### 2. Add the secrets to your GitHub repo

On GitHub, go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add these three:

| Secret name              | Value                              |
| ------------------------ | ---------------------------------- |
| `WEATHERLINK_API_KEY`    | your API Key from step 1           |
| `WEATHERLINK_API_SECRET` | your API Secret from step 1        |
| `WEATHERLINK_STATION_ID` | your numeric Station ID or UUID    |

If your account has exactly one station, you can skip `WEATHERLINK_STATION_ID` and the script will auto-detect.

### 3. Enable GitHub Pages

**Settings** → **Pages** → **Source: Deploy from a branch** → Branch: `main` / `/ (root)` → **Save**.

After the first Action run succeeds, the site will be live at `https://<user>.github.io/<repo>/`.

### 4. Enable workflow write permissions

**Settings** → **Actions** → **General** → scroll to **Workflow permissions** → select **Read and write permissions** → **Save**. This allows the Action to commit the refreshed `data/weather.json` back to the repo.

### 5. Seed the historical data (first run)

The repo ships with a `data/weather.json` seed built from your existing workbook (2025 full year + 2026 YTD through April 15). To repopulate it fresh from the WeatherLink API instead, or to refresh from a later workbook:

**Option A — backfill from WeatherLink** (slow: one API call per day, ~5–10 min):

1. Go to **Actions** tab → **Refresh Weather Data** workflow.
2. Click **Run workflow** → set **mode** to `backfill` → **Run workflow**.

**Option B — use a workbook** (fast, requires uploading the .xlsm):

1. Drop an updated `Weather_Data.xlsm` into `data/Weather_Data.xlsm` and commit it.
2. Run the `backfill` workflow as above. It'll merge workbook-sourced historical normals (avgHi / avgLo) with live API data.

After the first successful run, the every-15-min `refresh` mode will keep things current.

---

## File layout

```
├── index.html                         ← the dashboard (loads data/weather.json)
├── data/
│   ├── weather.json                   ← refreshed by the Action (gitignored: no)
│   └── Weather_Data.xlsm              ← optional: source workbook for historical normals
├── scripts/
│   └── fetch_weatherlink.py           ← Python script the Action runs
├── .github/
│   └── workflows/
│       └── refresh-weather.yml        ← the Action itself
└── README.md
```

---

## Running the fetch script locally

```bash
export WEATHERLINK_API_KEY=...
export WEATHERLINK_API_SECRET=...
export WEATHERLINK_STATION_ID=...      # optional if only one station
export WEATHERLINK_TZ="America/Chicago"

pip install requests pandas openpyxl

# Just today + current conditions
python scripts/fetch_weatherlink.py --mode refresh

# Full year-to-date backfill
python scripts/fetch_weatherlink.py --mode backfill
```

Opening `index.html` directly in the browser with `file://` **will not work** — browsers block `fetch()` of local JSON by default. Use a tiny local server:

```bash
python3 -m http.server 8000
# then visit http://localhost:8000
```

---

## Rate limits and subscription notes

- **Rate limit:** WeatherLink v2 allows plenty of headroom for a 15-minute cron. Full backfill does ~100–365 calls, well under daily quotas.
- **Historic data requires Pro or Pro+:** the `/historic/{id}` endpoint is gated. If you're on the Basic tier, the backfill will return empty days. Current conditions work on all tiers.
- **Archive records are capped at 24 hours per request:** the script handles this by calling day-by-day.

If backfill returns empty data, the most likely cause is a Basic-tier subscription. Let me know and we can adjust to current-conditions-only mode.

---

## Troubleshooting

**The Action runs but `data/weather.json` doesn't update**
→ Check **Settings** → **Actions** → **General** → **Workflow permissions** is set to Read and write.

**All the live tiles show "—"**
→ Open `data/weather.json` in the browser (`https://<user>.github.io/<repo>/data/weather.json`). If `current` is `{}`, the API fetch is failing. Check the Action logs in the Actions tab.

**My field names are different**
→ Davis stations vary (WeatherLink Live vs. Vantage Connect vs. EnviroMonitor). The script tries multiple common field names, but if your station uses unusual ones open a commit with the raw `/current/{id}` JSON and I can widen the field-name dictionary.

**The cron runs less often than every 15 min**
→ GitHub Actions cron is best-effort and commonly drifts 5–15 min under load. For more guaranteed timing you'd need a paid scheduler (not worth it for weather).

---

## Credits

- Station: Foxen Canyon, middle Tennessee (approx. 35.924° N, 86.868° W)
- Data: Davis Instruments station via [WeatherLink v2 API](https://weatherlink.github.io/v2-api/)
- Typography: [Rajdhani](https://fonts.google.com/specimen/Rajdhani) and [Share Tech Mono](https://fonts.google.com/specimen/Share+Tech+Mono)
- Pipeline: GitHub Actions + GitHub Pages
