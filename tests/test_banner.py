"""Startup banner: color degradation, content, no accidental stdout use."""

import io

from netpulse.banner import render_banner


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_banner_contains_wordmark_and_byline():
    out = render_banner("1.0.0", stream=io.StringIO())
    assert "N E T P U L S E" in out
    assert "Skymind Automation" in out
    assert "1.0.0" in out


def test_banner_has_no_ansi_codes_for_non_tty_stream():
    out = render_banner("1.0.0", stream=io.StringIO())
    assert "\033[" not in out


def test_banner_has_ansi_codes_for_tty_stream():
    out = render_banner("1.0.0", stream=_FakeTTY())
    assert "\033[" in out


def test_banner_respects_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = render_banner("1.0.0", stream=_FakeTTY())
    assert "\033[" not in out


def test_banner_respects_dumb_term(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    out = render_banner("1.0.0", stream=_FakeTTY())
    assert "\033[" not in out
