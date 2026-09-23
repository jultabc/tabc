"""The refusals a tac name or identifier produces.

Four codes, and they carry the same shape as every other refusal: error sentence,
code, message, details, retry. The sentences are what the server already answers, so
a caller reading the sentence sees no change; the code is what a program reads.
"""

from .send_refusal import SendRefusal


def id_invalid(given=None):
    """🔴 Designation is by identifier. A name is not one: a name can change, and a
    designation that followed a rename would name a different tac tomorrow.

    🔴 The message says how to find the identifier, because typing a name here is the
    common mistake — every string in our documents and letters is a name. The details
    do not echo what arrived (woo).
    """
    return SendRefusal(
        "tac must be a canonical UUID; a name is not a designation",
        "TAC_ID_INVALID",
        "address a tac by its UUID, written lowercase with hyphens; look one up by name "
        "with `tabc tac search`",
        {"field": "tac"},
        "never",
    )


def not_found(given, sentence=None):
    return SendRefusal(
        sentence or f"no such tac: {given}",
        "TAC_NOT_FOUND",
        "no tac carries this identifier, name or previous identifier",
        {"field": "tac", "given": given},
        "never",
    )


def name_taken(name, held_by=None, reason="name"):
    """reason: 'name' when another tac holds it now, 'legacy' when it was one's identifier."""
    message = ("another tac holds this name"
               if reason == "name" else
               "this name was the previous identifier of another tac, and resolving it "
               "would be ambiguous")
    details = {"field": "name", "name": name, "held_as": reason}
    if held_by is not None:
        details["tac_id"] = held_by
    return SendRefusal(f"tac name already taken: {name}", "TAC_NAME_TAKEN", message, details, "never")


def name_invalid(name, reason):
    return SendRefusal(
        reason,
        "TAC_NAME_INVALID",
        reason,
        {"field": "name", "name": name if isinstance(name, str) else None},
        "never",
    )


def not_converted():
    """🔴 Also the state after a rollback: the columns are there and a row has no UUID."""
    from .tac_store import NOT_CONVERTED

    return SendRefusal(
        NOT_CONVERTED,
        "TAC_NOT_CONVERTED",
        "this ledger still holds tacs without a UUID; run tabus.tac_migration, then retry",
        {"field": "tac"},
        "after_condition",
    )
