"""tg.cap(): a cut Telegram can still parse.

Telegram refuses a message over 4096 characters, so something has to cut a long one — and it
parses what it gets as HTML, so a message that ENDS with `<code>` still open is refused WHOLE
("can't parse entities"). That is the worst possible outcome: the truncation deletes the alert
it was supposed to shorten, and the operator hears nothing at all about money parked for them.

The old guard looked at the last `<` and asked whether a `>` followed it — which `…<code>`
satisfies happily. The guard is on the PAIR: after the cut, every tag still open is closed, or
dropped along with the text it opened when its opener no longer fits.
"""

from __future__ import annotations

from pgasme import tg

TAGS = ("b", "i", "code", "pre", "a")


def balanced(text: str) -> bool:
    """Every opener has its closer — counted the way Telegram's parser cares about."""
    return all(text.count(f"<{t}>") == text.count(f"</{t}>") for t in TAGS)


def test_a_short_message_is_handed_over_untouched():
    assert tg.cap("HELD: <code>dep1</code> nothing to cut") == (
        "HELD: <code>dep1</code> nothing to cut"
    )


def test_a_cut_that_lands_after_a_code_opener_never_leaves_it_open():
    text = "A" * 3480 + "<code>" + "b" * 200 + "</code>"
    out = tg.cap(text)
    assert len(out) <= tg.MAX_CHARS
    assert out.endswith(tg.CUT_MARKER)
    assert out.count("<code>") == out.count("</code>")
    assert balanced(out)


def test_the_same_for_a_bold_opener():
    text = "A" * 3480 + "<b>" + "b" * 200 + "</b>"
    out = tg.cap(text)
    assert len(out) <= tg.MAX_CHARS
    assert out.endswith(tg.CUT_MARKER)
    assert out.count("<b>") == out.count("</b>")
    assert balanced(out)


def test_a_message_wrapped_in_a_tag_is_closed_and_not_annihilated():
    """Backing up to before the opener is one legal repair, but it must not be the only one: a
    whole alert inside one `<b>` would be cut back to nothing, and an empty page is a lost one."""
    text = "<b>" + "word " * 900 + "</b>"
    out = tg.cap(text)
    assert len(out) <= tg.MAX_CHARS
    assert out.endswith("</b>" + tg.CUT_MARKER)
    assert balanced(out) and out.count("word") > 600  # the message survived the repair


def test_nested_tags_are_closed_innermost_first():
    text = "<b>the plan: <code>" + "y " * 3000 + "</code></b>"
    out = tg.cap(text)
    assert len(out) <= tg.MAX_CHARS
    assert out.endswith("</code></b>" + tg.CUT_MARKER)  # reverse order, or Telegram refuses it
    assert balanced(out)


def test_unclosed_names_exactly_what_a_head_still_owes():
    assert tg.unclosed("plain text with no tags at all") == ""
    assert tg.unclosed("<b>done</b>") == ""
    assert tg.unclosed("<b>half") == "</b>"
    assert tg.unclosed("<b>a <code>b") == "</code></b>"
    assert tg.unclosed("<b>a <code>b</code>") == "</b>"
    assert tg.unclosed('<a href="https://pgas.me">link') == "</a>"
    assert tg.unclosed("<div>not a telegram tag") == ""  # nothing Telegram parses, nothing owed


def test_the_cut_still_never_hands_over_half_a_command_or_half_an_entity():
    """The three older repairs are unchanged — a cut inside a `code span`, mid-word, or inside an
    entity is how a runbook command reached the operator in half."""
    text = "run this: " + "context. " * 500 + "`python -m pgasme.beam replan-shield --apply`"
    out = tg.cap(text)
    assert out.count("`") % 2 == 0 and "`python" not in out
    entity = "x" * 3480 + " &amp; " + "y" * 200
    assert "&" not in tg.cap(entity).replace("&amp;", "")
