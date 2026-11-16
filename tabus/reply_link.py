"""Reply links: a message may name the message it answers (`reply_to`).

The columns were already in the store (messages.reply_to, messages.thread_id).
This module adds the rules for who may link to what, and the read side: the
parent shown above a reply, and one thread drawn as a tree.

Visibility is one rule, used everywhere here: a node sees a message when it sent
it or when a delivery row addressed to it exists, in any state. A message a node
cannot see is never a reply target for it, never a prefix candidate, and never a
line in its thread view.

🔴 Outside (CLI, daemon response) "no such message" and "a message you cannot
   see" are one refusal, REPLY_TARGET_UNAVAILABLE, with the same sentence. Telling
   them apart would let any node probe for ids it was never sent. The difference
   is kept only in `.internal` on the refusal, which the daemon writes to its own
   log and never returns.

🔴 Nothing deletes from `messages` (no DELETE in this release; `tabc rm --purge`
   removes only a node with no history). So a parent cannot vanish under its
   replies, and no foreign-key action is set. A release that adds message
   deletion decides what happens to replies then.
"""

from .send_refusal import SendRefusal

# A prefix shorter than this is refused before any lookup. Eight hex characters
# is what the listings print.
REPLY_PREFIX_MIN = 8
# Candidates listed when a prefix matches more than one visible message.
AMBIGUOUS_LIST_MAX = 10

_VISIBLE = (
    "(m.sender_id = ? OR EXISTS (SELECT 1 FROM deliveries d "
    "WHERE d.message_id = m.id AND d.recipient_id = ?))"
)


def _unavailable(target, internal):
    r = SendRefusal(
        f"reply target unavailable: {target}",
        "REPLY_TARGET_UNAVAILABLE",
        "no message with this id that the sender sent or received",
        {"field": "reply_to", "target": target},
        "never",
    )
    r.internal = internal
    return r


def _invalid(target, why):
    return SendRefusal(
        f"reply target invalid: {why}",
        "REPLY_TARGET_INVALID",
        why,
        {"field": "reply_to", "min_length": REPLY_PREFIX_MIN},
        "never",
    )


def _ambiguous(target, candidates, total):
    return SendRefusal(
        f"reply target ambiguous: {target} matches {total} messages",
        "REPLY_TARGET_AMBIGUOUS",
        "type more of the id",
        {
            "field": "reply_to",
            "target": target,
            "candidates": candidates,
            "more": total - len(candidates),
        },
        "never",
    )


def _escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve_reply_target(con, node, target):
    """Resolve `target` (a full id, or a prefix of at least REPLY_PREFIX_MIN
    characters) among the messages `node` can see.

    Returns (row, None) with row = {id, thread_id, subject}, or (None, refusal).
    """
    if not isinstance(target, str):
        return None, _invalid(target, "reply_to must be a string")
    if len(target) < REPLY_PREFIX_MIN:
        return None, _invalid(
            target, f"reply_to needs at least {REPLY_PREFIX_MIN} characters of the id"
        )
    row = con.execute(
        f"SELECT m.id, m.thread_id, m.subject FROM messages m WHERE m.id = ? AND {_VISIBLE}",
        (target, node, node),
    ).fetchone()
    if row:
        return dict(row), None
    rows = con.execute(
        f"SELECT m.id, m.thread_id, m.subject FROM messages m "
        f"WHERE m.id LIKE ? ESCAPE '\\' AND {_VISIBLE} ORDER BY m.accepted_at, m.id "
        f"LIMIT {AMBIGUOUS_LIST_MAX + 1}",
        (_escape_like(target) + "%", node, node),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0]), None
    if len(rows) > 1:
        n = con.execute(
            f"SELECT count(*) FROM messages m WHERE m.id LIKE ? ESCAPE '\\' AND {_VISIBLE}",
            (_escape_like(target) + "%", node, node),
        ).fetchone()[0]
        return None, _ambiguous(target, [r["id"] for r in rows[:AMBIGUOUS_LIST_MAX]], n)
    # Not visible. Whether it exists at all goes to the daemon log only.
    exists = con.execute(
        "SELECT 1 FROM messages WHERE id = ? OR id LIKE ? ESCAPE '\\' LIMIT 1",
        (target, _escape_like(target) + "%"),
    ).fetchone()
    return None, _unavailable(target, "unseen" if exists else "unknown")


def parent_of(con, node, message_id):
    """The parent shown above a reply: {id, subject} when `node` can see the
    parent, {id} when it cannot, None when the message is not a reply."""
    row = con.execute("SELECT reply_to FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row or not row["reply_to"]:
        return None
    parent = con.execute(
        f"SELECT m.id, m.subject FROM messages m WHERE m.id = ? AND {_VISIBLE}",
        (row["reply_to"], node, node),
    ).fetchone()
    return dict(parent) if parent else {"id": row["reply_to"]}


def thread_view(con, node, target):
    """One thread as `node` sees it, oldest first, each row with its depth.

    Returns (rows, None) or (None, refusal). A row: id, sender_id, subject,
    accepted_at, reply_to, depth, recipients, parent_hidden.
    A reply whose parent `node` cannot see is drawn at depth 0 with
    parent_hidden=True, so a hidden message never appears and never leaves a gap
    that names it.
    """
    root, refusal = resolve_reply_target(con, node, target)
    if refusal is not None:
        return None, refusal
    rows = [
        dict(r)
        for r in con.execute(
            f"SELECT m.id, m.sender_id, m.subject, m.accepted_at, m.reply_to "
            f"FROM messages m WHERE m.thread_id = ? AND {_VISIBLE} "
            f"ORDER BY m.accepted_at, m.id",
            (root["thread_id"], node, node),
        )
    ]
    seen = {r["id"]: r for r in rows}
    children = {}
    tops = []
    for r in rows:
        r["recipients"] = [
            d["recipient_id"]
            for d in con.execute(
                "SELECT recipient_id FROM deliveries WHERE message_id = ? ORDER BY recipient_id",
                (r["id"],),
            )
        ]
        r["parent_hidden"] = bool(r["reply_to"]) and r["reply_to"] not in seen
        if r["reply_to"] in seen:
            children.setdefault(r["reply_to"], []).append(r)
        else:
            tops.append(r)
    # 🔴 A loop, not recursion: a chain of replies can be longer than Python's
    #    recursion limit.
    out = []
    stack = [(t, 0) for t in reversed(tops)]
    while stack:
        r, depth = stack.pop()
        r["depth"] = depth
        out.append(r)
        stack.extend((c, depth + 1) for c in reversed(children.get(r["id"], [])))
    return out, None
