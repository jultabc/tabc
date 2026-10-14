"""Deliver notifications to an explicit Codex thread, without terminal input.

This module does not register routes or mark messages read. The receipt journal
records delivery attempts, not messages. An uncertain attempt is never retried
automatically: the queue command has no caller-supplied idempotency key.

Notification text is passed in argv and can be visible to local process tools.
Callers must pass notification metadata only, never message bodies or secrets.
Receipt parsing requires exactly one success line on stdout. Additional stdout
is treated as UNKNOWN even if submission may have succeeded.

Rollout order: deploy and verify the updated doorbell daemon first, then
register each intended Codex node with the documented options; an older daemon
returns DELIVER_UNSUPPORTED.
"""

import os
import re
import sqlite3
import subprocess
import uuid


def deliver_notification(target, notification_id, message, auto_enter):
    """Doorbell boundary: use only a recognized local native Codex binary."""
    import pwd
    import shutil
    from . import paths
    from .terminal_frontend import executable_kind
    executable = shutil.which("codex")
    if not executable or not notification_id:
        return "UNKNOWN", -1
    executable = os.path.realpath(executable)
    if executable_kind(executable, pwd.getpwuid(os.getuid()).pw_dir) != "codex":
        return "UNKNOWN", -1
    try:
        queue = QueueDelivery(paths.state("codex_queue_receipts.db"), executable)
        try:
            state, _ = queue.deliver(target, notification_id, message, auto_enter=auto_enter)
            return state, 1 if state == "QUEUED" else 0
        finally:
            queue.close()
    except (OSError, sqlite3.Error, ValueError):
        return "UNKNOWN", -1


def capture_current_thread():
    """Accept per-request thread identity only below a native managed daemon.

    No terminal ownership is inferred. Shared daemon ancestry is used only to
    identify the execution product. Same-OS-user environment forgery is outside
    this local routing check, as for existing terminal route capture.
    """
    from .terminal_frontend import process_info, native_executable, executable_kind
    import pwd
    try:
        target = current_thread()
        uid = os.getuid()
        home = pwd.getpwuid(uid).pw_dir
        session = os.getsid(0)
        pid = os.getpid()
        seen = set()
        for _ in range(64):
            if pid <= 1 or pid in seen:
                return None
            seen.add(pid)
            info = process_info(pid)
            if info is None or info[0] != uid:
                return None
            if os.getsid(pid) != session:
                executable = native_executable(pid)
                if not executable or executable_kind(executable, home) != "codex":
                    return None
                if info[2] != pid or info[4] not in ("??", "?"):
                    return None
                args = info[5]
                if os.path.basename(args[0]) != "codex" or args[1:] != [
                    "app-server", "--listen", "unix://",
                    "--analytics-default-enabled", "--managed-daemon",
                ]:
                    return None
                if process_info(pid) != info or native_executable(pid) != executable:
                    return None
                return target
            pid = info[1]
    except (OSError, ValueError, TypeError, IndexError, subprocess.SubprocessError):
        return None
    return None


def thread_id(value):
    """Require a canonical UUID; session names are not routing identities."""
    if not isinstance(value, str):
        raise ValueError("Codex thread must be a canonical UUID")
    try:
        valid = str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Codex thread must be a canonical UUID")
    return value


def current_thread(environ=None):
    """Read the execution context, not the shared daemon's inherited tab ID.

    This is a candidate identity only, not proof of terminal ownership.
    Registration must separately validate the Codex execution context.
    """
    env = os.environ if environ is None else environ
    return thread_id(env.get("CODEX_THREAD_ID"))


class QueueDelivery:
    """One attempted submission per (thread UUID, notification identity).

    Use a dedicated local receipt database. The caller supplies an absolute,
    trusted Codex executable path; no route may supply executable arguments.
    The caller must reuse the same delivery_id after restart, timeout, or any
    exception, including a receipt UPDATE/commit failure after queue acceptance.
    Never allocate a new identity merely to retry an uncertain submission.
    """

    def __init__(self, receipt_db, executable, run=None):
        if not os.path.isabs(executable):
            raise ValueError("Codex executable must be an absolute path")
        self.executable = executable
        self.run = run or subprocess.run
        self.db = sqlite3.connect(receipt_db, timeout=5)
        self.db.execute("""CREATE TABLE IF NOT EXISTS codex_queue_receipts (
            thread_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('PENDING','QUEUED','UNKNOWN')),
            submission_id TEXT,
            PRIMARY KEY(thread_id, delivery_id)
        )""")
        self.db.commit()

    def close(self):
        self.db.close()

    def deliver(self, target, delivery_id, message, *, auto_enter=False):
        target = thread_id(target)
        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError("stable delivery_id required")
        if not isinstance(message, str) or not message or "\x00" in message:
            raise ValueError("nonempty notification text required")
        # Queue submits input; it has no equivalent to typing without Enter.
        if auto_enter is not True:
            return "DISABLED", None

        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT state, submission_id FROM codex_queue_receipts "
                "WHERE thread_id=? AND delivery_id=?", (target, delivery_id),
            ).fetchone()
            if row:
                self.db.commit()
                return ("UNKNOWN" if row[0] == "PENDING" else row[0]), row[1]
            self.db.execute(
                "INSERT INTO codex_queue_receipts VALUES (?, ?, 'PENDING', NULL)",
                (target, delivery_id),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

        state, submission = "UNKNOWN", None
        try:
            result = self.run(
                [self.executable, "queue", "--remote=unix://",
                 f"--thread={target}", f"--message={message}"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=15, check=False,
            )
            receipt = re.fullmatch(
                r"Queued message ([0-9a-f-]{36}) for thread " + re.escape(target) + r"\.",
                result.stdout.strip(),
            )
            if result.returncode == 0 and receipt:
                submission = thread_id(receipt.group(1))
                state = "QUEUED"
        except (OSError, subprocess.SubprocessError, ValueError):
            # Failure can follow acceptance. Do not translate it to safe retry.
            pass
        self.db.execute(
            "UPDATE codex_queue_receipts SET state=?, submission_id=? "
            "WHERE thread_id=? AND delivery_id=?",
            (state, submission, target, delivery_id),
        )
        self.db.commit()
        return state, submission
