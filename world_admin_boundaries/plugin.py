# -*- coding: utf-8 -*-
"""The plugin: a toolbar button with a menu, the panel, the settings, and the one repository they all read."""
import json
import os

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                                 QLineEdit, QMenu, QPushButton, QTextBrowser, QToolButton, QVBoxLayout)
from qgis.core import QgsApplication, QgsSettings

from .compat import QAction, INFO, WARNING, run_modal
from .net import Cache, CachedHttpReader, Fetcher, QgisHttpReader, log
from .panel import TerraePanel, level_note
from .repository import LEVEL_NAMES, LocalReader, Repository, RepositoryError, human_size, is_url, normalise_location

HERE = os.path.dirname(__file__)
KEY = "world_admin_boundaries/"


def plugin_config():
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def open_repository(location, version=None, cache_dir=None):
    """The repository at a folder or URL, loaded. Raises RepositoryError with a message a person can act on."""
    location = normalise_location(location)
    if not location:
        raise RepositoryError("No data source is set.")
    if not is_url(location) and not os.path.isdir(location):
        raise RepositoryError("The data source folder does not exist: {}".format(location))
    if is_url(location):
        reader = CachedHttpReader(location, cache_dir) if cache_dir else QgisHttpReader(location)
    else:
        reader = LocalReader(location)
    return Repository(location, reader, version or None).load()


class SettingsDialog(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("World Administrative Boundaries \u2014 settings")
        self.setMinimumWidth(560)
        col = QVBoxLayout(self)
        note = QLabel("The data repository is a folder on this computer or network, or the address of the repository online. "
                      "The plugin reads from it what exists; nothing else is configured.")
        note.setWordWrap(True)
        col.addWidget(note)
        form = QFormLayout()
        row = QHBoxLayout()
        self.txt_source = QLineEdit(app.source())
        self.txt_source.setPlaceholderText("https://\u2026/  or  a folder")
        browse = QPushButton("Folder\u2026")
        browse.clicked.connect(self._browse_source)
        default = QPushButton("Default")
        default.clicked.connect(lambda: self.txt_source.setText(app.default_source()))
        row.addWidget(self.txt_source, 1)
        row.addWidget(browse)
        row.addWidget(default)
        form.addRow("Data source", row)
        self.cmb_version = QComboBox()
        self.cmb_version.addItem("Latest", "")
        for v in reversed(app.repo.releases if app.repo is not None else []):
            self.cmb_version.addItem(v, v)
        i = self.cmb_version.findData(app.setting("version", ""))
        self.cmb_version.setCurrentIndex(max(i, 0))
        form.addRow("Release", self.cmb_version)
        row = QHBoxLayout()
        self.txt_cache = QLineEdit(app.cache_dir())
        browse2 = QPushButton("Folder\u2026")
        browse2.clicked.connect(self._browse_cache)
        row.addWidget(self.txt_cache, 1)
        row.addWidget(browse2)
        form.addRow("Downloads are kept in", row)
        row = QHBoxLayout()
        self.lbl_cache = QLabel("")
        self.btn_open = QPushButton("Open")
        self.btn_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.txt_cache.text())))
        self.btn_empty = QPushButton("Empty")
        self.btn_empty.clicked.connect(self._empty)
        row.addWidget(self.lbl_cache, 1)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_empty)
        form.addRow("", row)
        self.chk_verify = QCheckBox("Check every download against the repository's SHA-256 checksum")
        self.chk_verify.setChecked(app.setting("verify", True, bool))
        form.addRow("", self.chk_verify)
        col.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        col.addWidget(buttons)
        self._refresh_cache()

    def _browse_source(self):
        folder = QFileDialog.getExistingDirectory(self, "The repository folder (it holds repository.json)", self.txt_source.text())
        if folder:
            self.txt_source.setText(folder)

    def _browse_cache(self):
        folder = QFileDialog.getExistingDirectory(self, "Keep downloads in", self.txt_cache.text())
        if folder:
            self.txt_cache.setText(folder)
            self._refresh_cache()

    def _refresh_cache(self):
        self.lbl_cache.setText("{} on disk".format(human_size(Cache(self.txt_cache.text()).size())))

    def _empty(self):
        Cache(self.txt_cache.text()).clear()
        self._refresh_cache()

    def values(self):
        source = normalise_location(self.txt_source.text())
        if source in self.app.known_defaults() or source == normalise_location(self.app.default_source()):
            source = ""                      # the default is not stored: it follows the data if it moves
        return {"source": source, "version": self.cmb_version.currentData() or "",
                "cache_dir": self.txt_cache.text().strip(), "verify": self.chk_verify.isChecked()}


class TerraeBoundariesPlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.config = plugin_config()
        self.repo = None
        self.repo_error = None
        self.fetcher = None
        self.panel = None
        self.toolbar = None
        self.menu = None
        self.actions = []
        self._loaded = False
        self._pointed = None

    # ---- settings --------------------------------------------------------------------------------------------------
    def setting(self, name, default=None, kind=str):
        return QgsSettings().value(KEY + name, default, type=kind)

    def set_setting(self, name, value):
        QgsSettings().setValue(KEY + name, value)

    def source(self):
        """The repository to read: the one the user set, else the default one."""
        chosen = normalise_location(self.setting("source", ""))
        if chosen and chosen not in self.known_defaults():
            return chosen
        return normalise_location(self.default_source())

    def known_defaults(self):
        """The addresses that are not a choice of the user: the one in config.json and the ones the pointer has given.
        A setting equal to one of them (an older version stored the default) follows the data when it moves."""
        seen = [self.config.get("default_source", ""), self.setting("pointed_source", "")] + self.setting("pointed_before", "").split("|")
        return set(normalise_location(x) for x in seen if x)

    def default_source(self):
        """Where the data is now. The address written in config.json can be superseded by a small file (`pointer`) the
        plugin's own repository publishes, so the data can move without a new version of the plugin. The last answer
        is kept for when the file cannot be read."""
        if self._pointed is None:
            self._pointed = self.setting("pointed_source", "") or self.config.get("default_source", "")
            pointer = self.config.get("pointer", "")
            if pointer:
                try:
                    head, _, name = pointer.rpartition("/")
                    found = json.loads(QgisHttpReader(head + "/").read(name).decode("utf-8")).get("source", "")
                    if found and is_url(found):
                        if normalise_location(found) != normalise_location(self.setting("pointed_source", "")):
                            self.set_setting("pointed_before", "|".join(sorted(self.known_defaults())))      # still not a choice
                        self._pointed = found
                        self.set_setting("pointed_source", found)
                except Exception as e:     # no connection, or no pointer yet: the last known address is used
                    log("The data address pointer could not be read ({}); using {}".format(e, self._pointed), INFO)
        return self._pointed

    def cache_dir(self):
        return self.setting("cache_dir", "") or os.path.join(QgsApplication.qgisSettingsDirPath(), "world_admin_boundaries", "cache")

    # ---- QGIS entry points -----------------------------------------------------------------------------------------
    def initGui(self):
        icon = QIcon(os.path.join(HERE, "icons", "terrae.svg"))
        main = self.iface.mainWindow()
        self.act_panel = QAction(icon, "World Administrative Boundaries\u2026", main)
        self.act_panel.setToolTip("World Administrative Boundaries: countries and their first- and second-level divisions (Terrae data)")
        self.act_panel.setCheckable(True)
        self.act_panel.triggered.connect(self.toggle_panel)
        self.menu = QMenu("World Administrative Boundaries", main)
        self.menu.setIcon(icon)
        self.menu.aboutToShow.connect(self._fill_menu)
        self.button = QToolButton()
        self.button.setDefaultAction(self.act_panel)
        self.button.setMenu(self.menu)
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.toolbar = self.iface.addToolBar("World Administrative Boundaries")
        self.toolbar.setObjectName("TerraeBoundariesToolbar")
        self.button_action = self.toolbar.addWidget(self.button)
        self.act_menu = self.menu.menuAction()
        self.iface.addPluginToWebMenu("&World Administrative Boundaries", self.act_panel)
        self.panel = TerraePanel(self.iface, self)
        self.panel.setVisible(False)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel.visibilityChanged.connect(self._panel_visibility)

    def unload(self):
        if self.panel is not None:
            self.panel.picker.stop(True)
            if self.fetcher is not None and self.fetcher.busy:
                self.fetcher.cancel()
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
        self.iface.removePluginWebMenu("&World Administrative Boundaries", self.act_panel)
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        self.menu = None

    # ---- the repository --------------------------------------------------------------------------------------------
    def ensure_repository(self, force=False):
        """Reads the repository the first time it is needed (or again, after the settings change)."""
        if self._loaded and not force:
            return self.repo
        self._loaded = True
        self.repo, self.repo_error = None, None
        try:
            self.repo = open_repository(self.source(), self.setting("version", ""), self.cache_dir())
        except RepositoryError as e:
            self.repo_error = str(e)
            log(self.repo_error, WARNING)
        except Exception as e:        # a manifest this version cannot read must not stop QGIS
            self.repo_error = "The repository could not be read: {}".format(e)
            log(self.repo_error, WARNING)
        self.fetcher = None
        if self.repo is not None:
            self.fetcher = Fetcher(self.repo, Cache(self.cache_dir()), self.setting("verify", True, bool), self.iface.mainWindow())
        if self.panel is not None:
            self.panel.set_repository(self.repo, self.fetcher, self.repo_error)
        return self.repo

    def open_settings(self):
        dlg = SettingsDialog(self, self.iface.mainWindow())
        if run_modal(dlg):
            for k, v in dlg.values().items():
                self.set_setting(k, v)
            self.ensure_repository(True)
            if self.repo is None:
                self.iface.messageBar().pushMessage("Terrae", self.repo_error or "The repository could not be read.", WARNING, 12)

    # ---- the panel and the menu ------------------------------------------------------------------------------------
    def toggle_panel(self, on=None):
        show = (not self.panel.isVisible()) if on is None or isinstance(on, bool) is False else on
        if show:
            self.ensure_repository()
        self.panel.setVisible(show)
        if show:
            self.panel.raise_()

    def _panel_visibility(self, visible):
        self.act_panel.setChecked(visible)

    def _show_panel(self):
        self.ensure_repository()
        self.panel.setVisible(True)
        self.panel.raise_()

    def _fill_menu(self):
        """The menu is made from the manifest each time it opens: datasets, levels and presets are the repository's."""
        m = self.menu
        m.clear()
        repo = self.ensure_repository()
        a = m.addAction("Choose and download\u2026")
        a.triggered.connect(self._show_panel)
        if repo is None:
            m.addSeparator()
            a = m.addAction(self.repo_error or "The repository could not be read")
            a.setEnabled(False)
        else:
            a = m.addAction("Pick countries on the map")
            a.triggered.connect(self._pick)
            m.addSeparator()
            for preset in repo.presets:
                a = m.addAction(preset["title"])
                a.setToolTip(preset.get("description", ""))
                a.triggered.connect(lambda _=False, p=preset: (self._show_panel(), self.panel.run_preset(p)))
            if repo.presets:
                m.addSeparator()
            for d in repo.datasets:
                sub = m.addMenu(d["title"])
                sub.setToolTipsVisible(True)
                for lv in repo.levels:
                    if lv.id not in d["trees"]:
                        continue
                    lsub = sub.addMenu("{} \u2014 {}".format(LEVEL_NAMES.get(lv.id, lv.id), level_note(lv)))
                    for text, geoms in (("Polygons", ["polygons"]), ("Lines", ["lines"]), ("Polygons and lines", ["polygons", "lines"])):
                        size = sum(d["world_files"][lv.id][g]["gpkg"]["bytes"] for g in geoms)
                        a = lsub.addAction("{}  ({}, whole world)".format(text, human_size(size)))
                        a.triggered.connect(lambda _=False, di=d["id"], li=lv.id, gg=geoms: (self._show_panel(), self.panel.quick_add(di, li, gg)))
        m.addSeparator()
        a = m.addAction("Settings\u2026")
        a.triggered.connect(self.open_settings)
        a = m.addAction("Reload the repository")
        a.triggered.connect(lambda: self.ensure_repository(True))
        a = m.addAction("Help")
        a.triggered.connect(self.show_help)

    def _pick(self):
        self._show_panel()
        if self.panel.repo is not None:
            self.panel.rad_select.setChecked(True)
            self.panel.btn_pick.setChecked(True)

    def show_help(self):
        run_modal(self.help_dialog())

    def help_dialog(self):
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle("World Administrative Boundaries \u2014 help")
        dlg.resize(720, 680)
        col = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.document().setDocumentMargin(18)
        try:
            with open(os.path.join(HERE, "README.md"), "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = "The help file is missing."
        if hasattr(view, "setMarkdown"):
            view.setMarkdown(text)
        else:
            view.setPlainText(text)
        col.addWidget(view)
        return dlg
