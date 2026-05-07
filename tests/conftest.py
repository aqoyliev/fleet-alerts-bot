import sys
import types
import unittest.mock as mock

# Stub data.config before any project module is imported
_data_pkg = types.ModuleType("data")
_config_mod = types.ModuleType("data.config")
_config_mod.MOTIVE_API_KEY = "test"
_config_mod.GROUP_CHAT_ID = 0
_config_mod.ADMINS = []
_config_mod.BOT_TOKEN = "test"
_config_mod.IP = "localhost"
_data_pkg.config = _config_mod
sys.modules["data"] = _data_pkg
sys.modules["data.config"] = _config_mod

# Stub all heavy/external dependencies
_stubs = [
    "aiogram",
    "aiogram.types",
    "aiogram.dispatcher",
    "aiogram.dispatcher.filters",
    "aiogram.utils",
    "aiogram.utils.exceptions",
    "aiohttp",
    "aiohttp.web",
    "asyncpg",
    "loader",
    "keyboards",
    "keyboards.inline",
    "keyboards.inline.violations",
    "utils.db_api",
    "utils.db_api.db",
    "utils.db_api.violations",
    "utils.db_api.companies",
    "utils.db_api.admins",
    "utils.db_api.users",
    "utils.motive",
    "utils.notify_admins",
    "utils.set_bot_commands",
]
for _mod in _stubs:
    sys.modules[_mod] = mock.MagicMock()
