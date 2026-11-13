"""Startup banner: a pulse-wave wordmark + Skymind Automation byline.

Printed to stderr so it never lands in --snapshot/--export's stdout output
(both are meant to be piped/parsed). Colors degrade automatically for
non-tty streams, NO_COLOR, or TERM=dumb.
"""

import os
import sys

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_BOLD_CYAN = "\033[1;96m"
_MAGENTA = "\033[1;35m"

_WAVE = "▁▂▃▅▇█▇▅▃▂▁"


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def _c(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}{_RESET}" if enabled else text


def render_banner(version: str, stream=None) -> str:
    """Build the banner text. Pure function, easy to test without a real tty."""
    stream = stream if stream is not None else sys.stderr
    color = _supports_color(stream)

    wordmark = " ".join("NETPULSE")
    wave = _c(_WAVE, "96", color)
    title = _c(wordmark, "1;96", color)
    tagline = _c(f"v{version} · silent LAN monitoring, self-hosted", "2", color)
    byline = _c("◆", "1;35", color) + " " + _c("Skymind Automation", "1;35", color)

    lines = [
        "",
        f"  {wave}   {title}   {wave}",
        f"  {tagline}",
        f"  {byline}",
        "",
    ]
    return "\n".join(lines)


def print_banner(version: str, stream=None) -> None:
    stream = stream if stream is not None else sys.stderr
    print(render_banner(version, stream), file=stream)
