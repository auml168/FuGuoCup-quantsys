# FuGuoCup Quant

**Cross-sectional sector momentum in Fullgoal OTC funds: signal construction, execution costs, and walk-forward evidence**

Personal project · Python / pandas / NumPy · [中文版](README.md)

**Research question.** Can cross-sectional sector momentum, combined with a trend filter, generate a stable risk-adjusted return in Fullgoal OTC funds after subscription/redemption fees, confirmation and settlement lags, and purchase limits?

**Answer.** There is some signal in-sample and in rolling validation, but only `sector_rotation` is positive in both rolling validation and the final 126-day holdout (+0.21% / +1.20%). Its 9.2x turnover and ¥6,329.89 fees are material, and no strategy cleared the deployment gate. The system therefore remains research-only.

## Research summary

| Item | Detail |
|---|---|
| Sample | 2021-01-04 → 2026-10-16 |
| Universe | 917 catalog records → 577 supported → 570 collected → 500 researched |
| Strategies | 9 fixed strategies |
| Validation | 252-day initial train · 8 × 126-day rolling folds · 126-day holdout |
| Execution | T-2 signal · T+1 confirmation · T+3 redemption settlement |
| Costs | Subscription fee, FIFO redemption tiers, minimum holding period, purchase limits, minimum order |
| Result | `sector_rotation` is the only double-positive strategy; no deployment approval |
| Delivery | Daily 14:20 Telegram target-weight signal; manual execution before 15:00 |

## Hypotheses

| Hypothesis | Test | Result |
|---|---|---|
| H1: Sector-level 20-day momentum persists | Rank sectors by average 20-day momentum; take top 2; 8 rolling folds + 126-day holdout | Weak support: +0.21% / +1.20%, limited magnitude |
| H2: Trend filtering and risk-adjusted ranking improve drawdown | Require `mom60 > 0`; rank on momentum, volatility, and drawdown | Mixed: lower drawdown than raw momentum, but `risk_budget` has negative rolling validation |
| H3: OTC fees and settlement lags materially erode the signal | Explicit subscription fee, FIFO redemption fee, T+1 confirmation, T+3 settlement | Supported: 9.21x turnover and ¥6,329.89 fees, about 3.16% of initial capital |

## Universe and data

- Discover products from the Fullgoal fund-company catalog; open each fund page and verify the manager ID. Exclude QDII, FOF, money market, gold/commodity, non-feeder ETFs, on-exchange, holding-period/regular-open/closed, back-end load, term/rolling/graded products, and unmapped categories.
- Parse unit NAV, vendor daily return, dividend/split events, subscription/redemption status, and purchase limits. Raw responses and CSVs are archived with SHA-256; snapshots can be rebuilt offline from the archived responses.
- Bond index funds (Eastmoney category `指数型-固收`) are treated as fixed income; convertible and mixed-bond funds are tagged separately. A/C share classes are grouped by name and only one share class per underlying is eligible.
- When unit NAV and vendor return disagree without an event explanation, the interval is marked `quality_break`. History is not deleted retroactively; a fund must re-accumulate history after the break, and a holding that crosses a break invalidates that backtest segment.

## Signal design

**Features.** For the past 128 observations: 20/60/126-day momentum, 20/60-day volatility, deviation from the 120-day moving average, 60-day drawdown, and risk-adjusted momentum. Every feature uses data available at or before the signal date.

**Sector rotation.** Rank sectors by average 20-day momentum and take the top two with positive momentum. Within those sectors, keep funds with positive 60-day momentum and select at most three by composite score.

**Portfolio constraints.** Single fund ≤35%, single sector ≤50%, total exposure ≤95%, one share class per group, remainder in cash. Constraints are applied during weight projection, not by post-hoc clipping.

**Execution lag.** Signals use NAV through T-2; orders submitted on T are confirmed on T+1 and redemption proceeds settle on T+3. The backtest never uses an unpublished NAV or reuses unsettled redemption cash.

### Factor definitions

| Factor | Definition |
|---|---|
| `mom20/60/126` | `P_t / P_{t-n} - 1` |
| `vol20/60` | Daily return standard deviation × √252 |
| `trend` | `P_t / mean(P_{t-119:t}) - 1` |
| `drawdown60` | `P_t / max(P_{t-59:t}) - 1` |
| `risk_adjusted` | `mom60 / max(vol60, 0.01)` |
| `score` | 30% rank(mom20) + 30% rank(mom60) + 20% rank(risk_adjusted) + 20% rank(-vol60) |

Cross-sectional ranks use only the fund set available on that date; later listings or restored funds are not projected backward.

## Backtest and execution assumptions

- Custom event-driven ledger: cash, tax lots, receivables, pending subscriptions/redemptions, fees, confirmations, settlement, locked shares, and cash overdraft are tracked explicitly.
- Subscription fee is charged outside the invested amount; redemptions use FIFO lots, calendar-day age, and tiered fees; lots inside the minimum holding period cannot be redeemed.
- Missing NAVs, long gaps, fills without a NAV, and quality breaks are recorded as segment errors. No zero-return or stitched-return fill is used.
- No third-party backtest engine; no leverage; no forced liquidation at the end; terminal value uses the last observable NAV.
- Research rebalances weekly; the live signal updates daily. Daily execution would increase turnover and fees, and that difference has not been validated in the backtest.

## Validation protocol

252 initial observations, eight non-overlapping 126-day rolling folds, and the last 126 observations (2026-04-10 to 2026-10-16) as a holdout. Parameters are fixed; there is no parameter search and no selection based on the test set.

- Each fold uses only data available before the fold starts. Positions, fees, confirmations, and settlements roll continuously across folds; cash is not reset per fold.
- Nine strategies are evaluated together. Equal weight is the incumbent benchmark, but it is not promoted because it wins one window.
- The holdout is used for reporting only; it does not select parameters, strategies, or weights.
- Multiple strategies × multiple windows create selection risk. A single fold's excess return or t-statistic is not treated as significant evidence; it is only a filter for further research.

## Results

Full run on 2026-10-17; holdout from 2026-04-10 to 2026-10-16:

| Strategy | Rolling validation | Holdout | Max drawdown | Sharpe | Turnover | Fees (CNY) |
|---|---:|---:|---:|---:|---:|---:|
| `sector_rotation` | +0.21% | **+1.20%** | 21.44% | 0.22 | 9.21 | 6,329.89 |
| `timing` | +21.18% | -6.13% | 18.70% | -0.60 | 8.76 | 5,690.10 |
| `risk_budget` | -6.96% | +0.37% | 10.23% | 0.12 | 4.08 | 2,671.70 |
| `equal_weight` | Not verifiable | -7.87% | 14.04% | -0.89 | 1.21 | 467.79 |
| `cash` | 0.00% | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 |

**Findings.**

- Only `sector_rotation` is positive in both rolling validation and the holdout. `timing` has the highest rolling validation (+21.18%) but a negative holdout (-6.13%), indicating regime dependence.
- The +1.20% holdout return is net of fees. Fees are ¥6,329.89 (about 3.16% of initial capital) with 9.21x turnover; costs are a binding constraint.
- The equal-weight incumbent is not reliably priceable: a holding crossed an unexplained NAV break on 2022-04-06. The segment is left empty rather than stitched into a return.
- No strategy cleared the deployment gate. The system does not auto-promote, does not produce executable amounts, and does not switch winners based on the holdout.
- The holdout has already been viewed and is not a forward sample. Historical results do not guarantee future performance.

**Research takeaway.** Sector momentum is not a stable alpha in this universe: the sign is inconsistent across rolling validation and the holdout, and costs bind. The more valuable output is the research infrastructure—reproducible quality-break quarantine, an explicit OTC settlement ledger, and an honest negative result—not an investable return curve.

## Live signal

Only the 14:20 scheduled run collects data and sends. The Telegram message contains target weights, action direction, and a reference amount based on the account value, with a 15:00 execution deadline.

```text
🟢 FuGuoCup trading signal
2026-10-19 14:20 | strategy sector_rotation | regime risk_on
━━━━━━━━━━━━━━
① What to buy / adjust
1. 161024 ... (defense)
   🟢 buy/build | target 35.0% | reference CNY 70,000
2. ...
   🟠 reduce | target 10.0% | sell about CNY 20,000

② Why this portfolio
• Market: risk_on, positive-trend breadth 100%
• Selection: top 2 sectors by 20-day momentum; within sector, positive 60-day momentum and top 3 composite score
• Constraints: single fund ≤35% / single sector ≤50% / total exposure ≤95%

③ How to execute
• Target trading day: 2026-10-19
• Deadline: rebalance to target before 15:00
• Target portfolio: 70.0% invested, 30.0% cash
• Reference amounts are estimates; purchase limits and suspension rules are set by the platform
• Research signal, not an execution instruction

④ Risk
• Historical results are not forward-looking; no return guarantee
```

Reference amounts are estimates from target weight × account value. They do not replace platform confirmation, fees, or daily purchase limits. The formal order path caps purchases at the current limit and respects subscription/redemption status; the Telegram reference amount is only for sizing.

**Next-step directions.** By default the system treats the last sent signal as executed, so the next signal produces add/reduce/clear directions from that assumed portfolio without manual account updates. If `ACCOUNT_JSON` is supplied, the reconciled account overrides the assumption; actual fills and NAVs can still differ.

## Implementation

- `src/fullgoal_quant/` separates data, universe construction, signals, accounting, portfolio construction, reporting, and notification. Research outputs are written to a manifest with config, source hashes, and time windows.
- 70 regression tests cover timing, fees, settlement, missing data, quality breaks, accounts, notification reservations, and signal text. CI runs Python 3.11/3.12/3.13 plus a live public-data smoke test.
- GitHub Actions runs at 14:20 for collection, research, and Telegram delivery; 08:30/20:30 triggers exit early to avoid duplicate collection.
- A durable reservation artifact is uploaded before Telegram is called; retries and timeouts do not blindly resend. Logs and public artifacts contain no account report, outbox, or token.

## Limitations and next experiments

- Survivorship bias: the catalog has no full historical delisting set.
- Vendor NAVs are revised after the fact; T-2 reduces release lag but does not prove point-in-time availability.
- Historical trading days use a domestic equity NAV calendar as a proxy; current and future operations use the official annual calendar.
- Fees, confirmation dates, settlement, and purchase limits must be verified per fund in formal mode; research defaults are assumptions.
- Next experiments:
  - **Data:** point-in-time fund universe and delistings; NAV release timestamps instead of the T-2 proxy.
  - **Signal:** robustness to sector definition and momentum window; equal-weight vs size-weighted sector aggregation; decompose sector-selection and fund-selection alpha.
  - **Portfolio:** daily vs weekly rebalancing net of costs; volatility/drawdown-conditioned exposure.
  - **Execution:** purchase limits, confirmation/settlement, limit prices, and capacity; calibrate fee assumptions with platform records.
  - **Validation:** forward records after freezing the design; multi-window significance tests rather than single-window returns.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v

fq doctor --limit 12
fq collect
fq research

cp config/account.example.json config/account.local.json
fq report --research --account config/account.local.json
```

Common commands:

```bash
fq discover
fq --data data/smoke research
fq --data data/expanded backtest --baseline-eight
fq calendar-check --year 2027
fq validate
fq collect --competition
```

## Configuration

| Field | Meaning |
|---|---|
| `competition.allowed_codes` | Official competition whitelist; empty uses the auto-discovered Fullgoal OTC pool in research mode |
| `rules` / `fund_rules` | Fees, confirmation/settlement, minimum holding, minimum order; verified per fund in formal mode |
| `signal.strategy` | Daily signal strategy, currently `sector_rotation` |
| `signal.daily` / `signal.buy_deadline` | Daily signal and 15:00 execution deadline |
| `signal.assume_execution` | Default true: treat the last sent signal as executed; `ACCOUNT_JSON` overrides with the reconciled account |
| `universe.workers` | Collection concurrency, currently 8 |
| `data.max_nav_lag_sessions` | Allowed NAV lag; the report blocks beyond it |

Account files: `config/account.example.json` is an empty template; `config/account.local.json` is the real account and is gitignored; the cloud uses GitHub Secret `ACCOUNT_JSON`.

## Project layout

```text
src/fullgoal_quant/
  data.py         collection, parsing, archives, SHA-256
  universe.py     Fullgoal OTC classification, share groups, manager checks
  signals.py      factors, strategies, portfolio constraints
  backtest.py     ledger, fees, confirmation, backtest
  research.py     rolling validation and manifest
  portfolio.py    account state and order proposals
  report.py       report, daily signal, Telegram text
  calendar.py     official calendar and 2027 parsing
tests/            70 regression tests
scripts/          cloud run, send, state restore, security checks
config/           settings / calendar / account.example
docs/             methodology, deployment, review
sample_results/   reproducible research evidence (historical)
```

## Documentation

[Methodology](docs/METHODOLOGY.md) · [Deployment](docs/DEPLOYMENT.md) · [Review](docs/REVIEW.md)

## Note

Personal research and competition support only; not investment advice. The system does not guarantee returns or take responsibility for trading outcomes. All trades are executed manually on the competition platform.
