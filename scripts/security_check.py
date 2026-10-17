"""Fail without printing matched credential values; scans only tracked project files."""
import json
import re
import subprocess
from pathlib import Path

patterns = [
    re.compile(r'\b\d{7,12}:[A-Za-z0-9_-]{30,}\b'),          # Telegram bot token
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),           # classic GitHub token
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),         # fine-grained GitHub PAT
]
files = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
bad = []
for name in files:
    if not name:
        continue
    p = Path(name)
    # Files that must never be tracked, even if force-added.
    if (name == '.env' or (name.startswith('.env.') and name != '.env.example')
            or name.startswith(('runtime/', 'data/', 'reports/'))
            or name in ('config/account.json', 'config/account.local.json')
            or (name.startswith('config/account') and '.backup' in name)):
        bad.append(name)
        continue
    if p.is_file():
        text = p.read_text(errors='ignore')
        if any(pattern.search(text) for pattern in patterns):
            bad.append(name)
    # The only tracked account file may be the empty template.
    if name.startswith('config/account') and name.endswith('.json') and name != 'config/account.example.json':
        bad.append(name)
    if name == 'config/account.example.json' and p.is_file():
        try:
            account = json.loads(p.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            bad.append(name)
        else:
            if account.get('lots') or account.get('pending') or account.get('reconciled_at'):
                bad.append(name)
if bad:
    raise SystemExit('Potential credential/account/runtime file detected; values hidden. Files: ' + ', '.join(sorted(set(bad))))
print('Tracked project credential/account scan passed; no values printed.')
