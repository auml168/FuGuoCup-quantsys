"""Fail without printing matched credential values; scans only tracked project files."""
import re,subprocess
from pathlib import Path

patterns=[re.compile(r'\b\d{7,12}:[A-Za-z0-9_-]{30,}\b'),re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b')]
files=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
bad=[]
for name in files:
    if not name:continue
    p=Path(name)
    if name=='.env' or name.startswith('runtime/'):
        bad.append(name);continue
    if p.is_file():
        text=p.read_text(errors='ignore')
        if any(pattern.search(text) for pattern in patterns):bad.append(name)
if bad:
    raise SystemExit('Potential credential/runtime file detected; values hidden. Files: '+', '.join(bad))
print('Tracked project credential scan passed; no values printed.')
