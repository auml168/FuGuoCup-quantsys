"""Entrypoint with no secrets in shell arguments; no eval or shell interpolation."""
import json
import os
import sys
from pathlib import Path

from fullgoal_quant.cli import CRONS, main
from fullgoal_quant.common import write_json
from fullgoal_quant.portfolio import validate_account
from fullgoal_quant.runstatus import production, record


def run():
    scheduled=production()
    try:
        # A reused workspace must never send an earlier outbox.
        Path('runtime/outbox.json').unlink(missing_ok=True)
        if scheduled:
            record('scheduled')
            if os.environ.get('FQ_EVENT') == 'schedule' and CRONS.get(os.environ.get('FQ_SCHEDULE')) != 'afternoon':
                record('failed',detail='调度表达式未映射至 afternoon')
                return 2
            if not Path('runtime/state-restored').exists():
                record('failed',detail='去重状态无法核验')
                return 2
        account = os.environ.get('ACCOUNT_JSON', '')
        if account:
            try:
                parsed = validate_account(json.loads(account))
            except (ValueError, KeyError, TypeError):
                record('failed',detail='ACCOUNT_JSON 格式错误，内容已隐藏')
                return 2
            write_json('config/account.local.json', parsed)
        else:
            print('ACCOUNT_JSON 未配置；使用示例空仓，仅用于研究预览', file=sys.stderr)
        args = ['run', '--slot', 'auto', '--account', 'config/account.local.json' if account else 'config/account.example.json']
        if scheduled:
            args += ['--prepare-send', '--wait-for-slot', '40']
        else:
            record('preview')
            if os.environ.get('FQ_RESEARCH') == 'true':
                args.append('--research')
            if os.environ.get('FQ_EVENT') == 'workflow_dispatch' and os.environ.get('FQ_MODE') == 'test-send':
                args += ['--prepare-send', '--force', '--test-send']
            elif os.environ.get('FQ_FORCE') == 'true':
                args.append('--force')
        result=main(args)
        if result and not scheduled:
            record('failed',detail='手动报告任务失败，请检查运行日志')
        return result
    except Exception:
        record('failed',detail='报告任务异常，详细内容已隐藏')
        return 2


if __name__ == '__main__':
    sys.exit(run())
