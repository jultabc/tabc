"""Creating and renaming tacs once the identifier is a UUID.

The identifier is minted here and never comes from a caller. The name is stored as it arrived apart
from surrounding spaces; `name_key` holds the folded form that decides both "is this name taken"
and "which tac has this name". `label` is the tac's description and is not touched
by either operation — a rename or a migration that overwrote it would erase the one
sentence that says what the tac is for.
"""

from . import tac_refusal
from .tac_identity import check_name, is_uuid_id, name_key, new_id

# 🔴 A tac_id that is not a canonical UUID means the ledger still holds rows written
#    by an older release (or by one running after a rollback). Creating or renaming
#    against that state would mix two kinds of identifier, so both stop until the
#    migration has run again.
NOT_CONVERTED = "this ledger still holds tacs without a UUID; run the tac migration first"


def ensure_columns(con):
    """Add the identity columns and their history tables. Existing rows keep their values."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(tacs)")}
    for column in ("name", "name_key"):
        if column not in columns:
            con.execute(f"ALTER TABLE tacs ADD COLUMN {column} TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tacs_name_key ON tacs(name_key)")
    con.execute("""CREATE TABLE IF NOT EXISTS tac_name_changes(
                       tac_id     TEXT NOT NULL,
                       old_name   TEXT NOT NULL,
                       new_name   TEXT NOT NULL,
                       changed_by TEXT,
                       changed_at TEXT NOT NULL)""")
    # 🔴 The string a tac was addressed by before the migration. Messages, documents and
    #    letters carry those strings, so the mapping is what makes them readable later.
    con.execute("""CREATE TABLE IF NOT EXISTS tac_legacy_ids(
                       old_id      TEXT PRIMARY KEY,
                       tac_id      TEXT NOT NULL UNIQUE REFERENCES tacs(tac_id),
                       migrated_at TEXT NOT NULL)""")


def converted(con):
    """True when this ledger carries the identity columns the UUID scheme needs.

    🔴 Asked separately from the rows below. A ledger with the columns missing and no tac
    rows at all has nothing to list, and create would run on to an INSERT naming a column
    that does not exist."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(tacs)")}
    return {"name", "name_key"} <= columns


def unconverted(con):
    """Rows that are not on the UUID scheme yet: no name, or an identifier that is not a UUID."""
    if not converted(con):
        return [row[0] for row in con.execute("SELECT tac_id FROM tacs")]
    rows = con.execute("SELECT tac_id, name, name_key FROM tacs").fetchall()
    return [row[0] for row in rows
            if row[1] is None or row[2] is None or not is_uuid_id(row[0])]


def key_mismatches(con):
    """Rows whose stored key is not what this interpreter computes from the stored name.

    🔴 The key is written once, by whichever interpreter created or renamed the tac. A
    later Unicode version folds some names differently, so a row written elsewhere can
    carry a key this one would never produce: the name is then invisible to a lookup here,
    and a second tac with the identical name can be created without the unique index
    noticing. Recomputing is the only way that shows up (jack).
    """
    if not converted(con):
        return []
    return [(row[0], row[1], row[2]) for row in
            con.execute("SELECT tac_id, name, name_key FROM tacs WHERE name IS NOT NULL")
            if row[2] != name_key(row[1])]


def resolve(con, given):
    """(tac_id, refusal). The designation a caller sent, checked against this ledger.

    🔴 A tac is designated by its identifier, never by its name. A name can change, so
    a designation that followed a name would name a different tac after a rename. Names
    are for creating a tac and for finding one (`tac_search`); the identifier is what
    comes back from that search and what every other call takes.

    Before the conversion the identifier IS the string a caller types, so a ledger that
    has not been converted keeps working exactly as it did. After it, only the canonical
    UUID designates, and the identifier a tac carried before the conversion designates
    nothing — `tac_legacy_ids` records it so an old letter can be read, not addressed.
    """
    if not isinstance(given, str) or not given.strip():
        return None, tac_refusal.id_invalid(given)
    if not converted(con):
        return given, None
    if is_uuid_id(given):
        return given, None
    # 🔴 A row whose identifier is still a string, in a ledger that has the columns but
    #    has not been converted. Designating it keeps the ledger usable in that window.
    if con.execute("SELECT 1 FROM tacs WHERE tac_id=?", (given,)).fetchone():
        return given, None
    return None, tac_refusal.id_invalid(given)


def create(con, name, by=None, at=None):
    """(ok, message, row). The identifier is minted here; surrounding spaces are removed and
    the rest of the name is stored as sent."""
    reason = check_name(name)
    if reason:
        return False, tac_refusal.name_invalid(name, reason), None
    display = name.strip()
    key = name_key(display)
    if not converted(con) or unconverted(con):
        return False, tac_refusal.not_converted(), None
    # 🔴 Same rule as before the UUID: a tac may not take a node's name, so a send
    #    target is never ambiguous between a node and a tac.
    if con.execute("SELECT 1 FROM nodes WHERE node_id=?", (display,)).fetchone():
        return False, tac_refusal.name_invalid(
            display, f"name collision: '{display}' is a node name, and a tac cannot share one"), None
    taken = con.execute("SELECT tac_id FROM tacs WHERE name_key=?", (key,)).fetchone()
    if taken:
        return False, tac_refusal.name_taken(display, taken[0]), None
    identifier = new_id()
    while con.execute("SELECT 1 FROM tacs WHERE tac_id=?", (identifier,)).fetchone():
        identifier = new_id()  # 🔴 A minted identifier that collides is drawn again, not refused.
    con.execute("INSERT INTO tacs(tac_id, label, name, name_key, created_at, created_by) "
                "VALUES(?,?,?,?,?,?)", (identifier, None, display, key, at, by))
    return True, f"created tac {identifier}: {display}", {"tac_id": identifier, "name": display}


def rename(con, tac_id, name, by=None, at=None):
    """(ok, message, row). Only a canonical UUID names the tac to rename."""
    if not is_uuid_id(tac_id):
        return False, tac_refusal.id_invalid(tac_id), None
    reason = check_name(name)
    if reason:
        return False, tac_refusal.name_invalid(name, reason), None
    display = name.strip()
    key = name_key(display)
    if not converted(con) or unconverted(con):
        return False, tac_refusal.not_converted(), None
    row = con.execute("SELECT name FROM tacs WHERE tac_id=?", (tac_id,)).fetchone()
    if row is None:
        return False, tac_refusal.not_found(tac_id), None
    if con.execute("SELECT 1 FROM nodes WHERE node_id=?", (display,)).fetchone():
        return False, tac_refusal.name_invalid(
            display, f"name collision: '{display}' is a node name, and a tac cannot share one"), None
    taken = con.execute("SELECT tac_id FROM tacs WHERE name_key=?", (key,)).fetchone()
    if taken and taken[0] != tac_id:
        return False, tac_refusal.name_taken(display, taken[0]), None
    # 🔴 label is left alone. The description is not the name.
    con.execute("UPDATE tacs SET name=?, name_key=? WHERE tac_id=?", (display, key, tac_id))
    if row[0] != display:
        con.execute("INSERT INTO tac_name_changes(tac_id, old_name, new_name, changed_by, changed_at) "
                    "VALUES(?,?,?,?,?)", (tac_id, row[0], display, by, at))
    return True, f"renamed tac {tac_id}: {display}", {"tac_id": tac_id, "name": display}


def resolve_name(con, name):
    """Rows whose name matches exactly, compared on the folded key."""
    return con.execute("SELECT tac_id, name, label FROM tacs WHERE name_key=? ORDER BY name",
                       (name_key(name),)).fetchall()


def search_name(con, fragment):
    """Rows whose name contains the fragment, compared on the folded key.

    🔴 The comparison happens on name_key rather than in SQL: sqlite's lower() folds
    ASCII only, so a name with an accent or a different normal form would not match
    what a caller typed.
    """
    piece = name_key(fragment)
    return [row for row in con.execute("SELECT tac_id, name, label, name_key FROM tacs ORDER BY name")
            if row[3] and piece in row[3]]
