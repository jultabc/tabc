"""Read-only sent history. The HTTP layer must authenticate the sender first."""


def list_sent(con, sender, limit=20, message_id=None):
    where = "sender_id=?"
    params = [sender]
    if message_id is not None:
        where += " AND id=?"
        params.append(message_id)
    params.append(max(1, min(limit, 200)))
    rows = con.execute(
        f"SELECT id, subject, body, accepted_at, tac_id FROM messages "
        f"WHERE {where} ORDER BY accepted_at DESC, rowid DESC LIMIT ?",
        params,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["deliveries"] = [
            dict(d) for d in con.execute(
                "SELECT recipient_id, state FROM deliveries WHERE message_id=? ORDER BY id",
                (item["id"],),
            )
        ]
        result.append(item)
    return result
