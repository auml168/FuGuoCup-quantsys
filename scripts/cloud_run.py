"""Entrypoint with no secrets in shell arguments; no eval or shell interpolation."""
import json
import os
import sys

from fullgoal_quant.cli import main
from fullgoal_quant.common import write_json
from fullgoal_quant.portfolio import validate_account


def run():
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
    return main(args)


if __name__ == "__main__":
    sys.exit(run())
