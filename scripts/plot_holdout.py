"""Render the holdout figure for the README from a frozen research run.

Usage: python scripts/plot_holdout.py [sample_results/2026-10-16] [docs/figures]
Needs matplotlib (not a runtime dependency): pip install matplotlib
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

RUN = Path(sys.argv[1] if len(sys.argv) > 1 else 'sample_results/2026-10-16')
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else 'docs/figures')
INITIAL = 200_000
# Fixed categorical order (blue, orange, aqua); benchmark in neutral muted gray.
SERIES = [('sector_rotation', 0), ('dual_momentum', 1), ('timing', 2), ('equal_weight', None)]
THEMES = {
    'light': {'surface': '#fcfcfb', 'text': '#0b0b0b', 'secondary': '#52514e', 'muted': '#898781',
              'grid': '#e1e0d9', 'series': ['#2a78d6', '#eb6834', '#1baf7a']},
    'dark': {'surface': '#1a1a19', 'text': '#f0efec', 'secondary': '#c3c2b7', 'muted': '#898781',
             'grid': '#2c2c2a', 'series': ['#3987e5', '#d95926', '#199e70']},
}


def load():
    curves = {}
    for name, _ in SERIES:
        f = pd.read_csv(RUN / f'{name}-test-equity.csv', parse_dates=['date']).set_index('date')
        curves[name] = f['assets'] / INITIAL - 1
    return curves


def draw(curves, mode):
    t = THEMES[mode]
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True, dpi=200,
                                      gridspec_kw={'height_ratios': [3, 2], 'hspace': 0.12})
    fig.patch.set_facecolor(t['surface'])
    for ax in (top, bottom):
        ax.set_facecolor(t['surface'])
        ax.grid(axis='y', color=t['grid'], linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ('top', 'right', 'left'):
            ax.spines[side].set_visible(False)
        ax.spines['bottom'].set_color(t['grid'])
        ax.tick_params(colors=t['secondary'], labelsize=8, length=0)
        ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
        ax.axhline(0, color=t['muted'], linewidth=0.8)
    ends = []
    for name, slot in SERIES:
        color = t['muted'] if slot is None else t['series'][slot]
        width = 2.4 if name == 'sector_rotation' else 1.6
        ret = curves[name]
        drawdown = (1 + ret) / (1 + ret).cummax() - 1
        top.plot(ret.index, ret, color=color, linewidth=width, solid_capstyle='round')
        bottom.plot(drawdown.index, drawdown, color=color, linewidth=width, solid_capstyle='round')
        top.scatter(ret.index[-1], ret.iloc[-1], s=22, color=color, zorder=3,
                    edgecolors=t['surface'], linewidths=1.5)
        ends.append([ret.iloc[-1], ret.iloc[-1], f'{name}  {ret.iloc[-1]:+.1%}', color])
    # Direct end labels in text ink with a colored line-key; nudge apart so
    # close finishes do not overlap, with a hairline back to the true end.
    ends.sort()
    gap = (top.get_ylim()[1] - top.get_ylim()[0]) * 0.065
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + gap)
    last = curves['sector_rotation'].index[-1]
    x0, x1, x2 = (last + pd.Timedelta(days=d) for d in (6, 9, 15))
    for y, true_y, label, color in ends:
        if abs(y - true_y) > 1e-9:
            top.plot([last, x0], [true_y, y], color=t['muted'], linewidth=0.6, clip_on=False)
        top.plot([x0, x1 + pd.Timedelta(days=2)], [y, y], color=color, linewidth=2.4,
                 solid_capstyle='round', clip_on=False)
        top.text(x2, y, label, color=t['text'], fontsize=8, va='center')
    top.set_xlim(curves['sector_rotation'].index[0] - pd.Timedelta(days=3), last + pd.Timedelta(days=3))
    top.set_title('Holdout 2026-04-10 → 2026-10-16, net of fees (initial ¥200,000)',
                  loc='left', color=t['text'], fontsize=10, pad=10)
    top.set_ylabel('Cumulative return', color=t['secondary'], fontsize=8)
    bottom.set_ylabel('Drawdown', color=t['secondary'], fontsize=8)
    bottom.xaxis.set_major_formatter(matplotlib.dates.DateFormatter('%Y-%m'))
    fig.subplots_adjust(left=0.08, right=0.78, top=0.92, bottom=0.07)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f'holdout-{mode}.png'
    fig.savefig(path, facecolor=t['surface'])
    plt.close(fig)
    return path


if __name__ == '__main__':
    data = load()
    for mode in THEMES:
        print(draw(data, mode))
