"""Send refusals: size limits, payload checks and machine-readable codes.

A refusal is still the sentence `bus_send` has always returned, so callers that
compare or print it keep working. The sentence carries a code, a message, details
and a retry hint, which the daemon adds to its HTTP 400 response next to `error`.

Sizes are UTF-8 bytes of the decoded string. Nothing is normalized or trimmed:
the bytes that were sent are the bytes that are counted and stored.
"""

MAX_BODY_BYTES = 65536
MAX_SUBJECT_BYTES = 1024
UNIT = "utf8_bytes"
# The whole HTTP request body, before JSON decoding. The largest valid /send (65,536 B of
# control characters escaped as \u00XX, plus a 1,024 B subject) is about 401 KB.
MAX_REQUEST_BYTES = 524288
# 🔴 Largest declared length converted to a number and reported as details.bytes: a signed
#    64-bit integer, so the value fits a Java long. A longer digit string is still a valid
#    length over the limit, but it never reaches int(): Python 3.10+ refuses to convert more
#    than 4,300 digits, and that raised inside the handler (no response).
MAX_REPORTED_LENGTH = 2**63 - 1

# 🔴 Existing sentence for a missing or oversized body. Older clients and scripts
#    match on it, so both codes keep it and only add fields.
BODY_MISSING_OR_TOO_LARGE = "body is missing or too large"


class SendRefusal(str):
    """The refusal sentence, with code, message, details and retry attached."""

    def __new__(cls, error, code, message, details, retry):
        self = super().__new__(cls, error)
        self.code = code
        self.message = message
        self.details = details
        self.retry = retry
        return self

    def fields(self):
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retry": self.retry,
        }


def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_invalid(field, value):
    return SendRefusal(
        f"{field} must be a string",
        "FIELD_TYPE_INVALID",
        f"{field} must be a JSON string; received {_json_type(value)}",
        {"field": field, "received": _json_type(value)},
        "never",
    )


def _encoding_invalid(field):
    return SendRefusal(
        f"{field} is not valid UTF-8 text",
        "FIELD_ENCODING_INVALID",
        f"{field} contains characters that cannot be encoded as UTF-8, such as an unpaired surrogate",
        {"field": field},
        "never",
    )


def _encodes(value):
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _too_large(error, field, size, limit):
    return SendRefusal(
        error,
        "BODY_TOO_LARGE" if field == "body" else "SUBJECT_TOO_LARGE",
        f"{field} is {size} UTF-8 bytes; the limit is {limit}",
        {"field": field, "bytes": size, "limit": limit, "unit": UNIT},
        "never",
    )


def check_send_payload(subject, body, other_text=None):
    """Return the first refusal for this subject and body, or None.

    Order: type (body, then subject), encoding (body, subject, then the other text
    fields in other_text), then body missing, empty, too large, then subject empty,
    too large. A body of null counts as missing, as it always has. An omitted subject
    arrives here as "" and is refused as empty.

    other_text: {field name: value} for the remaining /send fields. Only their
    encoding is judged here; a string or the strings inside a list are checked.
    """
    if body is not None and not isinstance(body, str):
        return _type_invalid("body", body)
    if not isinstance(subject, str):
        return _type_invalid("subject", subject)
    # 🔴 JSON can carry an unpaired surrogate (\ud800) that decodes to a str Python cannot
    #    encode. Sizes below and storage both need the UTF-8 bytes, so this comes first.
    if body is not None and not _encodes(body):
        return _encoding_invalid("body")
    if not _encodes(subject):
        return _encoding_invalid("subject")
    for field, value in (other_text or {}).items():
        values = value if isinstance(value, list) else [value]
        if any(isinstance(v, str) and not _encodes(v) for v in values):
            return _encoding_invalid(field)
    if body is None:
        return SendRefusal(
            BODY_MISSING_OR_TOO_LARGE,
            "BODY_MISSING",
            "body is missing",
            {"field": "body"},
            "never",
        )
    # 🔴 Blank means str.isspace: the same set the MCP adapter's strip() removes.
    #    Zero-width and direction marks are not blank. The body is only judged here,
    #    never trimmed.
    if body == "" or body.isspace():
        return SendRefusal(
            "body is empty or whitespace only",
            "BODY_EMPTY",
            "body must contain at least one non-whitespace character",
            {"field": "body"},
            "never",
        )
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        return _too_large(BODY_MISSING_OR_TOO_LARGE, "body", size, MAX_BODY_BYTES)
    # 🔴 Same blank set as the body. An omitted subject arrives as "" and lands here too.
    if subject == "" or subject.isspace():
        return SendRefusal(
            "subject is empty or whitespace only",
            "SUBJECT_EMPTY",
            "subject must contain at least one non-whitespace character",
            {"field": "subject"},
            "never",
        )
    size = len(subject.encode("utf-8"))
    if size > MAX_SUBJECT_BYTES:
        return _too_large("subject is too large", "subject", size, MAX_SUBJECT_BYTES)
    return None


def _one_length(value):
    """(valid, size, digits) for one Content-Length value; digits identifies the value."""
    # 🔴 HTTP optional whitespace is SP and HTAB. strip() would also remove U+00A0, U+0085,
    #    VT and FF, and accept a value such as "12\xa0" as 12.
    raw = (value or "").strip(" \t")
    if raw == "":
        return True, 0, "0"
    # 🔴 HTTP allows digits only. int() alone would also take "+12", "1_000" and non-ASCII digits.
    if not (raw.isascii() and raw.isdigit()):
        return False, None, None
    digits = raw.lstrip("0") or "0"
    if len(digits) > len(str(MAX_REPORTED_LENGTH)):
        return True, None, digits
    size = int(digits)
    return True, size if size <= MAX_REPORTED_LENGTH else None, digits


def declared_length(content_length):
    """(valid, size) for the Content-Length header.

    content_length: the header value, or a list of every value when the header may repeat.
    valid: each value is ASCII digits only once surrounding SP and HTAB are removed; leading
    zeros are allowed and the value is decimal. Repeated values must all be the same number.
    A missing or empty header is valid, size 0.
    size: the value as an int, or None when invalid or larger than MAX_REPORTED_LENGTH.
    """
    values = content_length if isinstance(content_length, (list, tuple)) else [content_length]
    parsed = [_one_length(v) for v in values] or [_one_length(None)]
    # 🔴 Two different lengths leave the body boundary ambiguous (RFC 9112 6.3): refuse, do not pick one.
    if not all(valid for valid, _, _ in parsed) or len({digits for _, _, digits in parsed}) > 1:
        return False, None
    return True, parsed[0][1]


def request_length_refusal(content_length):
    """(HTTP status, response body) for a request whose declared length is refused, or None.

    Judged from the Content-Length header alone, before the signature is checked.
    A missing header is length 0, as the daemon has always read it.
    """
    valid, size = declared_length(content_length)
    if not valid:
        return 400, {
            "error": "invalid Content-Length",
            "code": "REQUEST_LENGTH_INVALID",
            "message": "Content-Length must be a non-negative integer",
            "details": {},
            "retry": "never",
        }
    if size is None:
        return 413, {
            "error": "request body too large",
            "code": "REQUEST_TOO_LARGE",
            "message": f"request body is more than {MAX_REPORTED_LENGTH} bytes; the limit is {MAX_REQUEST_BYTES}",
            "details": {"limit": MAX_REQUEST_BYTES, "unit": "bytes"},
            "retry": "never",
        }
    if size > MAX_REQUEST_BYTES:
        return 413, {
            "error": "request body too large",
            "code": "REQUEST_TOO_LARGE",
            "message": f"request body is {size} bytes; the limit is {MAX_REQUEST_BYTES}",
            "details": {"bytes": size, "limit": MAX_REQUEST_BYTES, "unit": "bytes"},
            "retry": "never",
        }
    return None


def unread_blocked_tac(sentence, tac_id, unread):
    return SendRefusal(
        sentence,
        "UNREAD_BLOCKED",
        "open the unread messages in this tac, then send",
        {"scope": "tac", "tac": tac_id, "unread": unread},
        "after_condition",
    )


def unread_blocked_dm(sentence, counts):
    """counts: [(recipient, unread messages from that recipient to the sender)]."""
    return SendRefusal(
        sentence,
        "UNREAD_BLOCKED",
        "open the unread messages from these recipients, then send",
        {"scope": "dm", "recipients": [{"node": r, "unread": n} for r, n in counts]},
        "after_condition",
    )
