#!/usr/bin/env python3
"""busd — a compatibility alias for the tabd daemon. The real entry point is tabd.py.

Kept during the rename rather than deleted outright. Use `tabd`.

🔴 The execv happens only under the __main__ guard; importing must not replace
the process.
When run directly it forwards its arguments to tabd.py. No logic is duplicated
here.
"""

import os
import sys

if __name__ == "__main__":
    # 🔴 Re-exec the real entry point as a module. It used to point at a sibling
    #    .py file by path; inside a package there is no such file to name, and an
    #    installed copy has no predictable directory at all. `-m` finds it wherever
    #    the package lives.
    os.execv(sys.executable, [sys.executable, "-m", "tabus.daemon"] + sys.argv[1:])
