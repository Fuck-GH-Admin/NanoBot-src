"""Root conftest — mocks nonebot before chatbot/__init__.py runs.

chatbot/__init__.py calls get_driver() at module level, which fails outside
NoneBot runtime.  This file is discovered by pytest BEFORE any test package
hierarchy is resolved, so we can safely replace nonebot in sys.modules.
"""

import sys
import types
from unittest.mock import MagicMock

# ── Helper: create a mock module that looks like a package ─────
def _pkg(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []  # make it a package
    for k, v in attrs.items():
        setattr(m, k, v)
    return m

# ── nonebot stubs ──────────────────────────────────────────────
_nb = _pkg("nonebot")
_nb.get_driver = MagicMock()
_nb.get_bot = MagicMock()
_nb.on_message = MagicMock()
_nb.on_notice = MagicMock()
_nb.log = MagicMock()

_nb_plugin = _pkg("nonebot.plugin")
_nb_plugin.PluginMetadata = MagicMock()

_nb_params = _pkg("nonebot.params")
_nb_params.Command = MagicMock()
_nb_params.RawCommand = MagicMock()
_nb_params.State = MagicMock()
_nb_params.EventPlainText = MagicMock()

_nb_adapters = _pkg("nonebot.adapters")
_nb_onebot = _pkg("nonebot.adapters.onebot")
_nb_v11 = _pkg("nonebot.adapters.onebot.v11")
_nb_v11.Bot = MagicMock()
_nb_v11.GroupMessageEvent = MagicMock()
_nb_v11.PrivateMessageEvent = MagicMock()
_nb_v11.MessageSegment = MagicMock()
_nb_v11.Message = MagicMock()
_nb_v11.PokeNotifyEvent = MagicMock()
_nb_v11.GroupIncreaseNoticeEvent = MagicMock()
_nb_v11.GroupDecreaseNoticeEvent = MagicMock()
_nb.adapters = _nb_adapters
_nb_adapters.onebot = _nb_onebot
_nb_onebot.v11 = _nb_v11

sys.modules["nonebot"] = _nb
sys.modules["nonebot.log"] = _nb.log
sys.modules["nonebot.plugin"] = _nb_plugin
sys.modules["nonebot.params"] = _nb_params
sys.modules["nonebot.adapters"] = _nb_adapters
sys.modules["nonebot.adapters.onebot"] = _nb_onebot
sys.modules["nonebot.adapters.onebot.v11"] = _nb_v11

# ── loguru stub ────────────────────────────────────────────────
_luru = _pkg("loguru")
_luru.logger = MagicMock()
sys.modules["loguru"] = _luru

# ── watchdog stubs (config.py hot-reload) ──────────────────────
_wd_obs = _pkg("watchdog.observers")
_wd_obs.Observer = MagicMock()
_wd_evt = _pkg("watchdog.events")
_wd_evt.FileSystemEventHandler = MagicMock()

_wd = _pkg("watchdog")
_wd.observers = _wd_obs
_wd.events = _wd_evt

sys.modules["watchdog"] = _wd
sys.modules["watchdog.observers"] = _wd_obs
sys.modules["watchdog.events"] = _wd_evt
