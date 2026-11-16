"""Reply links: a message may name the message it answers (`reply_to`).

The columns were already in the store (messages.reply_to, messages.thread_id).
This module adds the rules for who may link to what, and the read side: the
parent shown above a reply, and one thread drawn as a tree.

Visibility is one rule, used everywhere here. A node sees a message when
- it sent it, or
- a delivery row addressed to it exists, in any state, or
- the message went to a tac and the node is a member of that tac now.
The third matches what a member can already read with `tabc tac show`, so a
member who joined late can answer earlier tac mail. A node removed from the tac
keeps only what was delivered to it.
A message a node cannot see is never a reply target for it, never a prefix
candidate, and never a line in its thread view.

🔴 Outside (CLI, daemon response) "no such message" and "a message you cannot
   see" are one refusal, REPLY_TARGET_UNAVAILABLE, with the same sentence. Telling
   them apart would let any node probe for ids it was never sent. The difference
   is kept only in `.internal` on the refusal, which the daemon writes to its own
   log and never returns.

🔴 The target is checked for shape before any lookup: 8 to 36 characters of
   0-9, a-f and "-", the characters a message id has (the CLI and the MCP adapter
   store canonical lowercase UUIDs). Upper-case hex is folded to lower case
   first, and prefix matching is case-sensitive, so there is one rule. Anything
   else is REPLY_TARGET_INVALID and the target is not repeated back, so a refusal
   or a log line never carries text the caller chose beyond an id's characters.

🔴 Nothing deletes from `messages` (no DELETE in this release; `tabc rm --purge`
   removes only a node with no history). So a parent cannot vanish under its
   replies, and no foreign-key action is set. A release that adds message
   deletion decides what happens to replies then.
"""

from .send_refusal import SendRefusal

# A prefix shorter than this is refused before any lookup. Eight hex characters
# is what the listings print.
REPLY_PREFIX_MIN = 8
# A full UUID in canonical form.
REPLY_TARGET_MAX = 36
_ID_CHARS = frozenset("0123456789abcdef-")
# Candidates listed when a prefix matches more than one visible message.
AMBIGUOUS_LIST_MAX = 10

# Three parameters, all the node id.
_VISIBLE = (
    "(m.sender_id = ? "
    "OR EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id = m.id AND d.recipient_id = ?) "
    "OR (m.tac_id IS NOT NULL AND EXISTS (SELECT 1 FROM tac_members tm "
    "WHERE tm.tac_id = m.tac_id AND tm.member_node_id = ?)))"
)


def _vis(node):
    return (node, node, node)


def _unavailable(target, internal):
    r = SendRefusal(
        f"reply target unavailable: {target}",
        "REPLY_TARGET_UNAVAILABLE",
        "no message with this id that the sender sent, received, or can read in one of its tacs",
        {"field": "reply_to", "target": target},
        "never",
    )
    r.internal = internal
    return r


def _invalid(why):
    return SendRefusal(
        f"reply target invalid: {why}",
        "REPLY_TARGET_INVALID",
        why,
        {"field": "reply_to", "min_length": REPLY_PREFIX_MIN, "max_length": REPLY_TARGET_MAX},
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


def check_target_shape(target):
    """(normalized target, None) or (None, REPLY_TARGET_INVALID). No lookup."""
    if not isinstance(target, str):
        return None, _invalid("reply_to must be a string")
    if not REPLY_PREFIX_MIN <= len(target) <= REPLY_TARGET_MAX:
        return None, _invalid(
            f"reply_to must be {REPLY_PREFIX_MIN} to {REPLY_TARGET_MAX} characters of a message id"
        )
    t = target.lower()
    if not set(t) <= _ID_CHARS:
        return None, _invalid("reply_to may contain only 0-9, a-f and -")
    return t, None


def resolve_reply_target(con, node, target):
    """Resolve `target` (a full id, or a prefix of at least REPLY_PREFIX_MIN
    characters) among the messages `node` can see.

    Returns (row, None) with row = {id, thread_id, subject}, or (None, refusal).
    """
    target, refusal = check_target_shape(target)
    if refusal is not None:
        return None, refusal
    row = con.execute(
        f"SELECT m.id, m.thread_id, m.subject FROM messages m WHERE m.id = ? AND {_VISIBLE}",
        (target, *_vis(node)),
    ).fetchone()
    if row:
        return dict(row), None
    # 🔴 substr, not LIKE: LIKE folds ASCII case, and the shape check already
    #    leaves only id characters, so no escaping is needed either.
    prefix = "substr(m.id, 1, ?) = ?"
    rows = con.execute(
        f"SELECT m.id, m.thread_id, m.subject FROM messages m "
        f"WHERE {prefix} AND {_VISIBLE} ORDER BY m.accepted_at, m.id "
        f"LIMIT {AMBIGUOUS_LIST_MAX + 1}",
        (len(target), target, *_vis(node)),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0]), None
    if len(rows) > 1:
        n = con.execute(
            f"SELECT count(*) FROM messages m WHERE {prefix} AND {_VISIBLE}",
            (len(target), target, *_vis(node)),
        ).fetchone()[0]
        return None, _ambiguous(target, [r["id"] for r in rows[:AMBIGUOUS_LIST_MAX]], n)
    # Not visible. Whether it exists at all goes to the daemon log only.
    exists = con.execute(
        "SELECT 1 FROM messages WHERE substr(id, 1, ?) = ? LIMIT 1",
        (len(target), target),
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
        (row["reply_to"], *_vis(node)),
    ).fetchone()
    return dict(parent) if parent else {"id": row["reply_to"]}


def _tac_label(con, tac_id):
    """The tac's name when the ledger has one, else its identifier. An older
    ledger may lack the name column, so the row is read whole."""
    row = con.execute("SELECT * FROM tacs WHERE tac_id = ?", (tac_id,)).fetchone()
    if row is None:
        return tac_id
    row = dict(row)
    return row.get("name") or row.get("label") or tac_id


def thread_view(con, node, target):
    """One thread as `node` sees it, oldest first, each row with its depth.

    Returns (rows, None) or (None, refusal). A row: id, sender_id, subject,
    accepted_at, reply_to, depth, to, parent_hidden.
    `to` is {"tac": name} for tac mail, {"broadcast": True} for an announcement,
    and {"nodes": [...]} for direct mail.
    A reply whose parent `node` cannot see is drawn at depth 0 with
    parent_hidden=True. Its reply_to still holds the parent's id (as /reopen
    gives it); the parent's subject and body never appear.
    """
    root, refusal = resolve_reply_target(con, node, target)
    if refusal is not None:
        return None, refusal
    rows = [
        dict(r)
        for r in con.execute(
            f"SELECT m.id, m.sender_id, m.subject, m.accepted_at, m.reply_to, "
            f"m.tac_id, m.broadcast "
            f"FROM messages m "
            f"WHERE m.thread_id = ? AND {_VISIBLE} "
            f"ORDER BY m.accepted_at, m.id",
            (root["thread_id"], *_vis(node)),
        )
    ]
    seen = {r["id"]: r for r in rows}
    children = {}
    tops = []
    for r in rows:
        tac_id, bcast = r.pop("tac_id"), r.pop("broadcast")
        if tac_id:
            r["to"] = {"tac": _tac_label(con, tac_id)}
        elif bcast:
            r["to"] = {"broadcast": True}
        else:
            r["to"] = {
                "nodes": [
                    d["recipient_id"]
                    for d in con.execute(
                        "SELECT recipient_id FROM deliveries WHERE message_id = ? ORDER BY recipient_id",
                        (r["id"],),
                    )
                ]
            }
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
