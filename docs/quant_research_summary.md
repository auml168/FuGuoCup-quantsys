# Cross-Sectional Sector Momentum in Fullgoal OTC Funds

**Research summary · Personal project · Python / pandas / NumPy**

## Research question

Can cross-sectional sector momentum, combined with a trend filter, generate a stable risk-adjusted return in Fullgoal OTC funds after subscription/redemption fees, confirmation and settlement lags, and purchase limits?

## Design at a glance

| Item | Detail |
|---|---|
| Sample | 2021-01-04 → 2026-10-16 |
| Universe | 917 catalog records → 577 supported → 570 collected → 500 researched |
| Strategies | 9 fixed strategies |
| Validation | 252-day initial train · 8 × 126-day rolling folds · 126-day holdout |
| Execution | T-2 signal · T+1 confirmation · T+3 redemption settlement |
| Costs | Subscription fee, FIFO redemption tiers, minimum holding, purchase limits |
| Result | Only `sector_rotation` positive in both rolling validation and holdout; no deployment approval |

## Signal

- **Factors:** 20/60/126-day momentum, 20/60-day volatility, 120-day MA deviation, 60-day drawdown, risk-adjusted momentum.
- **Sector rotation:** rank sectors by average 20-day momentum; take the top 2 positive sectors; within each, require `mom60 > 0` and select at most 3 funds by composite score.
- **Composite score:** 30% rank(mom20) + 30% rank(mom60) + 20% rank(risk-adjusted) + 20% rank(-vol60).
- **Constraints:** single fund ≤35%, single sector ≤50%, total exposure ≤95%, one share class per group, remainder in cash.

## Validation and results

252 initial observations, eight non-overlapping 126-day rolling folds, and a 126-day holdout (2026-04-10 to 2026-10-16). Parameters are fixed; there is no parameter search and no selection based on the holdout.

| Strategy | Rolling validation | Holdout | Max drawdown | Sharpe | Turnover | Fees (CNY) |
|---|---:|---:|---:|---:|---:|---:|
| `sector_rotation` | +0.21% | **+1.20%** | 21.44% | 0.22 | 9.21 | 6,329.89 |
| `timing` | +21.18% | -6.13% | 18.70% | -0.60 | 8.76 | 5,690.10 |
| `risk_budget` | -6.96% | +0.37% | 10.23% | 0.12 | 4.08 | 2,671.70 |
| `equal_weight` | Not verifiable | -7.87% | 14.04% | -0.89 | 1.21 | 467.79 |
| `cash` | 0.00% | 0.00% | 0.00% | 0.00 | 0.00 | 0.00 |

## Findings

- Only `sector_rotation` is positive in both rolling validation and the holdout. `timing` has the highest rolling validation (+21.18%) but a negative holdout (-6.13%), indicating regime dependence.
- The +1.20% holdout return is net of fees. Fees are ¥6,329.89 (about 3.16% of initial capital) with 9.21x turnover; costs are a binding constraint.
- The equal-weight incumbent is not reliably priceable: a holding crossed an unexplained NAV break on 2022-04-06. The segment is left empty rather than stitched into a return.
- No strategy cleared the deployment gate. The system remains research-only and does not select winners on the holdout.
- The holdout has already been viewed and is not a forward sample. Historical results do not guarantee future performance.

## Research takeaway

Sector momentum is not a stable alpha in this universe: the sign is inconsistent across rolling validation and the holdout, and costs bind. The more valuable output is the research infrastructure — reproducible quality-break quarantine, an explicit OTC settlement ledger, and an honest negative result.

## Next experiments

- Point-in-time fund universe and delistings; NAV release timestamps instead of the T-2 proxy.
- Robustness to sector definition and momentum window; decompose sector-selection and fund-selection alpha.
- Daily vs weekly rebalancing net of costs; purchase-limit, settlement, and capacity-aware execution.
- Forward records after freezing the design; multi-window significance tests rather than single-window returns.

---

Code, methodology, and reproducible evidence: https://github.com/auml168/FuGuoCup-quantsys

Personal research only; not investment advice.
