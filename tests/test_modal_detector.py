"""Tests for the pure-function startup modal detector.

The detector is the most safety-critical piece of the auto-skip path: a
false positive types a stray digit into Claude, a false negative leaves
the session wedged forever. So we test it with realistic-shaped fixtures
covering the happy path, ANSI noise, and a few negative cases.
"""
from clawx import detect_startup_modal, detect_compact_event, detect_rate_limit_modal


def test_returns_none_on_empty():
    assert detect_startup_modal(b"") is None


def test_returns_none_on_plain_text():
    assert detect_startup_modal(b"hello world\n> ") is None


def test_returns_none_when_keyword_missing():
    # Numbered list but no compact/summarize keyword → not a modal we care about.
    buf = b"Pick one:\n  1. apples\n  2. oranges\n  3. pears\n"
    assert detect_startup_modal(buf) is None


def test_returns_none_with_keyword_but_only_one_option():
    # "compact" appears but no menu — could be normal log line.
    buf = b"Conversation will auto-compact soon.\n"
    assert detect_startup_modal(buf) is None


def test_detects_three_option_compact_prompt():
    buf = (
        b"Context is getting full. Auto-compact options:\n"
        b"  1. Compact now\n"
        b"  2. Summarize and continue\n"
        b"  3. Leave alone\n"
        b"> "
    )
    # Should pick the highest option (the conventional skip slot).
    assert detect_startup_modal(buf) == 3


def test_detects_two_option_prompt():
    buf = (
        b"Auto-compact this conversation?\n"
        b"  1. Yes\n"
        b"  2. No\n"
    )
    assert detect_startup_modal(buf) == 2


def test_strips_ansi_escape_sequences():
    # Real Claude TUI output is colored — make sure ANSI doesn't fool us.
    ansi = b"\x1b[1;36m"
    reset = b"\x1b[0m"
    buf = (
        ansi + b"Auto-compact?" + reset + b"\n"
        b"  " + ansi + b"1." + reset + b" Compact\n"
        b"  " + ansi + b"2." + reset + b" Summarize\n"
        b"  " + ansi + b"3." + reset + b" Skip\n"
    )
    assert detect_startup_modal(buf) == 3


def test_handles_paren_style_options():
    buf = (
        b"Context approaching limit - summarize?\n"
        b"  1) yes\n"
        b"  2) no\n"
    )
    assert detect_startup_modal(buf) == 2


def test_case_insensitive_keyword():
    buf = (
        b"AUTO-COMPACT REQUIRED\n"
        b"  1. now\n"
        b"  2. later\n"
    )
    assert detect_startup_modal(buf) == 2


def test_handles_invalid_utf8_gracefully():
    # PTY can emit partial UTF-8 mid-buffer; should not crash.
    buf = b"\xff\xfe summarize?\n  1. a\n  2. b\n"
    # Either detects or returns None — must not raise.
    result = detect_startup_modal(buf)
    assert result in (None, 2)


# ── Compact event detector tests ─────────────────────────────────

def test_compact_returns_none_on_empty():
    assert detect_compact_event(b"") is None


def test_compact_returns_none_on_plain_text():
    assert detect_compact_event(b"hello world\n> ") is None


def test_compact_detects_standard_message():
    buf = b"\xe2\x9c\xbb Conversation compacted (ctrl+o for history)\n"
    assert detect_compact_event(buf) is True


def test_compact_detects_with_ansi():
    # Real PTY output: grey text with ANSI color codes
    buf = (
        b"\x1b[38;5;246m\xe2\x9c\xbb\x1b[1C"
        b"Conversation\x1b[1Ccompacted\x1b[1C"
        b"(ctrl+o\x1b[1Cfor\x1b[1Chistory)\x1b[39m\n"
    )
    assert detect_compact_event(buf) is True


def test_compact_not_triggered_by_rate_limit():
    buf = (
        b"You've hit your limit\n"
        b"/rate-limit-options\n"
        b"  1. Stop and wait for limit to reset\n"
        b"  2. Upgrade your plan\n"
    )
    assert detect_compact_event(buf) is None


def test_compact_case_insensitive():
    buf = b"CONVERSATION COMPACTED\n"
    assert detect_compact_event(buf) is True


def test_compact_not_triggered_by_word_compact_alone():
    # The word "compact" alone shouldn't trigger — needs "conversation compacted"
    buf = b"Let me compact these files into an archive\n"
    assert detect_compact_event(buf) is None


# ── Rate-limit modal detector tests ──────────────────────────────

def test_rate_limit_returns_none_on_empty():
    assert detect_rate_limit_modal(b"") is None


def test_rate_limit_returns_none_on_plain_text():
    assert detect_rate_limit_modal(b"hello world\n> ") is None


def test_rate_limit_detects_standard_prompt():
    buf = (
        b"You've hit your limit \xc2\xb7 resets 12am (Asia/Taipei)\n"
        b"/rate-limit-options\n\n"
        b"What do you want to do?\n\n"
        b"> 1. Stop and wait for limit to reset\n"
        b"  2. Upgrade your plan\n\n"
        b"Enter to confirm \xc2\xb7 Esc to cancel\n"
    )
    # Should always pick 1 = "Stop and wait"
    assert detect_rate_limit_modal(buf) == 1


def test_rate_limit_detects_with_ansi():
    ansi = b"\x1b[1;33m"
    reset = b"\x1b[0m"
    buf = (
        ansi + b"You've hit your limit" + reset + b"\n"
        b"/rate-limit-options\n"
        b"  " + ansi + b"1." + reset + b" Stop and wait for limit to reset\n"
        b"  " + ansi + b"2." + reset + b" Upgrade your plan\n"
    )
    assert detect_rate_limit_modal(buf) == 1


def test_rate_limit_not_triggered_by_compact():
    # Compact modal should NOT trigger rate-limit detector.
    buf = (
        b"Context is getting full. Auto-compact options:\n"
        b"  1. Compact now\n"
        b"  2. Summarize and continue\n"
        b"  3. Leave alone\n"
    )
    assert detect_rate_limit_modal(buf) is None


def test_rate_limit_keyword_wait_for_limit():
    buf = (
        b"wait for limit to reset\n"
        b"  1. ok\n"
        b"  2. nah\n"
    )
    assert detect_rate_limit_modal(buf) == 1
