#!/usr/bin/env python3
"""Print a read-only snapshot for reviewing authentication configuration.
"""
import os
import sys
import json
import glob
import subprocess


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    # Scan the repository this script lives in by default, or a path given as argv[1].
    scan_root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(root)

    # 1) Node keys replace the removed shared bus token. Signatures do not send
    #    private keys. Keys live under TABC_HOME (default ~/.tabc), not scripts/.
    home = os.environ.get("TABC_HOME") or os.path.expanduser("~/.tabc")
    secret_files = sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(home, ".node_key.*"))
    )
    for f in [".db_ro_password", ".db_readonly_password"]:
        p = os.path.join(root, f)
        if os.path.exists(p):
            secret_files.append(f)

    # 2) Scan tracked repository files for literal database credentials.
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=scan_root,
            capture_output=True, text=True, timeout=10
        ).stdout.splitlines()
    except Exception:
        tracked = []

    import re
    cred_pattern = re.compile(
        r"""(?:password|passwd|token|secret|api_key)\s*[=:]\s*["'][^"']{8,}["']""",
        re.IGNORECASE,
    )
    hits = []
    for f in tracked:
        fp = os.path.join(scan_root, f)
        if not os.path.exists(fp):
            continue
        try:
            content = open(fp, encoding="utf-8", errors="ignore").read()
            for m in cred_pattern.finditer(content):
                hits.append({"file": f, "line": content[:m.start()].count("\n") + 1})
        except Exception:
            pass

    # 3) Check for environment files.
    env_files = []
    for d in [scan_root, root]:
        for name in [".env", ".env.local", ".db_password"]:
            p_ = os.path.join(d, name)
            if os.path.exists(p_):
                env_files.append(p_)

    result = {
        "tabus_secret_files": secret_files,
        "tracked_cred_literal_hits": len(hits),
        "hits_by_dir": {},
        "env_files": env_files,
        "hits_detail": hits[:20],
    }
    for h in hits:
        d = h["file"].split("/")[0] if "/" in h["file"] else "root"
        result["hits_by_dir"][d] = result["hits_by_dir"].get(d, 0) + 1

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
