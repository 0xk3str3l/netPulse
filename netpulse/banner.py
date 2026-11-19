"""Startup banner: an ECG-style pulse trace + Skymind Automation byline.

Printed to stderr so it never lands in --snapshot/--export's stdout output
(both are meant to be piped/parsed). Colors degrade automatically for
non-tty streams, NO_COLOR, or TERM=dumb.
"""

import os
import sys

_RESET = "\033[0m"

_WORDMARK = "N E T P U L S E"
_TRACE_WIDTH = 56
_PEAK = "\u2588"
_REST = "\u2500"
_RISE = "/"
_FALL = "\\"


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def _c(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}{_RESET}" if enabled else text


def _trace_rows(width: int = _TRACE_WIDTH, centers=(14, 30, 46), height: int = 2):
    """Draw a resting ECG line with one QRS spike per centre column.

    Row 0 is the peak, rows 1..height are the rising/falling diagonals, and
    the last row is the baseline the diagonals meet. Pure text, no colors.
    """
    rows = [[" "] * width for _ in range(height + 2)]
    rows[-1] = [_REST] * width
    for center in centers:
        rows[0][center] = _PEAK
        for step in range(1, height + 1):
            rows[step][center - step] = _RISE
            rows[step][center + step] = _FALL
        for col in range(center - height, center + height + 1):
            rows[-1][col] = " "
        rows[-1][center - height] = _RISE
        rows[-1][center + height] = _FALL
    return ["".join(row).rstrip() for row in rows]


def render_banner(version: str, stream=None) -> str:
    """Build the banner text. Pure function, easy to test without a real tty."""
    stream = stream if stream is not None else sys.stderr
    color = _supports_color(stream)

    peak, *pulses, rest = _trace_rows()
    version_tag = f"v{version}"
    pad = " " * max(2, _TRACE_WIDTH - len(_WORDMARK) - len(version_tag))

    lines = [
        "",
        f"   {_c(_WORDMARK, '1;96', color)}{pad}{_c(version_tag, '2', color)}",
        f"   {_c(peak, '1;96', color)}",
    ]
    lines.extend(f"   {_c(row, '96', color)}" for row in pulses)
    lines.extend(
        [
            f"   {_c(rest, '2;36', color)}",
            f"   {_c('every device leaves a trace  ·  self-hosted, no cloud', '2', color)}",
            "   " + _c("◆", "1;35", color) + " " + _c("Skymind Automation", "1;35", color),
            "",
        ]
    )
    return "\n".join(lines)


def print_banner(version: str, stream=None) -> None:
    stream = stream if stream is not None else sys.stderr
    print(render_banner(version, stream), file=stream)
