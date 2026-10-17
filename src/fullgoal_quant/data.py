from __future__ import annotations

import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .common import GuardError, TZ, now_cn, read_json, write_json
from .universe import classify

BASE = "https://fund.eastmoney.com"
COMPANY = BASE + "/company/80000221.html"


def get_text(url, attempts=3):
    """No eval/JS execution. Bounded retries. Raw source is archived separately."""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; FullgoalQuant/0.1)",
                "Referer": BASE + "/",
            })
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read().decode("utf-8-sig")
        except (urllib.error.URLError, TimeoutError, UnicodeError):
            if attempt == attempts - 1:
                raise GuardError("公共基金数据接口请求失败；请检查网络或稍后重试") from None
            time.sleep(1 + attempt)


def js_json(text, name):
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*", text)
    if not match:
        raise GuardError(f"接口缺少 {name}")
    try:
        return json.JSONDecoder().raw_decode(text[match.end():].lstrip())[0]
    except (ValueError, TypeError):
        raise GuardError(f"接口字段 {name} 格式变更") from None


def parse_nav(text, code):
    if js_json(text, "fS_code") != code or js_json(text, "ishb"):
        raise GuardError("基金代码不符或货币基金暂不支持")
    rows = js_json(text, "Data_netWorthTrend")
    if len(rows) < 2:
        raise GuardError("净值样本不足")
    frame = pd.DataFrame(rows)
    dates = pd.to_datetime(frame["x"], unit="ms", utc=True).dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize()
    nav = pd.to_numeric(frame["y"], errors="raise")
    returns = pd.to_numeric(frame["equityReturn"], errors="raise") / 100
    if dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise GuardError("净值日期重复或顺序异常")
    if not np.isfinite(nav).all() or (nav <= 0).any() or not np.isfinite(returns).all() or (returns <= -1).any():
        raise GuardError("净值/收益率存在无效数值")
    events = frame.get("unitMoney", pd.Series("", index=frame.index)).fillna('').astype(str)
    unexplained = ((nav.pct_change() - returns).abs() > .005) & events.eq('')
    # Keep every historical prefix: a future quality event must never delete past
    # eligibility. Unknown interval returns are marked, not reconstructed. Signals
    # quarantine the observed segment and accounting rejects held crossings.
    unexplained.iloc[0] = False
    # Vendor equityReturn includes dividend adjustments. Do not pct_change cumulative NAV.
    returns.iloc[0] = 0.0
    result = pd.DataFrame({"date": dates, "unit_nav": nav, "daily_return": returns,
                           "tri": (1 + returns).cumprod(),
                           "event": frame.get("unitMoney", pd.Series("", index=frame.index)),
                           "quality_break": unexplained})
    result.loc[unexplained, 'tri'] = np.nan
    return js_json(text, "fS_name"), result


def parse_status_page(text):
    match = re.search(r'交易状态：(.*?)</div>', text, re.S)
    if not match:
        raise GuardError("无法识别申赎状态，禁止猜测")
    plain = re.sub(r"<[^>]+>", "", match[1]).replace("&nbsp;", " ")
    cap = re.search(r"购买上限\s*([\d.]+)\s*(万|亿)?元", plain)
    limit = None
    if cap:
        limit = float(cap[1]) * {None: 1, "万": 10000, "亿": 100000000}[cap[2]]
    buy = "开放申购" in plain or ("限大额" in plain and limit is not None)
    return {"buy_open": buy, "sell_open": "开放赎回" in plain,
            "purchase_limit": limit, "status_text": plain.strip()}


def is_otc_candidate(name, category):
    return classify(name, category)["otc_candidate"]


def parse_profile(text):
    manager = re.search(r'管\s*理\s*人</span>\s*[：:]\s*<a[^>]+company/(\d+)\.html', text)
    if not manager or manager[1] != '80000221':
        raise GuardError("基金详情管理人不是已核验的富国基金")
    block = re.search(r'class="infoOfFund"(.*?)</table>', text, re.S)
    plain = html.unescape(re.sub(r'<[^>]+>', ' ', block[1] if block else ''))
    risk = re.search(r'(中高风险|中低风险|高风险|中风险|低风险)', plain)
    return {"manager_verified": True, "risk_label": risk[1] if risk else "未知（不得按类别猜测）",
            "risk_source": "sales_page_not_official_suitability_grade"}


class Collector:
    def __init__(self, root="data"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def archive(self, label, url, text):
        digest = hashlib.sha256(text.encode()).hexdigest()
        target = self.root / "raw" / (label + "-" + digest[:16] + ".txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return {"url": url, "sha256": digest, "raw": str(target), "bytes": len(text.encode()),
                "fetched_at": datetime.now(timezone.utc).isoformat()}

    def discover(self):
        company = get_text(COMPANY)
        if "富国基金管理有限公司" not in company:
            raise GuardError("基金公司页面身份核验失败")
        company_codes = set(re.findall(r"fund\.eastmoney\.com/(\d{6})\.html", company))
        if len(company_codes) < 50:
            raise GuardError("公司页面结构可能变更")
        url = BASE + "/js/fundcode_search.js"
        text = get_text(url)
        all_funds = js_json(text, "r")
        funds = [{"code": r[0], "name": r[2], "category": r[3], **classify(r[2], r[3])}
                 for r in all_funds if r[0] in company_codes]
        snapshot = {"fetched_at": now_cn().isoformat(), "funds": funds,
                    "sources": [self.archive("company", COMPANY, company), self.archive("catalog", url, text)]}
        write_json(self.root / "catalog.json", snapshot)
        return snapshot

    def collect(self, cfg, competition=False, limit=None):
        catalog = self.discover()
        index = {f["code"]: f for f in catalog["funds"]}
        codes = cfg["competition"]["allowed_codes"] if competition else [f["code"] for f in catalog["funds"] if f["otc_candidate"]]
        planned = len(codes)
        if limit:
            if competition:
                raise GuardError("比赛模式不可截断白名单")
            # Stratify by type/sector, not recent returns; bounded smoke runs are labeled incomplete.
            buckets = {}
            for c in codes:
                f = index[c]
                buckets.setdefault((f['asset_type'], f['sector']), []).append(c)
            codes = ['100038'] if '100038' in codes else []
            for values in buckets.values():
                if '100038' in values:
                    values.remove('100038')
            while any(buckets.values()) and len(codes) < limit:
                for key in sorted(buckets):
                    if buckets[key] and len(codes) < limit:
                        codes.append(buckets[key].pop(0))
        if not codes:
            raise GuardError("没有配置基金代码")
        metadata, errors = {}, {}
        def fetch_one(code):
            try:
                if code not in index or not index[code]["otc_candidate"]:
                    raise GuardError("非已核验富国场外候选基金，或当前版本不支持该品种")
                url = f"{BASE}/pingzhongdata/{code}.js?v={int(time.time())}"
                text = get_text(url)
                nav_source = self.archive(code, url, text)
                name, frame = parse_nav(text, code)
                if name != index[code]["name"]:
                    raise GuardError("基金名称在两个数据源中不一致")
                frame = frame[frame.date <= pd.Timestamp(now_cn().date())]
                status_url = f"{BASE}/{code}.html"
                status_text = get_text(status_url)
                meta = {**index[code], **parse_profile(status_text), **parse_status_page(status_text),
                        "fetched_at": now_cn().isoformat(), "latest_nav_date": str(frame.date.iloc[-1].date()),
                        "sources": [nav_source, self.archive(code + "-status", status_url, status_text)]}
                target = self.root / "nav" / (code + ".csv")
                target.parent.mkdir(parents=True, exist_ok=True)
                frame.to_csv(target, index=False)
                meta['csv_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
                meta['history_rows'] = len(frame)
                required = cfg.get('universe', {}).get('min_history', 252)
                meta['research_ready'] = len(frame) >= required
                meta['data_notes'] = [] if meta['research_ready'] else [f'历史不足{required}条']
                meta['quality_break_dates'] = frame.loc[frame.quality_break, 'date'].dt.strftime('%Y-%m-%d').tolist()
                if meta['quality_break_dates']:
                    meta['data_notes'].append('逐时点隔离无法解释收益断点；持仓跨断点回测无效，不回溯删除历史')
                time.sleep(0.25)
                return code, meta, None
            except (GuardError, KeyError, ValueError, IndexError) as exc:
                return code, None, str(exc)
        with ThreadPoolExecutor(max_workers=cfg.get('universe', {}).get('workers', 4)) as pool:
            futures = [pool.submit(fetch_one, c) for c in codes]
            for future in as_completed(futures):
                code, meta, error = future.result()
                if error:
                    errors[code] = error
                else:
                    metadata[code] = meta
        metadata = dict(sorted(metadata.items()))
        for fund in catalog['funds']:
            c = fund['code']
            fund.update(metadata.get(c, {}))
            fund['collection_status'] = 'ok' if c in metadata else ('error: ' + errors[c] if c in errors else 'unsupported' if not fund['otc_candidate'] else 'not_requested')
            fund.setdefault('risk_label', '未知（未取得产品详情）')
            fund.setdefault('history_rows', None)
        write_json(self.root / 'universe.json', catalog)
        snapshot = {"fetched_at": now_cn().isoformat(), "mode": "competition" if competition else "research",
                    "funds": metadata, "errors": errors, "planned_supported": planned,
                    "requested": len(codes), "universe_complete": not limit and not errors,
                    "catalog_count": len(catalog['funds'])}
        write_json(self.root / "snapshot.json", snapshot)
        if errors and competition:
            raise GuardError(f"采集不完整：{errors}；本次快照已保存，禁止使用旧数据补齐")
        return snapshot


def load_market(root="data", codes=None):
    root = Path(root)
    snapshot = read_json(root / "snapshot.json")
    codes = list(snapshot["funds"]) if codes is None else codes
    if not codes or set(codes) - set(snapshot["funds"]):
        raise GuardError("采集快照不完整，请重新 collect")
    frames = {}
    for code in codes:
        path = root / 'nav' / (code + '.csv')
        expected = snapshot['funds'][code].get('csv_sha256')
        if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise GuardError(f'{code} 净值文件校验失败，请重新采集')
        f = pd.read_csv(path, parse_dates=['date']).set_index('date')
        breaks = f.get('quality_break', pd.Series(False, index=f.index))
        if not breaks.isin([True, False]).all():
            raise GuardError(f'{code} 质量标记无效')
        valid_tri = np.isfinite(f.tri) | (f.tri.isna() & breaks)
        if f.index.duplicated().any() or not f.index.is_monotonic_increasing or not np.isfinite(f[['unit_nav','daily_return']]).all().all() or not valid_tri.all() or (f[['unit_nav','tri']] <= 0).any().any() or (f.daily_return <= -1).any():
            raise GuardError(f'{code} 本地净值数据无效')
        frames[code] = f
    return snapshot, frames
