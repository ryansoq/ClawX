"""Tests for the pure-function startup modal detector.

The detector is the most safety-critical piece of the auto-skip path: a
false positive types a stray digit into Claude, a false negative leaves
the session wedged forever. So we test it with realistic-shaped fixtures
covering the happy path, ANSI noise, and a few negative cases.
"""
from clawx import (
    detect_startup_modal,
    detect_compact_event,
    detect_rate_limit_modal,
    detect_feedback_modal,
    detect_resume_modal,
)


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


# ── False positive regression tests (from real transcripts) ─────

def test_rate_limit_not_triggered_by_code_diff():
    """Real transcript: test code containing rate-limit keywords in a diff."""
    buf = (
        b" 117 +    buf = (\n"
        b' 118 +        ansi + b"You\'ve hit your limit" + reset + b"\\n"\n'
        b' 119 +        b"/rate-limit-options\\n"\n'
        b" 120 +        b\"  1. Stop and wait for limit to reset\\n\"\n"
        b" 121 +        b\"  2. Upgrade your plan\\n\"\n"
    )
    assert detect_rate_limit_modal(buf) is None


def test_startup_modal_not_triggered_by_code_diff():
    """Real transcript: code diff with 'compact' keyword + line numbers."""
    buf = (
        b" 31 +def test_detects_three_option_compact_prompt():\n"
        b" 32 +    buf = (\n"
        b' 33 +        b"Auto-compact options:\\n"\n'
        b' 34 +        b"  1. Compact now\\n"\n'
        b' 35 +        b"  2. Summarize\\n"\n'
        b' 36 +        b"  3. Leave alone\\n"\n'
        b" 37 +    )\n"
    )
    assert detect_startup_modal(buf) is None


def test_startup_modal_not_triggered_by_conversation_about_compact():
    """Real transcript: discussing compact events in chat."""
    buf = (
        b"Ryan: compact happened 8 times in 2.5 days\n"
        b"That's about 1. every 7-8 hours which is normal\n"
        b"for heavy tool use sessions. 2. The death spiral\n"
        b"theory was wrong.\n"
    )
    assert detect_startup_modal(buf) is None


def test_rate_limit_still_detects_real_prompt():
    """Real transcript: actual rate limit prompt from Claude."""
    buf = (
        b"You've hit your limit \xc2\xb7 resets 12am (Asia/Taipei)\n"
        b"/upgrade or /extra-usage to finish what you're working on.\n\n"
        b"  1. Stop and wait for limit to reset\n"
        b"  2. Upgrade your plan\n"
    )
    assert detect_rate_limit_modal(buf) == 1


def test_compact_not_triggered_by_code_diff():
    """Real transcript: code diff discussing compact events."""
    buf = (
        b" 215 +        b\" 37 +    )\\n\"\n"
        b" 216 +    )\n"
        b" 217 + Conversation compacted\n"
    )
    assert detect_compact_event(buf) is None


def test_compact_not_triggered_by_log_grep_stdout():
    """Real false positive from 2026-04-14: grep stdout on clawx logs
    contained 'conversation' and 'compacted' in different lines, which
    the old loose matcher ('both words anywhere in buffer') flagged as
    a compact event. The new matcher requires adjacency.
    """
    buf = (
        b"2026-04-13 23:02:36 [ClawX] [Compact] detected via PTY stream\n"
        b"2026-04-13 23:02:39 [ClawX] Injected post-compact identity reload\n"
        b"Note: previous conversation covered the Polymarket arbitrage question\n"
        b"later the same file shows the word compacted in a different line\n"
    )
    assert detect_compact_event(buf) is None


def test_compact_not_triggered_by_discussion_about_compact():
    """Real-world false positive: chat messages talking about compact
    events without the actual 'Conversation compacted' phrase.
    """
    buf = (
        b"Ryan: the conversation is getting long, are we near compact?\n"
        b"Nami: /context shows 78k/200k, not close to being compacted yet\n"
    )
    assert detect_compact_event(buf) is None


def test_compact_still_detects_adjacent_with_multiple_spaces():
    """Edge case: multiple ANSI cursor-moves become multiple spaces
    after _ANSI_RE.sub — still within the \\s{1,8} bound.
    """
    buf = b"\xe2\x9c\xbb Conversation    compacted (ctrl+o for history)\n"
    assert detect_compact_event(buf) is True


# ── Feedback modal detector tests ───────────────────────────────

def test_feedback_returns_none_on_empty():
    assert detect_feedback_modal(b"") is None


def test_feedback_returns_none_on_plain_text():
    assert detect_feedback_modal(b"hello world\n> ") is None


def test_feedback_detects_standard_prompt():
    buf = (
        b"How is Claude doing this session? (optional)\n"
        b"  1: Bad  2: Fine  3: Good  0: Dismiss\n"
    )
    assert detect_feedback_modal(buf) == 0


def test_feedback_detects_with_ansi():
    ansi = b"\x1b[1;36m"
    reset = b"\x1b[0m"
    buf = (
        ansi + b"How is Claude doing this session?" + reset + b" (optional)\n"
        b"  1: Bad  2: Fine  3: Good  0: Dismiss\n"
    )
    assert detect_feedback_modal(buf) == 0


def test_feedback_not_triggered_by_code_diff():
    buf = (
        b" 42 +    # How is Claude doing this session?\n"
        b" 43 +    assert result == 0  # Dismiss\n"
    )
    assert detect_feedback_modal(buf) is None


def test_feedback_not_triggered_by_conversation():
    buf = b"Ryan asked how is claude doing this session and I said fine\n"
    # No "dismiss" option → None
    assert detect_feedback_modal(buf) is None


def test_startup_modal_still_detects_real_prompt():
    """Real prompt: actual compact modal at session resume."""
    buf = (
        b"\xe2\x9c\xbb Conversation compacted (ctrl+o for history)\n\n"
        b"Auto-compact prompt:\n"
        b"  1. compact\n"
        b"  2. summarize\n"
        b"  3. skip\n"
    )
    assert detect_startup_modal(buf) == 3


# -------------------- resume-mode modal --------------------

def test_resume_returns_none_on_empty():
    assert detect_resume_modal(b"") is None


def test_resume_returns_none_on_plain_text():
    assert detect_resume_modal(b"hello world\n> ") is None


def test_resume_detects_standard_prompt():
    """Fixture taken from the real screenshot Ryan sent (2026-04-12)."""
    buf = (
        b"This session is 2h 14m old and 117.6k tokens.\n"
        b"Resuming the full session will consume a substantial portion of your usage summary.\n\n"
        b"  1. Resume from summary (recommended)\n"
        b"  2. Resume full session as-is\n"
        b"  3. Don't ask me again\n"
        b"\n"
        b"Enter to confirm \xc2\xb7 Esc to cancel\n"
    )
    assert detect_resume_modal(buf) == 3


def test_resume_detects_with_ansi():
    ansi = b"\x1b[1;36m"
    reset = b"\x1b[0m"
    buf = (
        ansi + b"This session is 2h 14m old and 117.6k tokens." + reset + b"\n"
        b"Resuming the full session will consume a substantial portion of your usage summary.\n"
        b"  1. Resume from summary (recommended)\n"
        b"  2. Resume full session as-is\n"
        b"  3. Don't ask me again\n"
    )
    assert detect_resume_modal(buf) == 3


def test_resume_requires_dont_ask_option():
    """If only options 1 and 2 are visible, don't return 3."""
    buf = (
        b"Resume from summary or resume full session?\n"
        b"  1. Resume from summary\n"
        b"  2. Resume full session as-is\n"
    )
    assert detect_resume_modal(buf) is None


def test_resume_not_triggered_by_code_diff():
    """A test file mentioning the string shouldn't trigger the detector."""
    buf = (
        b" 117 +    # Resume from summary (recommended)\n"
        b' 118 +    assert choice == 3  # "Don\'t ask me again"\n'
    )
    assert detect_resume_modal(buf) is None


def test_resume_not_triggered_by_conversation():
    """Plain prose with both phrases should NOT trigger (prose lacks
    the start-of-line '3. Don't ask me again' option format)."""
    buf = b"Ryan said: resume from summary makes more sense than don't ask me again\n"
    assert detect_resume_modal(buf) is None


def test_resume_detects_cursor_marker():
    """Handle ❯ cursor prefix that Claude sometimes renders."""
    buf = (
        b"Resume from summary (recommended)\n"
        b"  1. Resume from summary (recommended)\n"
        b"  2. Resume full session as-is\n"
        b"\xe2\x9d\xaf 3. Don't ask me again\n"
    )
    assert detect_resume_modal(buf) == 3


def test_resume_detects_carriage_return_only_lines():
    """Claude Code's PTY output terminates modal lines with \\r (not \\n).

    Regression test for the bug Ryan caught — the real modal in the PTY
    looked like:

        "\\r❯1Resume from summary (recommended)"
        "\\r2. Resumefullsessionas-is"
        "\\r  3. Don't ask me again"

    The line-start anchor was `\\n` only, so the regex missed every option.
    """
    buf = (
        b"\rResuming the full session will consume usage limits.\r"
        b"\r\xe2\x9d\xaf1Resume from summary (recommended)"
        b"\r2. Resume full session as-is"
        b"\r  3. Don't ask me again"
        b"\r\r\nEnter to confirm\r\n"
    )
    assert detect_resume_modal(buf) == 3


def test_resume_real_transcript_fixture():
    """Regression test against a real captured PTY chunk.

    Saved from the 13:42:24 incident where detect_resume_modal failed on
    live data: Claude rendered the modal with \\r line terminators and
    our regex required \\n. Fixture is exactly the 8KB sliding window the
    handler had when the modal was on screen.
    """
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "resume_modal_real.bin"
    data = fixture.read_bytes()
    assert detect_resume_modal(data) == 3


def test_resume_not_tripped_by_date_in_diff_guard():
    """The diff-context guard rejects buffers containing code diff patterns.

    Originally `\\d{2,}\\s*[+\\-]` — which also matches dates like
    '2026-04-12' (`04-`). Tightened to `\\d{2,}\\s+[+\\-]\\s`.
    """
    buf = (
        b"This session is 2h 14m old, started 2026-04-12 08:30.\n"
        b"Resume from summary (recommended)\n"
        b"  1. Resume from summary (recommended)\n"
        b"  2. Resume full session as-is\n"
        b"  3. Don't ask me again\n"
    )
    assert detect_resume_modal(buf) == 3


def test_resume_diff_guard_still_rejects_real_code_diff():
    """Make sure the tightened diff guard still catches real diff context."""
    buf = (
        b"Ryan posted this diff:\n"
        b" 117 +    if 'resume from summary' in text:\n"
        b" 118 +        return 3\n"
        b" 119 +    if '  3. don't ask me again' in text:\n"
    )
    assert detect_resume_modal(buf) is None
