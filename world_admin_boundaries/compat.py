# -*- coding: utf-8 -*-
"""The few places where QGIS 3 (Qt 5) and QGIS 4 (Qt 6) differ."""
from qgis.core import Qgis, QgsBlockingNetworkRequest, QgsSymbolLayer

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

_props = getattr(QgsSymbolLayer, "Property", QgsSymbolLayer)
STROKE_WIDTH = getattr(_props, "StrokeWidth", None) or getattr(QgsSymbolLayer, "PropertyStrokeWidth")
