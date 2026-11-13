# Congress Trades: data pipeline + website

Pulls every stock trade US members of Congress disclose under the STOCK Act, matches it to
the member's committee seats, and flags trades where the company's sector falls under a
committee the member sits on.

## What it does

| Step | Source | How |
|---|---|---|
| 1. Find new House filings | House Clerk yearly index (`{YEAR}FD.zip`) | XML, filter Periodic Transaction Reports |
| 2. Read House trades | House PTR PDFs | E-filed (~88%): rules on the PDF text layer. Scanned (~12%): Claude reads the PDF |
| 3. Find + read Senate trades | Senate eFD search + PTR pages | HTML table. Paper scans queued for review |
| 4. Member + committees | `unitedstates/congress-legislators` (GitHub) | Match by name + state/district, on any seat held since 2021 (current and former members) |
| 5. Company sector | SEC ticker list + SEC SIC code | `config/sic_sectors.csv` |
| 6. Overlap flag | `config/committee_sectors.csv` | Trade sector is one of the member's committee sectors |
| 7. Checks | `src/validate.py` | Official amount ranges, date order, ticker in SEC list, AI confidence |

Both mapping files are plain CSVs: edit them to change the method. Publish them on the site
as the methodology.

## Outputs (`data/`)

| File | One row per | Use |
|---|---|---|
| `trades.csv` | transaction | Main table. Opens in Excel / Power BI |
| `filings.csv` | filing | Processing status of every report |
| `review.csv` | item needing a human look | Trades failing a check + filings not parsed |
| `members.csv` | current member | Name, party, seat, committees, leadership roles (feeds the website) |
| `holdings.csv` | member x stock held | Estimated position, % of portfolio, held by (Self / Spouse / Joint / Dependent), recent activity |
| `portfolios.csv` | member who trades | Stocks held, est. portfolio, 12-month return vs S&P (same dates), return if copied on filing date |
| `meta.json` | site | Price source in use, plain S&P 500 12-month return |

`trades.csv` key columns:

| Column | Meaning |
|---|---|
| `chamber`, `filer_name`, `bioguide`, `party`, `state` | Who |
| `tx_date`, `filing_date`, `filing_lag_days`, `late` | When; `late` = filed > 45 days after the trade |
| `owner` | Self / Spouse / Joint / Dependent |
| `ticker`, `asset_name`, `asset_type`, `tx_type` | What (Purchase / Sale / Sale (Partial) / Exchange) |
| `amount_range`, `amount_min`, `amount_max` | Disclosed range (exact amounts are not disclosed) |
| `sector` | From SEC SIC code; only for stocks and options |
| `overlap`, `overlap_committees` | Committee-sector overlap flag and which committees |
| `parse_method`, `confidence`, `issues`, `status` | `text` / `html` / `ai`; `status` = ok or review |
| `source_url` | Original filing. Link it on every row of the site |

## Set up (once, ~20 min)

1. Create a **public** GitHub repo (public keeps hosting and automation free) and upload
   everything in this folder: **Add file → Upload files**.
2. **Settings → Actions → General → Workflow permissions** → **Read and write** → Save.
3. **Settings → Secrets and variables → Actions** → add:
   - `SCRAPER_USER_AGENT` = `YourSiteName your@email.com` (SEC requires a contact)
   - `ANTHROPIC_API_KEY` = your Claude API key (only used for scanned filings)
   - `MARKETSTACK_API_KEY` = add before the site goes public (see "Portfolios and performance")
4. **Actions → Update trades → Run workflow**, and type `2022-01-01` in the "since" box so
   portfolios have history. A run stops starting new filings after 150 minutes and saves;
   the next run picks up the rest, so the first load spreads over a few runs. Later runs take minutes.
5. **Settings → Pages** → Source: **Deploy from a branch**, branch `main`, folder `/ (root)` → Save.
   The site is live at `https://YOUR-USERNAME.github.io/REPO-NAME/` within ~2 min.
6. From then on it runs every 3 hours, commits fresh CSVs, and the site updates itself.

If a source breaks (site layout change, block), the run fails and GitHub emails you.

How runs handle problems:

| Situation | What happens |
|---|---|
| One filing errors (server hiccup) | Marked `error`, retried next run; scheduled runs give up after 3 tries, a manual **Run workflow** retries all |
| More than 5% of filings (min 5) error | Run is marked failed (email) |
| 3 AI errors in a row (e.g. no API credit left) | AI switched off for the rest of the run, scanned filings wait as `needs_ai`, run marked failed |
| House rules parser improved (`PARSER_VERSION` in `src/house.py`) | Filings read by the older version are read again automatically, old rows replaced |
| Older filings still need work | The run looks back to their date by itself; no "since" needed |

Run locally (House only; the Senate site blocks non-US connections):

```
pip install -r requirements.txt
python -m pytest -q
python src/pipeline.py --chambers house --since 2026-10-01 --max 20
```

## Portfolios and performance (`src/portfolio.py`)

Members disclose trades and dollar ranges, not share counts, so portfolios are estimates:
buys add the range midpoint, partial sales reduce it, a full sale closes the position.
Stocks only. Positions are tracked per owner (member, spouse, joint, dependent child) and
combined under the member. Positions bought before the data starts and never traded since don't show.

Performance needs daily prices. Two sources, chosen automatically:

| Source | When | Licence |
|---|---|---|
| Yahoo Finance via `yfinance` (free) | No `MARKETSTACK_API_KEY` set | Personal use only: testing and private use |
| Marketstack Basic ($9.99/month) | `MARKETSTACK_API_KEY` set | Lists commercial use; confirm public display in writing |

Set `PRICE_SOURCE=none` to switch performance off. The Method page names the source in use
(`data/meta.json`). Figures:

| Figure | Definition |
|---|---|
| 12-month return | Every stock bought in the last 12 months, from trade date to today (or full sale), weighted by range midpoint |
| vs S&P | Same calculation for SPY over the same days, subtracted |
| If copied on filing date | Same, starting on the day the trade became public |

Each member's "vs S&P (same dates)" uses the S&P over the days their money was in each stock,
so it differs per member. The plain 12-month S&P return is shown once on the home page.

The calculation is ours (`src/portfolio.py`); only the daily closing prices come from a
vendor. Before going public, add the Marketstack key and ask Marketstack to confirm in
writing that showing returns calculated from their prices on a public website is covered.
Prices are fetched at most once a day (~8 requests), with a weekly full refresh
(~1 request per 100 tickers per 1,000 trading days): about 3,500 requests a month.
Raw prices are cached outside the repo so they are never published.

## Website (`index.html`)

One file, no build step, no server. It reads the CSVs in `data/` and
`config/committee_sectors.csv`, and shows only trades with `status = ok`.

| Page | Link | Shows |
|---|---|---|
| Home | `#home` | Best performers (12 months), top 10 most owned, top 10 by %, big bets, top buys / sells in 90 days (expandable), latest filings |
| Member | `#member-<bioguide>` | Portfolio: stock, % of portfolio, recent activity, est. value, committee overlap; trade history |
| Stock | `#stock-<TICKER>` | Who owns it, % of their portfolio, trades |
| Members | `#members` | Every member of the House and Senate (All / House / Senate), sortable |
| Activity | `#activity` | Every trade, newest first |
| Grand portfolio | `#grand` | All holdings combined |
| Late filers | `#late` | Filed more than 45 days after the trade |
| Method | `#method` | How every number is worked out, committee-to-sector table |

- **Name:** "Disclosure Desk" is a working title. Change it in the `<title>` and the `brand` link.
- **Preview on your PC:** opening the file directly can't load the CSVs. Run
  `python -m http.server 8000` in this folder, then open `http://localhost:8000`.
- **Ads:** add your ad network's snippet before `</head>` once traffic justifies it.

## Running cost

| Item | US$/month |
|---|---|
| GitHub Actions (public repo free; private ~500 of 2,000 free minutes) | 0 |
| Claude for scanned filings (~60/yr House, Sonnet 5.5 at $2/$10 per M tokens, ~$0.04 each) | < 1 |
| SEC, House, Senate, GitHub data | 0 |
| Website hosting (GitHub Pages) | 0 |
| Prices for performance (Marketstack Basic, optional) | 9.99 |
| Custom domain (optional) | ~1 |

## Verification status

| Part | Tested against | Status |
|---|---|---|
| House index | Real 2026 index rows | ✅ |
| Member matching, committees | Real current Congress data (539 members, 230 committees) | ✅ |
| Committee → sector mapping | Every mapped committee ID exists in the real data | ✅ |
| Sector, overlap, checks, storage, re-runs | Unit + end-to-end tests | ✅ |
| House PDF rules parser | 24 real 2026 filings, 237 trades, checked field by field (`tests/fixtures/house_real/`) | ✅ |
| Portfolio estimates, returns vs S&P, price fetching | Unit tests with fixed prices and a fake price API | ✅ |
| Senate search + PTR parser | First live run: 589 filings, 5,783 trades | ✅ |
| AI extractor | Mocked API. First live run: Sonnet 5.5 rejects forced tool use (fixed: `tool_choice` auto) | ⚠️ Confirm on next run |
| House parser, 2022-style PDFs (labels spelled out, odd capitals) | Synthetic PDF built from real live-run text | ✅ |
| Former members, chamber switchers | Real current + historical member data: 26,915 of 26,930 live trades match | ✅ |

Any e-filed House PDF where the rule parser's row count doesn't match the number of asset
codes in the document is re-read by Claude automatically, so a parser miss becomes a cost
(cents) rather than bad data.

## Known limits

- Committee seats are **current** assignments, applied to past trades. Former members have
  no committee data, so their trades never get an overlap flag. Add history before
  analysing older years.
- SIC codes are coarse (e.g. Alphabet is "services-computer programming"). Fix individual
  cases in `config/sic_sectors.csv` or add a ticker override if needed.
- Amounts are ranges by law. Performance calcs must use range midpoints or counts.
- Senate paper filings are listed in `review.csv` with links, not parsed (rare).
- Wording on the site: report facts ("bought $15K–$50K of LMT while on Armed Services"),
  never "insider trading".
