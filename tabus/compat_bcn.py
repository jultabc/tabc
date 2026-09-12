#!/usr/bin/env python3
"""bcn — a compatibility alias for the tabc client. The real entry point is
`tabus.cli`, reached as the `tabc` command or `python3 -m tabus.cli`.

Kept during the rename rather than deleted outright, so anything still calling
the old name keeps working. Use `tabc`.

🔴 The execv happens only under the __main__ guard. Imported as a module — which
is how tests reach it — it must not replace the process.
When run directly it forwards its arguments to `tabus.cli` and does nothing
else. No logic is duplicated here.
"""

import os
import sys

if __name__ == "__main__":
    # 🔴 Re-exec the real entry point as a module. It used to point at a sibling
    #    .py file by path; inside a package there is no such file to name, and an
    #    installed copy has no predictable directory at all. `-m` finds it wherever
    #    the package lives.
    os.execv(sys.executable, [sys.executable, "-m", "tabus.cli"] + sys.argv[1:])
