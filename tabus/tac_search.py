"""Member-scoped, read-only TAC search. No delivery state changes."""


def search(con, node, query, tac=None, limit=50):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must contain 1..500 characters")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 to 200")
    # instr implements literal matching, including %, _, and quotes. Parameters
    # are never interpolated into SQL. lower handles ASCII case folding.
    rows = con.execute(
        """WITH visible AS (
            SELECT t.* FROM tacs t JOIN tac_members tm ON tm.tac_id=t.tac_id
            WHERE tm.member_node_id=?
              AND NOT EXISTS (SELECT 1 FROM removed_nodes WHERE node_id=?)
              AND (? IS NULL OR t.tac_id=?)
        ), hits AS (
            SELECT 'tac' AS kind, t.tac_id, NULL AS id,
                   COALESCE(t.label,t.tac_id) AS subject,
                   t.close_summary AS body,
                   COALESCE(t.closed_at,t.created_at) AS at
            FROM visible t
            WHERE instr(lower(t.tac_id),lower(?))>0
               OR instr(lower(COALESCE(t.label,'')),lower(?))>0
               OR instr(lower(COALESCE(t.close_summary,'')),lower(?))>0
            UNION ALL
            SELECT 'message',m.tac_id,m.id,m.subject,m.body,m.accepted_at
            FROM messages m JOIN visible t ON t.tac_id=m.tac_id
            WHERE instr(lower(m.subject),lower(?))>0
               OR instr(lower(m.body),lower(?))>0
        ) SELECT * FROM hits ORDER BY at DESC,tac_id,kind,id LIMIT ?""",
        (node, node, tac, tac, query, query, query, query, query, limit),
    ).fetchall()
    return [dict(row) for row in rows]
