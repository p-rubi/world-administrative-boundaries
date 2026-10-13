# -*- coding: utf-8 -*-
"""Terrae boundaries: administrative boundaries of the world in QGIS."""


def classFactory(iface):
    from .plugin import TerraeBoundariesPlugin
    return TerraeBoundariesPlugin(iface)