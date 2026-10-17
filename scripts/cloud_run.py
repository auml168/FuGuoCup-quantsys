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
    args = ["run", "--slot", "auto", "--account", "config/account.local.json" if account else "config/account.json"]
    if os.environ.get("FQ_RESEARCH") == "true":
        args.append("--research")
    if os.environ.get("FQ_EVENT") == "schedule" or os.environ.get("FQ_SEND") == "true":
        args.append("--prepare-send")
    return main(args)


if __name__ == "__main__":
    sys.exit(run())
