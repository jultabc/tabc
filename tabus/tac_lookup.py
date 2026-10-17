"""Find a tac by name, read-only. The client turns a typed name into an identifier here.

🔴 Designation stays by identifier. This endpoint does not make a name a designation:
every other endpoint still refuses a name with TAC_ID_INVALID. It answers one question,
"which tac carries this name now", so the client can print the identifier it is about
to use and then send that identifier. A rename moves the name; the identifier the
client printed is what the request used.

Names are unique on the folded key (ux_tacs_name_key), closed tacs included, so an
exact match is at most one row. `similar` lists the rows whose folded name contains
the folded query, for a caller who typed part of a name. Neither changes state.
"""

from .tac_identity import name_key

SIMILAR_LIMIT = 20


def _row(r):
    return {"tac_id": r["tac_id"], "name": r["name"], "label": r["label"],
            "closed_at": r["closed_at"]}


def lookup(con, name, limit=SIMILAR_LIMIT):
    """(exact row or None, similar rows). Raises ValueError on a bad query."""
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("name must contain 1..200 characters")
    columns = {row[1] for row in con.execute("PRAGMA table_info(tacs)")}
    # 🔴 A ledger before the identity migration has no name column. There is nothing
    #    to look up there: the identifier is the string a caller types.
    if not {"name", "name_key"} <= columns:
        return None, []
    key = name_key(name.strip())
    rows = con.execute(
        "SELECT tac_id, name, label, closed_at, name_key FROM tacs ORDER BY name"
    ).fetchall()
    exact = next((r for r in rows if r["name_key"] == key), None)
    # 🔴 Compared on name_key in Python, as tac_store.search_name does: sqlite lower()
    #    folds ASCII only.
    similar = [r for r in rows if r["name_key"] and key in r["name_key"]
               and (exact is None or r["tac_id"] != exact["tac_id"])][:limit]
    return (_row(exact) if exact else None), [_row(r) for r in similar]
