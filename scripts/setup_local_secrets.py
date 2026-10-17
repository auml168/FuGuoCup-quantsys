"""Interactive local bootstrap. Never prints the Token or puts it in shell history."""
import getpass
import os
import re
from pathlib import Path


def main():
    token = getpass.getpass("Telegram Bot Token（输入隐藏）：").strip()
    chat = getpass.getpass("Chat ID（输入隐藏；可留空稍后填写）：").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Token 格式无效；请从 BotFather 复制，不要输入命令或其他文本")
    if chat and not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", chat):
        raise SystemExit("Chat ID 格式无效")
    destination = Path(".env")
    # Do not overwrite a secret without an explicit user action.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("TELEGRAM_BOT_TOKEN=" + token + "\nTELEGRAM_CHAT_ID=" + chat + "\n")
    print("已保存到权限 600 的 .env（已被 Git 忽略）。应用不会自动加载它。")
    print("在自己的终端中执行：set -a; source .env; set +a")


if __name__ == "__main__":
    main()
