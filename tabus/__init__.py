"""tabus — a message bus between sessions on one machine.

The bus itself lives in `tabus.bus`. This module re-exports it so that
`import tabus` followed by `tabus.bus_send(...)` keeps meaning what it always
meant: before the package layout, `tabus` *was* that module, and twenty-seven
call sites are written that way. Re-exporting keeps the public surface identical
while the files underneath get a shape a package can have.

  tabus.bus         the store and its operations
  tabus.cli         the `tabc` command line
  tabus.daemon      the local HTTP daemon
  tabus.doorbell    the notifier
  tabus.nodekey     ed25519 signing keys, one per node
  tabus.lang        the message catalogue
  tabus.paths       where this installation keeps its state

🔴 `from .bus import *` rather than a hand-listed set of names. The bus module
defines what is public; restating that list here would be a second copy of it,
and the two would drift the first time someone added a function to one and not
the other. `bus` carries no `__all__`, so this takes its module-level names that
do not begin with an underscore — the same rule a reader would apply by eye.
"""

from .bus import *  # noqa: F401,F403
from . import bus  # noqa: F401  — also reachable by its own name
from . import paths  # noqa: F401

# 🔴 Re-export copies references; it does not alias the module. Reading
#    `tabus.bus_send` and `tabus.bus.bus_send` gives the same function, but
#    *assigning* `tabus.bus_send = f` rebinds the name here only — callers inside
#    tabus.bus look their globals up in their own module and never see it. Code
#    that replaces a function (a test with a stub, a caller installing a policy)
#    must target `tabus.bus`. The read path being identical is what makes this
#    easy to get wrong, so it is stated rather than left to be discovered.
__version__ = bus.__version__ if hasattr(bus, "__version__") else "0"
