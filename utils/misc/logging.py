import logging
import sys

FORMAT = u'%(filename)s [LINE:%(lineno)d] #%(levelname)-8s [%(asctime)s]  %(message)s'


class _MaxLevel(logging.Filter):
    """Passes only records at or below `level` — the ceiling stdout gets."""

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.level


# basicConfig's default handler puts everything on stderr. Under Railway that tags
# every INFO line as an error, so a real one is indistinguishable from the ~1500
# routine events a day. Split the streams by severity instead.
_stdout = logging.StreamHandler(sys.stdout)
_stdout.addFilter(_MaxLevel(logging.INFO))
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setLevel(logging.WARNING)

logging.basicConfig(format=FORMAT,
                    level=logging.INFO,
                    # level=logging.DEBUG,  # Можно заменить на другой уровень логгирования.
                    handlers=[_stdout, _stderr],
                    )
