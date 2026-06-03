# -*- coding: utf-8 -*-
# __init__.py — QGIS plugin entry point.
# QGIS calls classFactory(iface) when the plugin is loaded.
# We import lazily (inside the function) so nothing breaks if
# the plugin folder is present but dependencies are missing.

def classFactory(iface):
    from .plugin import MaskQPlugin
    return MaskQPlugin(iface)
