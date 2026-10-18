from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time
from pathlib import Path

import pandas as pd

from .backtest import compare
from .calendar import Calendar, extend_official
from .common import GuardError, TZ, competition_errors, load_config, now_cn, read_json
from .data import Collector, load_market
from .portfolio import import_account, validate_account
from .report import build_report, save_report
from .telegram import api, chat_ids, send_report
from .research import evaluate
from .universe import active_codes, analysis_codes, metadata_for
from .notifications import reserve, state_at, complete

SLOTS = {"morning": time(8, 30), "afternoon": time(14, 20), "evening": time(20, 30)}
CRONS = {"30 0 * * 1-5": "morning", "20 6 * * 1-5": "afternoon", "30 12 * * 1-5": "evening"}


def slot_guard(slot, at=None):
    at = (at or now_cn()).astimezone(TZ)
    if slot == "manual":
        return True
    target = datetime.combine(at.date(), SLOTS[slot], TZ)
    minutes = (at - target).total_seconds() / 60
    # Never deliver a delayed afternoon order plan after the platform cutoff.
    return 0 <= minutes <= (35 if slot == "afternoon" else 45)


def historical_status(path):
    if not path:
        return None
    f = pd.read_csv(path, dtype={"code": str})
    required = {"code", "date", "buy_open", "sell_open", "purchase_limit"}
    if not required <= set(f):
        raise GuardError("历史状态 CSV 缺少 code,date,buy_open,sell_open,purchase_limit；缺失不能当作不限额")
    f["date"] = pd.to_datetime(f["date"])
    for name in ("buy_open", "sell_open"):
        if not f[name].isin([0, 1, True, False]).all():
            raise GuardError("历史状态须为0或1，date表示当日决策前已知状态")
        f[name] = f[name].astype(bool)
    if f.duplicated(["code", "date"]).any():
        raise GuardError("历史状态有重复记录")
    return f


def make_parser():
    p = argparse.ArgumentParser(description="富国杯量化研究和决策支持，不自动下单")
    p.add_argument("--config", default="config/settings.json")
    p.add_argument("--data", default="data")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="发现富国基金候选池，不等于比赛白名单")
    collect = sub.add_parser("collect", help="下载真实净值和当前申赎状态")
    collect.add_argument("--competition", action="store_true")
    collect.add_argument("--limit", type=int)
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--competition", action="store_true")
    backtest.add_argument("--baseline-eight", action="store_true")
    backtest.add_argument("--start", default="2021-01-01")
    backtest.add_argument("--end")
    backtest.add_argument("--status-history")
    backtest.add_argument("--out", default="reports/backtest")
    research = sub.add_parser('research', help='固定方案的滚动样本外检验；不自动替换原模型')
    research.add_argument('--out', default='reports/research')
    research.add_argument('--status-history')
    for name in ("report", "run"):
        report = sub.add_parser(name, help="生成报告" if name == "report" else "交易日检查→采集→回测→报告→可选推送")
        report.add_argument("--research", action="store_true")
        report.add_argument("--account", default="config/account.example.json")
        report.add_argument("--selection", default="reports/research/comparison.json")
        report.add_argument("--slot", choices=["auto", "manual", *SLOTS], default="manual")
        report.add_argument("--send", action="store_true")
        report.add_argument("--prepare-send", action="store_true", help="仅生成持久化发送预约，不发送")
        report.add_argument("--force", action="store_true", help="非交易日也生成研究信号，仅用于手动测试")
    account = sub.add_parser("account", help="导入平台实际账户，保留原文件备份")
    account.add_argument("action", choices=["show", "validate", "import"])
    account.add_argument("--file")
    account.add_argument("--source")
    bot = sub.add_parser("telegram")
    bot.add_argument("action", choices=["check", "chat-id", "test"])
    sub.add_parser("validate", help="检查比赛配置是否具备正式启用条件")
    calendar_check=sub.add_parser('calendar-check',help='只从上交所官方完整年度安排更新日历；未知年份不猜测')
    calendar_check.add_argument('--year',type=int,default=2027)
    doctor = sub.add_parser("doctor", help="真实请求公共接口，保存可审计证据")
    doctor.add_argument("--limit", type=int, default=12)
    return p


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        if args.command == "telegram":
            if args.action == "check":
                bot = api("getMe", {})
                print("连接成功，机器人：@" + bot.get("username", "unknown"))
            elif args.action == "chat-id":
                print("发现 Chat ID：", ", ".join(chat_ids()) or "无；请先给机器人发送 /start，且确保未配置 webhook")
            else:
                send_report("富国杯系统连接测试成功。仅发送分析建议，不自动下单。")
                print("测试消息已发送")
            return 0
        if args.command == "account":
            if args.action == "import":
                if not args.source:
                    raise GuardError("导入账户需要 --source 文件路径")
                account = import_account(args.source, args.file or "config/account.local.json")
            else:
                # CI and first-time users must not need a real account file.
                account = validate_account(read_json(args.file or "config/account.example.json"))
            if os.environ.get("GITHUB_ACTIONS") == "true":
                # Actions logs are public for public repositories. Print no
                # cash or position counts even if a real account was imported.
                print(f"账户校验通过：核对日 {account['as_of']}（金额与持仓不在公开日志显示）")
            else:
                print(f"账户校验通过：核对日 {account['as_of']}，可用现金 {account['cash']:.2f}，{len(account['lots'])}笔持仓，{len(account.get('pending', []))}笔在途")
            return 0
        cfg = load_config(args.config)
        if args.command=='calendar-check':
            base=Calendar()
            try:
                calendar=extend_official(base,args.year,Path(args.data)/'calendar.json')
                print(f'官方日历核验通过，覆盖至 {calendar.end}')
            except GuardError:
                if args.year <= now_cn().year:
                    raise
                print(f'{args.year}年官方完整日历尚未核验，保留未来日期拦截；当前覆盖至{base.end}')
            return 0
        if args.command == "validate":
            errors = competition_errors(cfg)
            if not cfg["rules"]["verified"]:
                errors.append("rules.verified 未确认")
            for code in cfg["competition"]["allowed_codes"]:
                rule = cfg.get("fund_rules", {}).get(code, {})
                if not rule.get("verified") or not rule.get("source"):
                    errors.append(f"{code} 申赎规则/费率缺少核验来源")
            if errors:
                raise GuardError("；".join(errors))
            print("比赛静态配置通过；正式报告仍会检查实际账户、数据与模型风险门槛")
            return 0
        collector = Collector(args.data)
        if args.command == "discover":
            result = collector.discover()
            print(f"发现 {len(result['funds'])} 条目录候选；管理人须逐只核验；见 {args.data}/catalog.json（含不支持品种标记）")
            return 0
        if args.command == "doctor":
            result = Collector(Path(args.data)/'smoke').collect(cfg, limit=args.limit)
            if result["errors"] or not result["funds"]:
                raise GuardError("公共接口抽样存在异常，请查阅 snapshot.json，不宣称全目录已通过")
            print(f"公共接口抽样实测通过，{len(result['funds'])} 只基金，原始响应和 SHA256 证据见 {args.data}/smoke/snapshot.json")
            return 0
        if args.command == "collect":
            if args.competition and competition_errors(cfg):
                raise GuardError("比赛配置未核验；默认 collect 仅采集研究池")
            result = collector.collect(cfg, args.competition, limit=args.limit)
            print(f"采集成功：{len(result['funds'])} 只基金")
            return 0
        if args.command in ('backtest','research'):
            snapshot=read_json(Path(args.data)/'snapshot.json')
            codes = (analysis_codes(cfg, snapshot) if args.command == 'research'
                     else active_codes(cfg, snapshot, competition=getattr(args,'competition',False)))
            if getattr(args,'baseline_eight',False):
                codes=[f['code'] for f in cfg['research_funds'] if f['code'] in snapshot['funds']]
            if not codes:
                raise GuardError("比赛白名单为空")
            _, frames = load_market(args.data, codes)
            metadata=metadata_for(cfg,snapshot)
            if args.command=='research':
                result=evaluate(frames,cfg,metadata,args.out,status_history=historical_status(args.status_history))
                print('滚动验证完成，保留模型：'+result['selected']+'；新模型未自动晋级')
                return 0
            result = compare(frames, cfg, metadata, args.out,
                             start=args.start, end=args.end, status_history=historical_status(args.status_history))
            print("验证集冻结选择：" + result["selected"] + "；结果见 " + args.out + "/comparison.md")
            return 0
        slot = args.slot
        if slot == "auto":
            schedule = os.environ.get("FQ_SCHEDULE", "")
            if (schedule and schedule not in CRONS) or (os.environ.get("FQ_EVENT") == "schedule" and not schedule):
                raise GuardError("未知调度表达式，停止发送")
            slot = CRONS.get(schedule, "manual")
        cal=Calendar()
        if now_cn().date().year>cal.end.year:
            cal=extend_official(cal,now_cn().date().year,Path(args.data)/'calendar.json')
        if args.command == "run":
            if not cal.is_session(now_cn().date()) and not args.force:
                print("非A股交易日，跳过采集与推送")
                return 0
            if not slot_guard(slot):
                print("工作流延迟超出有效时段，跳过过时报告；请在 Actions 查看延迟")
                return 0
        if args.command=='run' and now_cn().month>=10 and cfg['competition']['end_date'] and int(cfg['competition']['end_date'][:4])>cal.end.year:
            try:
                cal=extend_official(cal,cal.end.year+1,Path(args.data)/'calendar.json')
            except GuardError:
                print('下一年度官方日历尚未完整核验，保留超期拦截。')
        research = args.research or (args.command == "run" and bool(competition_errors(cfg)))
        if args.command == "run":
            collector.collect(cfg, competition=not research)
        current=read_json(Path(args.data)/"snapshot.json")
        codes = active_codes(cfg, current, competition=True) if not research else analysis_codes(cfg, current)
        snapshot, frames = load_market(args.data, codes)
        metadata=metadata_for(cfg,snapshot)
        if args.command == "run":
            selection_path = Path(args.selection)
            evaluate(frames, cfg, metadata, selection_path.parent)
        account=read_json(args.account)
        universe_path = Path(args.data)/'universe.json'
        report = build_report(cfg, snapshot, frames, read_json(args.selection), account, research=research, slot=slot, calendar=cal,
                              delivery_state=state_at() if Path('runtime/state-restored').exists() else None,
                              universe=read_json(universe_path) if universe_path.exists() else None)
        save_report(report)
        if os.environ.get('GITHUB_ACTIONS')=='true':
            print('报告已生成；账户、仓位和金额不写入公开运行日志。')
        else:
            print(report['text'])
        if args.prepare_send:
            if not slot_guard(slot):
                raise GuardError('报告处理后已过时，禁止预约发送')
            owner=os.environ.get('GITHUB_RUN_ID','local')
            if reserve(report,account,owner=owner):
                from .common import write_json
                write_json('runtime/outbox.json',report)
                print('发送预约已生成，须先上传持久化预约再发送。')
            else:
                print('本时段已预约/发送，跳过重复报告。')
        if args.send:
            if os.environ.get('GITHUB_ACTIONS')=='true':
                raise GuardError('云端必须通过持久化预约工作流发送，不能直接 --send')
            if not slot_guard(slot):
                raise GuardError("处理期间已超出发送窗口，报告已保存但未推送")
            if reserve(report,account):
                if report.get("telegram_text"):
                    send_report(report["telegram_text"], parse_mode="HTML")
                else:
                    send_report(report["text"])
                complete(report["report_id"], report=report)
                print("Telegram 推送完成")
            else:
                print("本时段已处理，跳过重复发送")
        return 0
    except (GuardError, FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        # No traceback: credentials may occur in HTTP objects. Data errors remain actionable.
        if isinstance(exc, GuardError):
            print("停止：" + str(exc), file=sys.stderr)
        elif isinstance(exc, FileNotFoundError):
            print("停止：文件缺失，请先采集数据/完成回测并检查路径", file=sys.stderr)
        else:
            print("停止：配置或数据格式异常，请运行测试并检查 JSON/CSV 字段", file=sys.stderr)
        if args.command == "run" and getattr(args, "send", False):
            try:
                send_report("富国杯分析任务失败，本时段不提供交易建议。请在 GitHub Actions 检查数据、配置或账户状态；勿沿用旧操作金额。")
            except GuardError:
                print("故障通知未能发送，请检查 Telegram 配置", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
