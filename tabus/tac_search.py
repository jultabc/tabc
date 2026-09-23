"""Member-scoped, read-only TAC search. No delivery state changes."""

from .tac_identity import name_key


def search(con, node, query, tac=None, limit=50):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must contain 1..500 characters")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 to 200")
    # 🔴 The name column exists only once the identity columns are there. A ledger
    #    before the migration has neither the column nor a name to search, so the
    #    expression stands in as NULL and the query is the same one as before.
    columns = {row[1] for row in con.execute("PRAGMA table_info(tacs)")}
    has_name = "name" in columns
    has_name_key = "name_key" in columns
    name = "t.name" if has_name else "NULL"
    folded_name = "t.name_key" if has_name_key else "NULL"
    folded_query = name_key(query)
    # SQLite lower() folds ASCII only. Register the same Unicode normalization
    # and case-folding used by TAC names so descriptions and message text have
    # one multilingual search contract too.
    con.create_function("tabc_fold", 1, lambda value: name_key(value or ""), deterministic=True)
    if tac:
        from .tac_store import resolve

        tac = resolve(con, tac)[0] or tac
    # instr implements literal matching, including %, _, and quotes. Parameters
    # are never interpolated into SQL. lower handles ASCII case folding.
    rows = con.execute(
        f"""WITH visible AS (
            SELECT t.* FROM tacs t JOIN tac_members tm ON tm.tac_id=t.tac_id
            WHERE tm.member_node_id=?
              AND NOT EXISTS (SELECT 1 FROM removed_nodes WHERE node_id=?)
              AND (? IS NULL OR t.tac_id=?)
        ), hits AS (
            SELECT 'tac' AS kind, t.tac_id, NULL AS id,
                   COALESCE(t.label,{name},t.tac_id) AS subject,
                   t.close_summary AS body,
                   COALESCE(t.closed_at,t.created_at) AS at
            FROM visible t
            WHERE instr(tabc_fold(t.tac_id),?)>0
               OR instr(COALESCE({folded_name},''),COALESCE(?,''))>0
               OR instr(tabc_fold(COALESCE(t.label,'')),?)>0
               OR instr(tabc_fold(COALESCE(t.close_summary,'')),?)>0
            UNION ALL
            SELECT 'message',m.tac_id,m.id,m.subject,m.body,m.accepted_at
            FROM messages m JOIN visible t ON t.tac_id=m.tac_id
            WHERE instr(tabc_fold(m.subject),?)>0
               OR instr(tabc_fold(m.body),?)>0
        ) SELECT * FROM hits ORDER BY at DESC,tac_id,kind,id LIMIT ?""",
        (node, node, tac, tac, folded_query, folded_query, folded_query, folded_query,
         folded_query, folded_query, limit),
    ).fetchall()
    return [dict(row) for row in rows]
