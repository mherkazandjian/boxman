import logging
import sys
from contextlib import contextmanager

BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

RESET_SEQ = "\033[0m"
COLOR_SEQ = "\033[%dm"
BOLD_SEQ = "\033[1m"

# Custom "STATUS" level sits between INFO (20) and WARNING (30). It is the
# default-visible level: concise, docker-compose-style progress lines. Regular
# info() chatter is hidden until the user asks for it with -v.
STATUS = 25
logging.addLevelName(STATUS, "STATUS")

# Single source of truth for the default verbosity.
DEFAULT_LEVEL = STATUS


def formatter_message(message, use_color=True):
    if use_color:
        message = message.replace("$RESET", RESET_SEQ).replace("$BOLD", BOLD_SEQ)
    else:
        message = message.replace("$RESET", "").replace("$BOLD", "")
    return message


COLORS = {
    'WARNING': YELLOW,
    'INFO': GREEN,
    'DEBUG': BLUE,
    'CRITICAL': MAGENTA,
    'ERROR': RED,
    'STATUS': CYAN,
}

# Rich, diagnostic format — only used at DEBUG (-vv and above).
RICH_FORMAT = (
    "[%(asctime)s %(levelname)s "
    "$BOLD%(filename)s{%(lineno)d}$RESET:%(funcName)s()] "
    "%(message)s"
)


class ColoredFormatter(logging.Formatter):
    """Level-aware, colorized formatter.

    - DEBUG: full ``[time LEVEL file{line}:func()] message`` diagnostic line.
    - WARNING/ERROR/CRITICAL: ``LEVEL: message`` with a colorized level tag.
    - STATUS/INFO: just ``message`` (docker-compose-style).
    """

    def __init__(self, use_color=True):
        logging.Formatter.__init__(self)
        self.use_color = use_color
        self._rich = formatter_message(RICH_FORMAT, use_color)

    def _colorize(self, levelname):
        if self.use_color and levelname in COLORS:
            color_code = 30 + COLORS[levelname]
            return COLOR_SEQ % color_code + BOLD_SEQ + levelname + RESET_SEQ
        return levelname

    def format(self, record):
        if record.levelno <= logging.DEBUG:
            self._style._fmt = self._rich
            pad = max(0, 8 - len(record.levelname))
            record.levelname = self._colorize(record.levelname) + " " * pad
        elif record.levelno >= logging.WARNING:
            self._style._fmt = "%(levelname)s: %(message)s"
            record.levelname = self._colorize(record.levelname)
        else:  # STATUS, INFO
            self._style._fmt = "%(message)s"
        return logging.Formatter.format(self, record)


# create logger
logger = logging.getLogger('boxman')
logger.setLevel(DEFAULT_LEVEL)

# Only configure the logger if it doesn't have handlers already
if not logger.handlers:
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # handler stays wide open; the *logger* level is the effective gate
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)

    formatter = ColoredFormatter(use_color=True)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # Prevent propagation to avoid duplicate logs if this is a child logger
    logger.propagate = False


def status(self, message, *args, **kws):
    """``logger.status(...)`` — emit at the default-visible STATUS level."""
    if self.isEnabledFor(STATUS):
        self._log(STATUS, message, args, **kws)


logging.Logger.status = status


def set_verbosity(count):
    """Map a -v count to a level and apply it. 0->STATUS, 1->INFO, >=2->DEBUG."""
    if count <= 0:
        level = DEFAULT_LEVEL
    elif count == 1:
        level = logging.INFO
    else:
        level = logging.DEBUG
    logger.setLevel(level)
    return level


def set_quiet():
    """Silence everything below WARNING (used by -q/--quiet)."""
    logger.setLevel(logging.WARNING)
    return logging.WARNING


def is_verbose(level=logging.DEBUG):
    """True if the boxman logger currently emits at ``level`` (default DEBUG)."""
    return logger.isEnabledFor(level)


@contextmanager
def suppressed(level=logging.CRITICAL + 1):
    """Temporarily raise the boxman logger level, restoring the prior level after.

    Replaces the old pattern of hardcoding a restore to logging.DEBUG, which
    clobbered any user-selected verbosity.
    """
    prev = logger.level
    logger.setLevel(level)
    try:
        yield
    finally:
        logger.setLevel(prev)
