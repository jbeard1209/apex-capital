"""Merge the proposed autoMode/permissions block (docs/audit/2026-11-11-permissions-setup.md)
into ~/.claude/settings.json. Backs up the old file first. Usage: python3 docs/audit/apply_permissions.py [target]"""
import json, re, shutil, sys, pathlib
doc = pathlib.Path(__file__).with_name("2026-11-11-permissions-setup.md").read_text()
block = json.loads(re.search(r"```json\n(.*?)\n```", doc, re.S).group(1))
target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / ".claude/settings.json")
cur = json.loads(target.read_text()) if target.exists() else {}
if target.exists():
    shutil.copy2(target, str(target) + ".bak")
cur["autoMode"] = block["autoMode"]
perms = cur.setdefault("permissions", {})
for k in ("allow", "deny", "ask"):
    perms[k] = list(dict.fromkeys(perms.get(k, []) + block["permissions"][k]))
target.write_text(json.dumps(cur, indent=2) + "\n")
print(f"Updated {target} (backup at {target}.bak). Start a new Claude session to pick it up.")
