# -*- coding: utf-8 -*-
"""The few places where QGIS 3 (Qt 5) and QGIS 4 (Qt 6) differ."""
from qgis.core import Qgis, QgsBlockingNetworkRequest

try:                                    # Qt 6: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:                     # Qt 5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401


def _level(name):
    scoped = getattr(Qgis, "MessageLevel", None)
    return getattr(scoped, name) if scoped is not None and hasattr(scoped, name) else getattr(Qgis, name)


INFO = _level("Info")
WARNING = _level("Warning")
CRITICAL = _level("Critical")
SUCCESS = _level("Success")

_codes = getattr(QgsBlockingNetworkRequest, "ErrorCode", QgsBlockingNetworkRequest)
NET_NO_ERROR = _codes.NoError


def exec_(obj, *args):
    """Run a dialog or a menu: `exec` in Qt 6, `exec_` in old Qt 5 bindings."""
    return (obj.exec if hasattr(obj, "exec") else obj.exec_)(*args)