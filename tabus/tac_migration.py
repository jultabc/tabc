"""Converting a ledger's tac identifiers to UUIDs.

The conversion runs on a copy and publishes a new file; the ledger it reads is opened
read-only and is never written. Nothing here runs at import or when the daemon starts:
a ledger changes only when someone runs this tool.

    python -m tabus.tac_migration <ledger.db>                 # preflight, reads only
    python -m tabus.tac_migration <ledger.db> --output <new>  # convert into a new file

The display name of an existing tac is its current string id — the string the team
already types and the one written in documents and letters. `label` is the tac's
description and is left exactly as it is.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from .tac_identity import check_name, is_uuid_id, name_key, new_id
from .tac_store import ensure_columns, key_mismatches

# Every column that holds a tac id. The conversion rewrites all of them together.
REFERENCES = (("tac_members", "tac_id"), ("messages", "tac_id"),
              ("tac_links", "child_tac"), ("tac_links", "parent_tac"))
# Columns that hold a tac id on purpose and are not rewritten: they record history.
HISTORICAL = {("tac_legacy_ids", "old_id"), ("tac_legacy_ids", "tac_id"), ("tac_name_changes", "tac_id")}
# 🔴 Columns named something else that still held a tac id. A leftover like this passed
#    the name-based scan silently; a backup ledger still carries prev_tac_id (jack).
KNOWN_OTHER_NAMES = ("prev_tac_id", "group_id", "grp_id")
TAC_COLUMN_NAMES = ("tac_id", "child_tac", "parent_tac") + KNOWN_OTHER_NAMES


class MigrationBlocked(ValueError):
    """The conversion stopped before writing anything. `issues` says what to look at."""

    def __init__(self, issues):
        self.issues = issues
        super().__init__(json.dumps(issues, ensure_ascii=False))


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def reference_inventory(con):
    """Every column that looks like it holds a tac id, by name or by foreign key."""
    found = set()
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for row in con.execute(f"PRAGMA table_info({_quoted(table)})"):
            if row[1] in TAC_COLUMN_NAMES:
                found.add((table, row[1]))
        for row in con.execute(f"PRAGMA foreign_key_list({_quoted(table)})"):
            if row[2] == "tacs":
                found.add((table, row[3]))
    return found


def check_references(con):
    """Refuse to convert while a tac column exists that this tool does not rewrite."""
    allowed = set(REFERENCES) | HISTORICAL | {("tacs", "tac_id")}
    unknown = reference_inventory(con) - allowed
    if unknown:
        raise MigrationBlocked([{"reason": "tac column this tool does not rewrite",
                                 "table": table, "column": column}
                                for table, column in sorted(unknown)])


def plan(con, renames=None):
    """What the conversion would do, or MigrationBlocked with every reason it cannot.

    renames: {old string id: name to use instead}. A tac whose old id cannot be a name has
    no other way through — it cannot be renamed first, because renaming needs the UUID that
    this conversion has not minted yet (jack).
    """
    renames = renames or {}
    check_references(con)
    columns = {row[1] for row in con.execute("PRAGMA table_info(tacs)")}
    if not columns:
        raise MigrationBlocked([{"reason": "no tacs table"}])
    converted = {"name", "name_key"} <= columns
    fields = "tac_id, label" + (", name, name_key" if converted else "")
    issues, keys, changes = [], {}, []
    for row in con.execute(f"SELECT {fields} FROM tacs ORDER BY tac_id"):
        old_id = row[0]
        done = converted and row[2] is not None and is_uuid_id(old_id)
        # 🔴 The name of an existing tac is its old string id, not its label. The label is
        #    a sentence describing the tac; it stays where it is.
        candidate = row[2] if done else renames.get(old_id, old_id)
        reason = check_name(candidate)
        if reason:
            issues.append({"old_id": old_id, "name": candidate, "reason": reason,
                           "fix": f"pass --rename {old_id}=<name> to give this tac a different name"})
            continue
        key = name_key(candidate.strip())
        if key in keys:
            issues.append({"old_id": old_id, "reason": "two tacs would share one name",
                           "name": candidate, "other": keys[key]})
        else:
            keys[key] = old_id
        if done:
            if row[3] != key:
                # 🔴 The stored key is not what this interpreter folds the name into, so the row
                #    was written where that name folds differently (jack).
                issues.append({"old_id": old_id, "name": candidate,
                               "reason": "stored name key was folded by another Unicode version"})
        else:
            changes.append({"old_id": old_id, "name": candidate.strip(), "name_key": key})
    for table, column in REFERENCES:
        present = {row[1] for row in con.execute(f"PRAGMA table_info({_quoted(table)})")}
        if column not in present:
            issues.append({"reason": "reference column missing", "table": table, "column": column})
            continue
        for row in con.execute(f"SELECT DISTINCT {column} FROM {_quoted(table)} WHERE {column} IS NOT NULL "
                               f"AND {column} NOT IN (SELECT tac_id FROM tacs)"):
            issues.append({"reason": "reference to a tac that does not exist",
                           "table": table, "column": column, "id": row[0]})
    if issues:
        raise MigrationBlocked(issues)
    return changes


def source_state(con):
    """What the ledger held when the copy was taken.

    🔴 Written into the output so the window between the copy and any later swap can be
    measured. Without it nobody can say afterwards what was in the ledger at that moment.
    """
    counts = {}
    for table in ("tacs", "messages", "deliveries", "tac_members", "tac_links"):
        row = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row[0]:
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0]
    newest = None
    if "messages" in counts:
        columns = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        if "accepted_at" in columns:
            newest = con.execute("SELECT MAX(accepted_at) FROM messages").fetchone()[0]
    return {"rows": counts, "newest_message_at": newest}


def convert(con, at, renames=None):
    """Convert an opened copy in one transaction. Returns the old id to new id mapping.

    🔴 Foreign keys are not enforced on this connection, so `foreign_key_check` at the
    end is what actually proves the references line up — not the pragma above it.
    """
    if con.in_transaction:
        raise ValueError("the conversion needs a transaction of its own")
    con.execute("BEGIN IMMEDIATE")
    try:
        changes = plan(con, renames)
        ensure_columns(con)
        mapping = []
        for item in changes:
            identifier = new_id()
            while con.execute("SELECT 1 FROM tacs WHERE tac_id=?", (identifier,)).fetchone():
                identifier = new_id()
            old = item["old_id"]
            # 🔴 label is not in this UPDATE. Writing the name into it would erase every
            #    description in one statement, and nothing would hold the old value.
            con.execute("UPDATE tacs SET tac_id=?, name=?, name_key=? WHERE tac_id=?",
                        (identifier, item["name"], item["name_key"], old))
            for table, column in REFERENCES:
                con.execute(f"UPDATE {_quoted(table)} SET {column}=? WHERE {column}=?", (identifier, old))
            con.execute("INSERT INTO tac_legacy_ids(old_id, tac_id, migrated_at) VALUES(?,?,?)",
                        (old, identifier, at))
            mapping.append({"old_id": old, "tac_id": identifier, "name": item["name"]})
        if plan(con, renames):
            raise MigrationBlocked([{"reason": "a tac was left unconverted"}])
        # 🔴 A stored key this interpreter would not produce means the row was written where
        #    the name folds differently. Left alone it hides the row from every lookup here.
        stale = key_mismatches(con)
        if stale:
            raise MigrationBlocked([{"reason": "stored name key was folded by another Unicode version",
                                     "tac_id": tac_id, "name": name} for tac_id, name, _ in stale])
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationBlocked([{"reason": "foreign key violation", "row": list(row)} for row in violations])
        con.commit()
        return mapping
    except BaseException:
        con.rollback()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tabus.tac_migration", description=__doc__)
    parser.add_argument("source", type=Path, help="the ledger to read; it is never written")
    parser.add_argument("--output", type=Path, help="write the converted ledger here; without it, only report")
    parser.add_argument("--rename", action="append", metavar="OLD_ID=NAME", default=[],
                        help="name to use for a tac whose old id cannot be one; repeatable")
    args = parser.parse_args(argv)
    renames = {}
    for item in args.rename:
        old_id, sep, name = item.partition("=")
        if not sep or not old_id or not name:
            parser.error(f"--rename takes OLD_ID=NAME, received {item!r}")
        renames[old_id] = name
    source = args.source.resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        state = source_state(original)
        if args.output is None:
            print(json.dumps({"source": state, "would_convert": plan(original, renames)},
                             ensure_ascii=False, indent=1))
            return 0
        output = args.output.resolve()
        if output == source:
            parser.error("the output must be a different file from the source")
        # 🔴 Built under a temporary name on the output's own filesystem, then linked into
        #    place: an existing file is never replaced, and no half-written ledger is
        #    published under the final name.
        with tempfile.TemporaryDirectory(prefix=".tac-migration-", dir=output.parent) as tmp:
            candidate = Path(tmp) / "candidate.db"
            with closing(sqlite3.connect(candidate)) as copy:
                original.backup(copy)
                from .bus import now_iso
                mapping = convert(copy, now_iso(), renames)
            os.link(candidate, output)
    print(json.dumps({"output": str(output), "source": state, "converted": mapping},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
