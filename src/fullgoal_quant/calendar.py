from __future__ import annotations

from datetime import date, timedelta

from .common import GuardError, read_json


class Calendar:
    """Explicit official annual closures; weekends never become equity sessions."""

    def __init__(self, path="config/calendar.json"):
        self.cfg = read_json(path)
        self.start = date.fromisoformat(self.cfg["valid_from"])
        self.end = date.fromisoformat(self.cfg["valid_to"])
        self.closed = set()
        for start, end in self.cfg["closures"]:
            d, stop = date.fromisoformat(start), date.fromisoformat(end)
            while d <= stop:
                self.closed.add(d)
                d += timedelta(days=1)

    def is_session(self, day):
        if not self.start <= day <= self.end:
            raise GuardError("交易日历超出核验期限，请根据上交所公告更新 config/calendar.json")
        return day.weekday() < 5 and day not in self.closed

    def previous(self, day, n=1):
        for _ in range(n):
            day -= timedelta(days=1)
            while not self.is_session(day):
                day -= timedelta(days=1)
        return day

    def next(self, day, n=1):
        for _ in range(n):
            day += timedelta(days=1)
            while not self.is_session(day):
                day += timedelta(days=1)
        return day

    def lag(self, old, new):
        n = 0
        while old < new:
            old += timedelta(days=1)
            n += self.is_session(old)
        return n


def parse_official_year(text, year):
    """All seven statutory closure sections must be present; no inferred 2027 dates."""
    import re, html
    plain = html.unescape(re.sub(r'<[^>]+>', '', text))
    marker = f'{year}年休市安排'
    if marker not in plain:
        raise GuardError(f'{year}年官方休市安排尚未在指定来源发布或页面结构发生变化')
    plain=plain.split(marker,1)[1].split('相关公告',1)[0]
    closures=[]
    for holiday in ('元旦','春节','清明节','劳动节','端午节','中秋节','国庆节'):
        match=re.search(holiday+r'\s*[：:]\s*(.*?)休市',plain,re.S)
        if not match:
            raise GuardError('官方日历不完整，不能按普通工作日补齐')
        days=re.findall(r'(\d{1,2})月(\d{1,2})日',match[1])
        if not 1<=len(days)<=2:
            raise GuardError('官方日期范围无法明确解析')
        start=date(year,int(days[0][0]),int(days[0][1]))
        end=date(year,int(days[-1][0]),int(days[-1][1]))
        if not 0 <= (end-start).days <= 20:
            raise GuardError('官方假期范围异常')
        closures.append([str(start),str(end)])
    return closures


def extend_official(calendar, year, destination):
    from .data import get_text
    from .common import write_json, now_cn
    import copy, hashlib
    source='https://www.sse.com.cn/disclosure/dealinstruc/closed/'
    text=get_text(source)
    closures=parse_official_year(text,year)
    cfg=copy.deepcopy(calendar.cfg)
    cfg['closures']=[x for x in cfg['closures'] if not x[0].startswith(str(year))]+closures
    cfg['valid_to']=f'{year}-12-31'
    cfg['verified_on']=str(now_cn().date())
    cfg.setdefault('annual_sources',{})[str(year)]={'url':source,'sha256':hashlib.sha256(text.encode()).hexdigest()}
    write_json(destination,cfg)
    return Calendar(destination)
