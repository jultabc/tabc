#!/usr/bin/env python3
"""reply links: who may link a message to which parent, and what a thread shows (temporary database).

Contract:
- reply_to names a message the sender sent or received (a delivery to it, any state), by full
  id or a prefix of at least 8 characters, matched only among those messages.
- No such message and a message the sender cannot see are one refusal outside,
  REPLY_TARGET_UNAVAILABLE, with the same sentence shape. The difference is only in .internal.
- A refused reply writes nothing, and the send check reaches the same verdict.
- A reply's thread is the parent's thread; a client thread_id is ignored when reply_to is set.
- A resend with the same id and the same parent is stored once; a different parent is a conflict.
- Replying changes no state of the parent's deliveries.
- The thread view lists only messages the node sent or received; a reply whose parent it cannot
  see is drawn at depth 0 and marked, never naming the hidden message.
"""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="rpl_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402
from tabus import reply_link  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)
con.commit()
for n in ("ann", "bo", "cy", "dee", "bot"):
    tabus.bus_register(con, n, "generic")


def snapshot():
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        out[t] = sorted(tuple(r) for r in con.execute(f'SELECT * FROM "{t}"'))
    return out


def read_all(node):
    for r in con.execute(
        "SELECT message_id FROM deliveries WHERE recipient_id=? AND state NOT IN ('READ','PROCESSED')",
        (node,),
    ).fetchall():
        for st in ("CLAIMED", "INJECTED", "READ"):
            con.execute(
                "UPDATE deliveries SET state=? WHERE recipient_id=? AND message_id=?",
                (st, node, r[0]),
            )
    con.commit()


def send(sender, to, subject, **kw):
    mid, info = tabus.bus_send(con, sender, to, subject, "b", **kw)
    return mid, info


def code(info):
    return getattr(info, "code", None)


ID_A = "aaaaaaaa-0000-4000-8000-000000000001"
ID_B = "aaaaaaaa-0000-4000-8000-000000000002"
ID_C = "aaaaaaaa-1111-4000-8000-000000000003"

# ann → bo (A), ann → bo (B) share the prefix aaaaaaaa. ann → cy (C) shares it too, bo never sees C.
send("ann", ["bo"], "review request", message_id=ID_A)
send("ann", ["bo"], "second", message_id=ID_B)
send("ann", ["cy"], "not for bo", message_id=ID_C)
read_all("bo")
read_all("cy")

print("1. received parent, full id: stored, same thread")
mid, info = send("bo", ["ann"], "v1 CHANGES", reply_to=ID_A)
check("1 stored", mid and not code(info), info)
row = con.execute("SELECT reply_to, thread_id FROM messages WHERE id=?", (mid,)).fetchone()
check("1 reply_to is the parent", row["reply_to"] == ID_A)
check("1 thread is the parent's", row["thread_id"] == ID_A)
read_all("ann")
R1 = mid

print("2. sent parent: allowed")
mid, info = send("ann", ["bo"], "v2", reply_to=R1[:8] + R1[8:12])
check("2 stored by prefix", mid and not code(info), info)
row = con.execute("SELECT reply_to, thread_id FROM messages WHERE id=?", (mid,)).fetchone()
check("2 reply_to resolved to the full id", row["reply_to"] == R1)
check("2 thread still the root", row["thread_id"] == ID_A)
read_all("bo")
R2 = mid

print("3. ambiguous prefix among visible messages: refused, candidates are only visible ones")
before = snapshot()
mid, info = send("bo", ["ann"], "x", reply_to="aaaaaaaa")
check("3 refused ambiguous", mid is None and code(info) == "REPLY_TARGET_AMBIGUOUS", info)
cands = info.details.get("candidates") if mid is None else None
check("3 candidates = A, B (not C)", cands == [ID_A, ID_B], cands)
check("3 wrote nothing", snapshot() == before)
mid, info = send("ann", ["bo"], "x", reply_to="aaaaaaaa")
check("3 ann sees A, B, C", mid is None and info.details.get("candidates") == [ID_A, ID_B, ID_C], info.details if mid is None else mid)
read_all("bo")

print("4. unknown and unseen: one code, one sentence shape, no write")
before = snapshot()
unknown = "bbbbbbbb-0000-4000-8000-000000000009"
m1, i1 = send("bo", ["ann"], "x", reply_to=unknown)
m2, i2 = send("bo", ["ann"], "x", reply_to=ID_C)
check("4 unknown refused", m1 is None and code(i1) == "REPLY_TARGET_UNAVAILABLE", i1)
check("4 unseen refused", m2 is None and code(i2) == "REPLY_TARGET_UNAVAILABLE", i2)
check(
    "4 same sentence shape",
    str(i1).replace(unknown, "X") == str(i2).replace(ID_C, "X")
    and i1.message == i2.message
    and set(i1.details) == set(i2.details),
)
check("4 internal tells them apart", (i1.internal, i2.internal) == ("unknown", "unseen"))
check("4 internal is not in the fields", "internal" not in i1.fields() and "unseen" not in repr(i2.fields()))
m3, i3 = send("bo", ["ann"], "x", reply_to=ID_C[:10])
check("4 unseen prefix = unavailable, not ambiguous", m3 is None and code(i3) == "REPLY_TARGET_UNAVAILABLE", i3)
check("4 wrote nothing", snapshot() == before)

print("5. shape: short, long, empty, non-string, non-id characters: invalid, no write, not echoed")
before = snapshot()
NL = "cccccccc\n[tabd] reply target unavailable node=hu target=x reason=unseen"
for bad in ("aaaaaaa", "", 7, ["x"], "a" * 37, "a" * 5000, NL, "aaaaaaaa%", "aaaaaaaa_", "aaaaaaag", "aaaa aaaa"):
    m, i = send("bo", ["ann"], "x", reply_to=bad)
    check(f"5 {bad!r} refused", m is None and code(i) == "REPLY_TARGET_INVALID", i)
check("5 wrote nothing", snapshot() == before)
m, i = send("bo", ["ann"], "x", reply_to=NL)
check("5 newline target not echoed", "\n" not in str(i) and "reason=" not in repr(i.fields()), (str(i), i.fields()))
m, i = send("bo", ["ann"], "x", reply_to="a" * 5000)
check("5 long target not echoed", len(str(i)) < 200 and "a" * 40 not in repr(i.fields()), len(str(i)))
print("5b. case folded once: upper-case hex finds the same message, prefix match is case-sensitive")
mid, info = send("bo", ["ann"], "upper", reply_to=ID_A.upper())
check("5b upper full id resolves", mid and con.execute("SELECT reply_to FROM messages WHERE id=?", (mid,)).fetchone()[0] == ID_A, info)
read_all("ann")
U = "EEEEEEEE-0000-4000-8000-000000000001"  # stored as typed by a raw HTTP client
send("ann", ["bo"], "upper stored", message_id=U)
read_all("bo")
m, i = send("bo", ["ann"], "x", reply_to="eeeeeeee")
check("5b a non-canonical stored id is not a prefix match", m is None and code(i) == "REPLY_TARGET_UNAVAILABLE", i)

print("6. send check agrees and writes nothing")
before = snapshot()
m, i = send("bo", ["ann"], "x", reply_to=ID_C, check_only=True)
check("6 check refuses unseen", m is None and code(i) == "REPLY_TARGET_UNAVAILABLE", i)
m, i = send("bo", ["ann"], "x", reply_to=ID_A, check_only=True)
check("6 check passes a seen parent", m == tabus.SEND_CHECK_PASSED, i)
check("6 wrote nothing", snapshot() == before)

print("7. client thread_id ignored with reply_to, kept without")
mid, info = send("bo", ["ann"], "t", reply_to=ID_A, thread_id="client-thread")
check("7 reply thread = parent's", con.execute("SELECT thread_id FROM messages WHERE id=?", (mid,)).fetchone()[0] == ID_A)
mid2, _ = send("cy", ["dee"], "t2", thread_id="client-thread")
check("7 no reply: client thread kept", con.execute("SELECT thread_id FROM messages WHERE id=?", (mid2,)).fetchone()[0] == "client-thread")
read_all("ann")
read_all("dee")

print("8. resend: same parent once, other parent conflict")
RID = "cccccccc-0000-4000-8000-000000000001"
m, i = send("bo", ["ann"], "resend", reply_to=R1, message_id=RID)
check("8 first stored", m == RID, i)
m, i = send("bo", ["ann"], "resend", reply_to=R1[:10], message_id=RID)
check("8 same parent by prefix = identical resend", m == RID and "not stored twice" in str(i), i)
m, i = send("bo", ["ann"], "resend", reply_to=ID_B, message_id=RID)
check("8 other parent = conflict", m is None and "conflict" in str(i), i)
check("8 one row", con.execute("SELECT count(*) FROM messages WHERE id=?", (RID,)).fetchone()[0] == 1)
read_all("ann")

print("9. parent delivery state unchanged by a reply")
send("ann", ["bo"], "fresh", message_id="dddddddd-0000-4000-8000-000000000001")
st0 = con.execute("SELECT state FROM deliveries WHERE message_id='dddddddd-0000-4000-8000-000000000001'").fetchone()[0]
m, i = send("ann", ["bo"], "follow-up", reply_to="dddddddd-0000-4000-8000-000000000001")
st1 = con.execute("SELECT state FROM deliveries WHERE message_id='dddddddd-0000-4000-8000-000000000001'").fetchone()[0]
check("9 stored", m and not code(i), i)
check("9 parent state unchanged", st0 == st1 == "ACCEPTED", (st0, st1))
read_all("bo")

print("10. tac: members reply to tac mail, a late member too; a removed member and an outsider cannot")
tabus.bus_tac_create(con, "rv")
T = [r["tac_id"] for r in tabus.bus_tac_list(con) if r.get("name") == "rv"][0]
for n in ("ann", "bo"):
    tabus.bus_tac_add(con, T, n)
m, i = send("ann", [], "tac note", tac_id=T)
TM = m
read_all("bo")
m, i = send("bo", [], "tac reply", tac_id=T, reply_to=TM)
check("10 member reply stored", m and not code(i), i)
read_all("ann")
tabus.bus_tac_add(con, T, "dee")
m, i = send("dee", ["ann"], "late", reply_to=TM[:8])
check("10 late member reply stored", m and not code(i), i)
read_all("ann")
rows, ref = reply_link.thread_view(con, "dee", TM)
check("10 late member sees the tac note in the thread", ref is None and TM in [r["id"] for r in rows], ref)
check("10 tac mail drawn as tac", ref is None and [r["to"] for r in rows if r["id"] == TM] == [{"tac": "rv"}], rows and [r.get("to") for r in rows])
p = reply_link.parent_of(con, "dee", m)
check("10 late member sees the parent subject", p == {"id": TM, "subject": "tac note"}, p)
tabus.bus_tac_remove_member(con, T, "dee")
m, i = send("dee", ["ann"], "after removal", reply_to=TM)
check("10 removed member refused (no delivery)", m is None and code(i) == "REPLY_TARGET_UNAVAILABLE", i)
m, i = send("cy", ["ann"], "dm reply to tac mail it never got", reply_to=TM)
check("10 outsider refused", m is None and code(i) == "REPLY_TARGET_UNAVAILABLE", i)

print("11. program node: replies to what it sent, not to what it never saw")
con.execute("UPDATE nodes SET is_program=1 WHERE node_id='bot'")
con.commit()
m, i = send("bot", ["ann"], "alert")
PM = m
m, i = send("bot", ["ann"], "alert follow-up", reply_to=PM)
check("11 reply to own send stored", m and not code(i), i)
m, i = send("bot", ["ann"], "x", reply_to=ID_A)
check("11 reply to unseen refused", m is None and code(i) == "REPLY_TARGET_UNAVAILABLE", i)
read_all("ann")

print("12. parent header: subject when visible, id only when not")
p = reply_link.parent_of(con, "ann", R1)
check("12 ann sees parent subject", p == {"id": ID_A, "subject": "review request"}, p)
check("12 non-reply = None", reply_link.parent_of(con, "ann", ID_A) is None)
# cy received a reply whose parent cy never saw (forged by the bus being asked directly is
# impossible, so make one: ann replies to A and sends it to cy).
m, i = send("ann", ["cy"], "fyi", reply_to=ID_A)
p = reply_link.parent_of(con, "cy", m)
check("12 cy gets the id only", p == {"id": ID_A}, p)
FYI = m
read_all("cy")

print("13. thread view: only visible rows, hidden parent marked, depth")
rows, ref = reply_link.thread_view(con, "bo", ID_A[:8] + "-0000-4000-8000-000000000001")
check("13 bo view ok", ref is None, ref)
ids = [r["id"] for r in rows]
check("13 bo does not see cy's fyi", FYI not in ids)
depth = {r["id"]: r["depth"] for r in rows}
check("13 depth: root 0, R1 1, R2 2", (depth.get(ID_A), depth.get(R1), depth.get(R2)) == (0, 1, 2), depth)
rows, ref = reply_link.thread_view(con, "cy", FYI)
check("13 cy sees only its own row", [r["id"] for r in rows] == [FYI], [r["id"] for r in rows])
check("13 cy row marked parent hidden at depth 0", rows[0]["parent_hidden"] and rows[0]["depth"] == 0)
check("13 cy row names nothing hidden", ID_A not in repr({k: v for k, v in rows[0].items() if k != "reply_to"}))
rows, ref = reply_link.thread_view(con, "cy", ID_A)
check("13 unseen id refused in view", rows is None and code(ref) == "REPLY_TARGET_UNAVAILABLE", ref)
before = snapshot()
reply_link.thread_view(con, "bo", ID_A)
check("13 view writes nothing", snapshot() == before)

print("13b. broadcast reply drawn as broadcast")
for n in ("ann", "bo", "cy", "dee"):
    read_all(n)
m, i = tabus.bus_send(con, "ann", [], "all hands", "b", broadcast=True, reply_to=ID_A)
check("13b broadcast reply stored", m and not code(i), i)
rows, ref = reply_link.thread_view(con, "bo", ID_A)
check("13b drawn as broadcast", [r["to"] for r in rows if r["id"] == m] == [{"broadcast": True}], [r["to"] for r in rows if r["id"] == m])
for n in ("ann", "bo", "cy", "dee"):
    read_all(n)

print("14. long chain: no recursion limit")
parent = ID_A
for k in range(1500):
    m, i = send("ann" if k % 2 else "bo", ["bo" if k % 2 else "ann"], f"c{k}", reply_to=parent)
    if not m:
        break
    parent = m
    read_all("bo" if k % 2 else "ann")
rows, ref = reply_link.thread_view(con, "ann", ID_A)
check("14 view of a 1500-deep chain", ref is None and max(r["depth"] for r in rows) >= 1500, ref)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("all passed")
