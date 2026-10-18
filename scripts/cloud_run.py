"""Entrypoint with no secrets in shell arguments; no eval or shell interpolation."""
import json
import os
import sys

from fullgoal_quant.cli import main
from fullgoal_quant.common import write_json
from fullgoal_quant.portfolio import validate_account


AFTERNOON_CRON = "20 6 * * 1-5"


def run():
    # Only the 14:20 Beijing run is the pre-settlement buy signal. The other
    # scheduled triggers exit early instead of re-collecting the universe.
    if os.environ.get("FQ_EVENT") == "schedule" and os.environ.get("FQ_SCHEDULE") != AFTERNOON_CRON:
        print("非14:20发送时段；跳过采集与推送", file=sys.stderr)
        return 0
    account = os.environ.get("ACCOUNT_JSON", "")
    if account:
        try:
            parsed = validate_account(json.loads(account))
        except (ValueError, KeyError, TypeError):
            print("ACCOUNT_JSON 格式错误，内容已隐藏", file=sys.stderr)
            return 2
        write_json("config/account.local.json", parsed)
    if not account:
        # Never silently use a mutable real-account file in the cloud. The
        # committed example is an explicit empty template; formal gates still
        # reject it, and research reports remain clearly labelled.
        print("ACCOUNT_JSON 未配置；使用示例空仓，仅用于研究预览", file=sys.stderr)
    args = ["run", "--slot", "auto", "--account", "config/account.local.json" if account else "config/account.example.json"]
    if os.environ.get("FQ_RESEARCH") == "true":
        args.append("--research")
    if os.environ.get("FQ_EVENT") == "schedule" or os.environ.get("FQ_SEND") == "true":
        args.append("--prepare-send")
    # A manual send=true dispatch is an explicit test; allow it on a weekend so
    # the operator can verify the Telegram signal format. Scheduled runs still
    # require an A-share session.
    if os.environ.get("FQ_FORCE") == "true" or (
            os.environ.get("FQ_EVENT") == "workflow_dispatch" and os.environ.get("FQ_SEND") == "true"):
        args.append("--force")
    # Manual send=true dispatch is an explicit test. Give it a unique report id
    # so the daily deduplication guard does not suppress it, and mark it so it
    # never becomes the next day's assumed-executed portfolio.
    if os.environ.get("FQ_EVENT") == "workflow_dispatch" and os.environ.get("FQ_SEND") == "true":
        args.append("--test-send")
    return main(args)


if __name__ == "__main__":
    sys.exit(run())
