from . import help
from . import start
from . import violations
from . import settings
# Last on purpose: support.py ends in a catch-all for private chats, and aiogram tries
# handlers in registration order. Imported earlier it would swallow /start and the menu.
from . import support
