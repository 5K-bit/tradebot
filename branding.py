"""LATHE terminal identity. Standard library only; never connects to MT5."""

import os
import shutil
import sys


WORDMARK = r"""  LL       AAA   TTTTTTT HH   HH EEEEEEE
  LL      AA AA    TTT   HH   HH EE
  LL     AA   AA   TTT   HHHHHHH EEEEE
  LL     AAAAAAA   TTT   HH   HH EE
  LLLLLL AA   AA   TTT   HH   HH EEEEEEE"""


def print_banner(context="PREVIEW", *, stream=None):
    """Print a compact, ASCII-safe banner, with color on supported terminals."""
    stream = sys.stdout if stream is None else stream
    interactive = getattr(stream, "isatty", lambda: False)()
    color = (interactive and "NO_COLOR" not in os.environ
             and os.environ.get("TERM") != "dumb"
             and (os.name != "nt" or bool(os.environ.get("WT_SESSION"))
                  or os.environ.get("TERM", "").startswith("xterm")))
    gold = "\033[93m" if color else ""
    reset = "\033[0m" if color else ""
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    title = WORDMARK if width >= 48 else "  LATHE"
    rule = "  " + "-" * min(54, max(10, width - 4))
    lines = ["", "  BLACKFONG / PROJECTS", rule,
             gold + title + reset,
             "", "  FOREX TRADING ENGINE",
             "  Observe. Evaluate. Execute.",
             rule, "  " + context,
             "  LOCAL SYSTEMS. CLEAR PURPOSE. / 5K", ""]
    print("\n".join(lines), file=stream)


if __name__ == "__main__":
    print_banner("PREVIEW / no broker connection")
