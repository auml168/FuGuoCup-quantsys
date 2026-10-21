# Cross-Sectional Sector Momentum in Fullgoal OTC Funds

**Research summary · Personal project · Python / pandas / NumPy**

## Research question

Can cross-sectional sector momentum, combined with a trend filter, generate a stable risk-adjusted return in Fullgoal OTC funds after subscription/redemption fees, confirmation and settlement lags, and purchase limits?

## Design at a glance

| Item | Detail |
|---|---|
| Sample | 2021-01-04 → 2026-10-16 |
| Universe | 917 catalog codes → 808 OTC → 701 scored → 500 tradeable in backtest |
| Strategies | 9 fixed strategies |
| Validation | 252-day initial train · 8 × 126-day rolling folds · 126-day holdout |
| Execution | T-2 signal · T+1 confirmation · T+3 redemption settlement |
| Costs | Subscription fee, FIFO redemption tiers, minimum holding, purchase limits |
| Result | No strategy shows a stable out-of-sample edge net of fees; no deployment approval |

## Signal

- **Factors:** 20/60/126-day momentum, 20/60-day volatility, 120-day MA deviation, 60-day drawdown, risk-adjusted momentum.
- **Sector rotation:** rank sectors by average 20-day momentum; take the top 2 positive sectors; within each, require `mom60 > 0` and select at most 3 funds by composite score.
- **Composite score:** 30% rank(mom20) + 30% rank(mom60) + 20% rank(risk-adjusted) + 20% rank(-vol60).
- **Constraints:** single fund ≤35%, single sector ≤50%, total exposure ≤95%, one share class per group, remainder in cash.

## Validation and results

252 initial observations, eight non-overlapping 126-day rolling folds, and a 126-day holdout (2026-04-10 to 2026-10-16). Parameters are fixed; there is no parameter search and no selection based on the holdout.

| Strategy | Rolling validation | Holdout | Max drawdown | Sharpe | Turnover | Fees (CNY) |
|---|---:|---:|---:|---:|---:|---:|
| `sector_rotation` | -4.38% (3/8 folds +) | **+1.23%** | 20.33% | 0.22 | 9.20 | 6,357 |
| `dual_momentum` | +1.00% (3/8) | +0.20% | 24.47% | 0.16 | 8.43 | 5,718 |
| `timing` | +21.79% (3/8) | -7.90% | 21.01% | -0.71 | 9.23 | 5,941 |
| `risk_budget` | -10.60% (1/8) | +0.15% | 10.44% | 0.08 | 5.42 | 3,513 |
| `equal_weight` | Not verifiable | -7.87% | 14.04% | -0.89 | 1.21 | 468 |
| `cash` | 0.00% | 0.00% | 0.00% | 0.00 | 0.00 | 0 |

All 9 strategies, metric definitions and the holdout chart: [README](../README.en.md). Source: `sample_results/2026-10-16/` (`model_hash ad4a7a6`).

## Findings

- Only `dual_momentum` is positive in both rolling validation and the holdout (+1.00% / +0.20%), economically zero. `timing` (+21.79% / -7.90%) and `sector_rotation` (-4.38% / +1.23%) flip sign, indicating regime dependence.
- The holdout is dominated by one episode: momentum strategies peaked on 2026-06-25 (`sector_rotation` +26.2%) and gave it back in July; near-zero returns hide 20%–24% drawdowns.
- Fees are 2.7%–3.5% of initial capital for momentum strategies (7.9–11.2x turnover); costs are a binding constraint.
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

Code, methodology, and reproducible evidence: https://github.com/stevia7896/FuGuoCup-quantsys

Personal research only; not investment advice.
