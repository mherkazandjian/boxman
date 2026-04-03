import logging
import sys

BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

RESET_SEQ = "\033[0m"
COLOR_SEQ = "\033[%dm"
BOLD_SEQ = "\033[1m"


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
    'ERROR': RED
}


class ColoredFormatter(logging.Formatter):
    def __init__(self, msg, use_color=True):
        logging.Formatter.__init__(self, msg)
        self.use_color = use_color

    def format(self, record):
        levelname = record.levelname
        if self.use_color and levelname in COLORS:
            color_code = 30 + COLORS[levelname]
            levelname_color = (
                COLOR_SEQ % color_code + BOLD_SEQ + levelname + RESET_SEQ
            )
            # Pad to a fixed visible width (8 chars) before adding ANSI codes
            # so columns align. ANSI escapes add ~12 invisible chars.
            pad = 8 - len(levelname)
            record.levelname = levelname_color + " " * pad
        return logging.Formatter.format(self, record)


# create logger
logger = logging.getLogger('boxman')
logger.log_depth = 0
logger.setLevel(logging.DEBUG)

# Only configure the logger if it doesn't have handlers already
if not logger.handlers:

    # remove all existing handlers first to ensure we don't add duplicates
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # create console handler and set level to debug
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)

    # create formatter
    FORMAT = (
        "[%(asctime)s %(levelname)s "
        "$BOLD%(filename)s{%(lineno)d}$RESET:%(funcName)s()] "
        "%(message)s"
    )
    COLOR_FORMAT = formatter_message(FORMAT, True)
    formatter = ColoredFormatter(COLOR_FORMAT)

    # add formatter to the console handler and then add the console handler to the logger
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # Prevent propagation to prevent duplicate logs if this is a child logger
    logger.propagate = False
