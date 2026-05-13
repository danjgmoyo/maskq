# -*- coding: utf-8 -*-
"""
plugin.py  —  Plugin lifecycle manager.

Responsibilities:
  • Create/destroy the toolbar icon and Raster menu entry.
  • Register/unregister the Processing provider.
  • Create the dock panel on first toggle, then show/hide it.

This file knows nothing about GDAL, masks, or histograms.
It only manages QGIS integration lifecycle.
"""
import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui     import QIcon
from qgis.PyQt.QtCore    import Qt
from qgis.core           import QgsApplication, Qgis


class MaskQPlugin:

    def __init__(self, iface):
        self.iface     = iface
        self._panel    = None   # MaskQPanel — created lazily on first open
        self._action   = None   # toolbar / menu toggle button
        self._provider = None   # Processing provider

    # ── QGIS plugin lifecycle ─────────────────────────────────────────────────

    def initGui(self):
        """Called by QGIS when the plugin is enabled."""
        icon_path = os.path.join(os.path.dirname(__file__), 'icons', 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self._action = QAction(icon, 'MaskQ', self.iface.mainWindow())
        self._action.setCheckable(True)
        self._action.setToolTip(
            'MaskQ — mask or clip rasters by value range, '
            'raster mask, or polygon boundary')
        self._action.triggered.connect(self._toggle_panel)

        self.iface.addToolBarIcon(self._action)
        self.iface.addPluginToRasterMenu('&MaskQ', self._action)

        # Register our Processing algorithms so they appear in the Toolbox
        from .provider import MaskQProvider
        self._provider = MaskQProvider()
        QgsApplication.processingRegistry().addProvider(self._provider)

    def unload(self):
        """Called by QGIS when the plugin is disabled or QGIS closes."""
        # Remove Processing provider.
        # Guard with sip.isdeleted because QGIS may have already deleted
        # the C++ object before calling Python unload.
        if self._provider is not None:
            try:
                from PyQt5 import sip
                if not sip.isdeleted(self._provider):
                    QgsApplication.processingRegistry().removeProvider(
                        self._provider)
            except Exception:
                pass
            self._provider = None

        if self._action is not None:
            try:
                self.iface.removeToolBarIcon(self._action)
                self.iface.removePluginToRasterMenu('&MaskQ',
                                                    self._action)
            except Exception:
                pass
            self._action = None

        if self._panel is not None:
            try:
                self._panel.cleanup()              # stop threads, remove preview
                self.iface.removeDockWidget(self._panel)
                self._panel.deleteLater()
            except Exception:
                pass
            self._panel = None

    # ── panel toggle logic ────────────────────────────────────────────────────

    def _toggle_panel(self):
        """Open, show, hide, or raise the dock panel on toolbar click."""
        # First click ever — create the panel and dock it
        if self._panel is None:
            self._open_panel()
            return

        # Don't close while a task is running
        if self._panel.has_active_task():
            self._panel.setVisible(True)
            self._panel.raise_()
            self.iface.messageBar().pushMessage(
                'MaskQ',
                'A task is running — please wait or click Cancel.',
                level=Qgis.Warning, duration=4)
            return

        if not self._panel.isVisible():
            self._panel.setVisible(True)
            self._panel.raise_()
            self._action.setChecked(True)
        elif self._is_panel_on_top():
            self._panel.setVisible(False)
            self._action.setChecked(False)
        else:
            self._panel.raise_()
            self._action.setChecked(True)

    def _open_panel(self):
        """Lazily import and create the panel (keeps startup time fast)."""
        from .panel import MaskQPanel
        self._panel = MaskQPanel(self.iface)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self._panel)
        # Keep toolbar button state in sync with panel visibility
        self._panel.visibilityChanged.connect(self._action.setChecked)
        self._action.setChecked(True)

    def _is_panel_on_top(self):
        """Return True if the panel is the visible tab in a tabbed dock group."""
        try:
            siblings = self.iface.mainWindow().tabifiedDockWidgets(self._panel)
            if not siblings:
                return True  # not tabbed, must be visible
            return self._panel.visibleRegion().boundingRect().width() > 0
        except Exception:
            return True
