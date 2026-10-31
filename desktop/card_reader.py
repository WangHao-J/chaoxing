"""Read chapter-card JSON without altering resource titles or guessing task IDs."""
import json
import re


def flag_is_true(value):
    """Accept explicit true values used by card APIs; strings like 'false' stay false."""
    if value is True:
        return True
    if type(value) is int:
        return value == 1
    return isinstance(value, str) and value.strip().lower() in ("true", "1")


def has_card_data_assignment(html):
    """Distinguish concrete card JSON from the template's empty initializer.

    Unused card slots contain mArg = '' and $.parseJSON(mArg) scaffolding.
    These do not imply missing additional tasks after a valid media card.
    Literal objects/arrays that cannot be decoded remain a diagnostic failure.
    """
    return isinstance(html, str) and re.search(r"\bmArg\s*=\s*[\[{]", html) is not None


def parse_course_card(html, original_extract, card_processor, original_decode=None):
    """Return (jobs, info, recognized), retaining the existing jobs/info interface.

    recognized means a structurally valid mArg object (or the existing not-open
    message) was read. It does not prove an empty job list means a completed
    chapter. Callers must retain that distinction before reporting progress.
    Unrecognized pages return recognized=False so a caller can preserve the
    chapter for another check. An optional legacy decoder can retain compatible
    jobs/info results; it cannot turn that diagnostic into recognized=True.
    """
    def fallback():
        if original_decode is None:
            return [], {}, False
        try:
            jobs, info = original_decode(html)
            if isinstance(jobs, list) and isinstance(info, dict):
                return jobs, info, False
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            pass
        return [], {}, False

    if not isinstance(html, str):
        return fallback()
    if "章节未开放" in html:
        return [], {"notOpen": True}, True

    # The official template initializes mArg to an empty string before assigning
    # the actual object. Read direct assignments in order, skipping strings and
    # the later $.parseJSON(mArg) conversion; never execute JavaScript.
    data = None
    cursor = 0
    while True:
        assignment = re.search(r"\bmArg\s*=\s*", html[cursor:])
        if assignment is None:
            break
        start = cursor + assignment.end()
        cursor = start
        try:
            value, end = json.JSONDecoder().raw_decode(html, start)
            cursor = end  # Do not find fake assignments inside JSON strings.
        except ValueError:
            if html[start:start + 1] in ("{", "["):
                data = None
            continue
        if isinstance(value, str) or value is None:
            continue
        # Arrays/scalars are not task-card objects and cannot prove an empty page.
        data = value if isinstance(value, dict) else None
    if data is None:
        return fallback()
    try:
        cards = data.get("attachments", [])
        defaults = data.get("defaults")
        if not isinstance(cards, list) or any(not isinstance(card, dict) for card in cards):
            return fallback()
        if defaults is not None and not isinstance(defaults, dict):
            return fallback()
        info = original_extract(data)
        jobs = card_processor(cards)
        if not isinstance(info, dict) or not isinstance(jobs, list):
            return fallback()
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return fallback()
    return jobs, info, True
