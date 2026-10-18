"""Offline research migration only; never refreshes source fetched_at or live status."""
import argparse
import hashlib
from pathlib import Path

from fullgoal_quant.common import GuardError, load_config, now_cn, read_json, write_json
from fullgoal_quant.data import parse_nav


def rebuild(root, cfg):
    root = Path(root)
    snapshot = read_json(root / 'snapshot.json')
    if snapshot['mode'] != 'research':
        raise GuardError('离线重建仅供研究；正式报告必须重新采集公开接口')
    for code, meta in snapshot['funds'].items():
        source = meta['sources'][0]
        raw = Path(source['raw']).read_text(encoding='utf-8')
        if hashlib.sha256(raw.encode()).hexdigest() != source['sha256']:
            raise GuardError(f'{code} 原始响应校验失败')
        name, frame = parse_nav(raw, code)
        if name != meta['name']:
            raise GuardError('原始名称不匹配')
        # Do not extend an old snapshot with dates unavailable at its collection.
        frame = frame[frame.date <= meta['latest_nav_date']]
        target = root / 'nav' / (code + '.csv')
        frame.to_csv(target, index=False)
        meta['csv_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        meta['history_rows'] = len(frame)
        min_history = cfg['universe'].get('min_history', 252)
        min_score = cfg['universe'].get('min_score_history', 128)
        meta['research_ready'] = len(frame) >= min_history
        meta['analysis_ready'] = len(frame) >= min_score
        meta['quality_break_dates'] = frame.loc[frame.quality_break, 'date'].dt.strftime('%Y-%m-%d').tolist()
        meta.pop('discarded_prefix_rows', None)
        notes = [] if meta['analysis_ready'] else [f'历史不足{min_score}条，暂不评分']
        if meta['analysis_ready'] and not meta['research_ready']:
            notes.append(f'历史不足{min_history}条，可评分但无完整回测')
        meta['data_notes'] = notes
        meta['collection_status'] = 'ok' if meta['analysis_ready'] else 'insufficient_history'
        if meta['quality_break_dates']:
            meta['data_notes'].append('逐时点隔离收益断点；持仓跨断点停止计价，不回溯删除历史')
    snapshot['research_reprocessed_at'] = now_cn().isoformat()
    snapshot['quality_policy'] = 'causal_break_quarantine_v2'
    write_json(root / 'snapshot.json', snapshot)
    universe = read_json(root / 'universe.json')
    for f in universe['funds']:
        if f['code'] in snapshot['funds']:
            f.update(snapshot['funds'][f['code']])
    funds = universe['funds']
    analysis_ready_count = sum(1 for f in funds if f.get('analysis_ready'))
    research_ready_count = sum(1 for f in funds if f.get('research_ready'))
    snapshot['coverage'] = {
        'catalog': len(funds),
        'otc': sum(1 for f in funds if f.get('otc', True)),
        'not_otc': sum(1 for f in funds if not f.get('otc', True)),
        'model_supported': sum(1 for f in funds if f.get('otc', True) and f.get('model_supported', True)),
        'collected': len(snapshot['funds']),
        'analysis_ready': analysis_ready_count,
        'full_backtest_ready': research_ready_count,
        'new_fund_short_backtest': analysis_ready_count - research_ready_count,
        'insufficient_history': sum(1 for f in funds if f.get('collection_status') == 'insufficient_history'),
        'model_unsupported': sum(1 for f in funds if f.get('collection_status') == 'model_unsupported'),
        'collection_errors': sum(1 for f in funds if str(f.get('collection_status', '')).startswith('error')),
        'not_requested': sum(1 for f in funds if f.get('collection_status') == 'not_requested'),
    }
    write_json(root / 'snapshot.json', snapshot)
    write_json(root / 'universe.json', universe)
    return snapshot


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    args = parser.parse_args()
    result = rebuild(args.data, load_config())
    print(f"Offline research reprocessed: {len(result['funds'])} funds; original fetch times preserved.")
