"""Tests for the pure-function startup modal detector.

The detector is the most safety-critical piece of the auto-skip path: a
false positive types a stray digit into Claude, a false negative leaves
the session wedged forever. So we test it with realistic-shaped fixtures
covering the happy path, ANSI noise, and a few negative cases.
"""
from clawx import detect_startup_modal


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


def test_handles_bracket_style_options():
    buf = (
        b"Auto-compact prompt:\n"
        b"  [1] compact\n"
        b"  [2] summarize\n"
        b"  [3] skip\n"
    )
    assert detect_startup_modal(buf) == 3


def test_handles_invalid_utf8_gracefully():
    # PTY can emit partial UTF-8 mid-buffer; should not crash.
    buf = b"\xff\xfe summarize?\n  1. a\n  2. b\n"
    # Either detects or returns None — must not raise.
    result = detect_startup_modal(buf)
    assert result in (None, 2)
