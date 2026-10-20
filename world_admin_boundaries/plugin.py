# -*- coding: utf-8 -*-
"""The plugin: a toolbar button with a menu, the panel, the settings, and the one repository they all read."""
import json
import os
import time
import traceback

from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                                 QLineEdit, QMenu, QPushButton, QTextBrowser, QToolButton, QVBoxLayout)
from qgis.core import QgsApplication, QgsSettings, QgsTask

from .compat import QAction, INFO, WARNING, CRITICAL
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


HEDGE_MS = 8000          # a reading that is not over by then is also started a second time: the first answer wins
MAX_TRIES = 3
POINTER_TIMEOUT_MS = 6000


def fetch_pointer(pointer):
    """The address the plugin's own repository says the data is at now, or "" (it reads one small file: it waits for the network)."""
    head, _, name = pointer.rpartition("/")
    reader = QgisHttpReader(head + "/")
    reader.timeout_ms = POINTER_TIMEOUT_MS         # it is only a shortcut: the last known address does as well
    found = json.loads(reader.read(name).decode("utf-8")).get("source", "")
    return found if found and is_url(found) else ""


def probe(custom, pointer, fallback, version, cache_dir):
    """Reads the repository. All the waiting for the network is here, with nothing of QGIS's settings or widgets in it, so that
    it can run in a background task. `custom` is the address the user set ("" = the default one: the pointer says where it is,
    `fallback` is the last address known when the pointer cannot be read). Returns a dict: repo, error, kind of error, the
    address the pointer gave, whether the pointer was tried."""
    result = {"repo": None, "error": None, "kind": None, "found": "", "pointer_tried": bool(pointer and not custom)}
    started = time.time()
    try:
        location = custom or fallback
        if result["pointer_tried"]:
            try:
                result["found"] = fetch_pointer(pointer)
                location = result["found"] or location
            except Exception as e:     # no connection, or no pointer yet: the last known address is used
                log("The data address pointer could not be read ({}); using {}".format(e, fallback), INFO)
        pointed = time.time()
        result["repo"] = open_repository(location, version, cache_dir)
        log("Repository read: pointer {:.1f} s, repository {:.1f} s ({})".format(pointed - started, time.time() - pointed, location), INFO)
    except RepositoryError as e:
        result["error"], result["kind"] = str(e), e.kind
    except Exception as e:        # a manifest this version cannot read must not stop QGIS
        result["error"], result["kind"] = "The repository could not be read: {}".format(e), "error"
    return result


def task_flags():
    """The flag that keeps a short background task out of QGIS's task list, where this QGIS has it."""
    return getattr(getattr(QgsTask, "Flag", QgsTask), "Hidden", None)


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
        self._loaded = False                # a reading of the repository has finished (well or not)
        self._pointed = None
        self.checking = False               # a reading is running in the background
        self._task = None
        self._generation = 0                # a reading superseded by a newer one is ignored when it ends
        self._inflight = 0                  # background readings of this generation not yet over
        self._tries = 0
        self._waiting = []                  # what to do when the repository is there (menu actions chosen while it was read)
        self._announce = False              # tell the user in the message bar if the next reading fails (after the settings)

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
                    self._adopt_pointer(fetch_pointer(pointer))
                except Exception as e:     # no connection, or no pointer yet: the last known address is used
                    log("The data address pointer could not be read ({}); using {}".format(e, self._pointed), INFO)
        return self._pointed

    def _adopt_pointer(self, found):
        """Keeps the address the pointer gave as the default one (and the one it replaces as 'still not a choice')."""
        if found and is_url(found):
            if normalise_location(found) != normalise_location(self.setting("pointed_source", "")):
                self.set_setting("pointed_before", "|".join(sorted(self.known_defaults())))
            self._pointed = found
            self.set_setting("pointed_source", found)

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
        self._starting = True                   # adding the panel shows it for an instant: that is not the user opening it
        self.panel.visibilityChanged.connect(self._panel_visibility)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        if self.setting("shown_once", False, bool):
            self.panel.setVisible(False)        # QGIS opens it again by itself if it was open when QGIS was closed
        else:
            self.set_setting("shown_once", True)
            self.check_async()                  # the first time after installing it the panel stays open, so the plugin is seen
        self._starting = False

    def unload(self):
        self._generation += 1                   # a reading still running has nobody to tell
        self._inflight = 0
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception as e:
                log("The reading of the repository could not be cancelled: {}".format(e), WARNING)
            self._task = None
        self.checking = False
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
    def _probe_args(self):
        chosen = normalise_location(self.setting("source", ""))
        custom = chosen if chosen and chosen not in self.known_defaults() else ""
        fallback = self._pointed or self.setting("pointed_source", "") or self.config.get("default_source", "")
        return custom, "" if custom else self.config.get("pointer", ""), fallback, self.setting("version", ""), self.cache_dir()

    def check_async(self, force=False, then=None, announce=False):
        """Reads the repository in the background, the first time it is needed (or again, with force). The panel shows
        "checking" meanwhile and QGIS stays free. `then` is called when the repository is there; nothing happens if it is not."""
        if then is not None:
            self._waiting.append(then)
        if announce:
            self._announce = True
        if self.checking:
            return
        if self._loaded and not force:
            self._run_waiting()
            return
        self.checking = True
        self._generation += 1
        self._inflight = 0
        self._tries = 0
        if self.panel is not None:
            self.panel.set_checking()
        self._launch()

    def _launch(self):
        """Starts one reading in the background. If it is slow, `_slow` starts another beside it: a request that hangs
        (a connection that stalls) does not hold the panel, because the new one usually answers at once."""
        generation = self._generation
        self._tries += 1
        self._inflight += 1
        QTimer.singleShot(HEDGE_MS, lambda: self._slow(generation))
        args = self._probe_args()
        options = {"on_finished": lambda exception, result: self._checked(generation, exception, result)}
        if task_flags() is not None:
            options["flags"] = task_flags()
        try:
            self._task = QgsTask.fromFunction("Reading the World Administrative Boundaries repository", lambda _task: probe(*args), **options)
            QgsApplication.taskManager().addTask(self._task)
        except Exception as e:      # no background task: read it here, and say so
            log("No background task ({}); the repository is read in the foreground.".format(e), WARNING)
            self._task = None
            self._checked(generation, None, probe(*args))

    def _slow(self, generation):
        try:
            if generation == self._generation and self.checking and self._tries < MAX_TRIES:
                log("The repository is slow to answer; trying again beside the first attempt (attempt {}).".format(self._tries + 1), INFO)
                self._launch()
        except Exception:
            log(traceback.format_exc(), CRITICAL)

    def _checked(self, generation, exception, result):
        """The reading is over (called in the main thread). Nothing may escape from here: it is a slot."""
        try:
            if generation != self._generation or not self.checking:
                return                      # an old reading, or one that lost the race to another of the same request
            self._inflight -= 1
            if exception is not None or not isinstance(result, dict):
                result = {"repo": None, "error": "The repository could not be read: {}".format(exception or "no answer"),
                          "kind": "error", "found": "", "pointer_tried": False}
            if result["repo"] is None and self._inflight > 0:
                return                      # this attempt failed, another is still running: wait for it
            self.checking = False
            self._task = None
            self._apply(result)
        except Exception:
            self.checking = False
            log(traceback.format_exc(), CRITICAL)

    def _apply(self, result):
        """Takes a reading as the state of the plugin: the repository, its fetcher, the panel, and what was waiting for it."""
        self._loaded = True
        self._adopt_pointer(result.get("found", ""))
        if result.get("pointer_tried") and self._pointed is None:
            self._pointed = self.setting("pointed_source", "") or self.config.get("default_source", "")
        self.repo, self.repo_error = result["repo"], result["error"]
        self.fetcher = None
        state, detail = None, ""
        if self.repo_error:
            log(self.repo_error, WARNING)
            state, detail = ("not_connected" if result["kind"] == "network" else "error"), self.repo_error
        if self.repo is not None:
            self.fetcher = Fetcher(self.repo, Cache(self.cache_dir()), self.setting("verify", True, bool), self.iface.mainWindow())
            if getattr(self.repo.reader, "offline", False):
                state = "offline"
        if self.panel is not None:
            self.panel.set_repository(self.repo, self.fetcher, self.repo_error, state, detail)
        announce, self._announce = self._announce, False
        if self.repo is None and announce:
            self.iface.messageBar().pushMessage("Terrae", self.repo_error or "The repository could not be read.", WARNING, 12)
        self._run_waiting()

    def _run_waiting(self):
        waiting, self._waiting = self._waiting, []
        if self.repo is None:
            return
        for action in waiting:
            try:
                action()
            except Exception:
                log(traceback.format_exc(), CRITICAL)

    def when_ready(self, action):
        """Runs `action` when the repository is there: at once if it is, after the reading if it is not."""
        self.check_async(then=action)

    def wait_ready(self, timeout=120.0):
        """Lets QGIS's event loop run until the reading is over (for tests and scripts)."""
        end = time.time() + timeout
        while self.checking and time.time() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        return not self.checking

    def ensure_repository(self, force=False):
        """The same reading, in the foreground (for tests and scripts): returns the repository, or None."""
        if self._loaded and not force and not self.checking:
            return self.repo
        self._generation += 1               # a reading in the background is superseded
        self._inflight = 0
        self.checking = False
        self._apply(probe(*self._probe_args()))
        return self.repo

    def open_settings(self):
        dlg = SettingsDialog(self, self.iface.mainWindow())
        if dlg.exec():
            for k, v in dlg.values().items():
                self.set_setting(k, v)
            self.check_async(True, announce=True)

    # ---- the panel and the menu ------------------------------------------------------------------------------------
    def toggle_panel(self, on=None):
        show = (not self.panel.isVisible()) if on is None or isinstance(on, bool) is False else on
        if show:
            self.check_async()
        self.panel.setVisible(show)
        if show:
            self.panel.raise_()

    def _panel_visibility(self, visible):
        self.act_panel.setChecked(visible)
        if visible and not getattr(self, "_starting", False):       # also when QGIS shows the panel again on its own, as it was left
            self.check_async()

    def _show_panel(self):
        self.check_async()
        self.panel.setVisible(True)
        self.panel.raise_()

    def _fill_menu(self):
        """The menu is made from the manifest each time it opens: datasets, levels and presets are the repository's."""
        m = self.menu
        m.clear()
        if not self._loaded:
            self.check_async()
        repo = self.repo
        a = m.addAction("Choose and download\u2026")
        a.triggered.connect(self._show_panel)
        if repo is None:
            m.addSeparator()
            a = m.addAction("Checking connection\u2026" if self.checking else (self.repo_error or "The repository could not be read"))
            a.setEnabled(False)
        else:
            a = m.addAction("Pick countries on the map")
            a.triggered.connect(self._pick)
            m.addSeparator()
            for preset in repo.presets:
                a = m.addAction(preset["title"])
                a.setToolTip(preset.get("description", ""))
                a.triggered.connect(lambda _=False, p=preset: (self._show_panel(), self.when_ready(lambda: self.panel.run_preset(p))))
            if repo.presets:
                m.addSeparator()
            options = (("Polygons", ["polygons"]), ("Lines", ["lines"]), ("Polygons and lines", ["polygons", "lines"]))
            for title, entries in repo.selector():
                m.addSection(title)
                for d in entries:
                    sub = m.addMenu(repo.label(d))
                    sub.setToolTipsVisible(True)
                    fixed = d.get("fixed_geometries")
                    choices = [o for o in options if o[1] == list(fixed)] if fixed else options
                    for lv in repo.levels:
                        if lv.id not in d["trees"]:
                            continue
                        lsub = sub.addMenu("{} \u2014 {}".format(LEVEL_NAMES.get(lv.id, lv.id), level_note(lv)))
                        for text, geoms in choices:
                            size = sum(d["world_files"][lv.id][g]["gpkg"]["bytes"] for g in geoms)
                            a = lsub.addAction("{}  ({}, whole world)".format(text, human_size(size)))
                            a.triggered.connect(lambda _=False, di=d["id"], li=lv.id, gg=geoms:
                                                (self._show_panel(), self.when_ready(lambda: self.panel.quick_add(di, li, gg))))
        m.addSeparator()
        a = m.addAction("Settings\u2026")
        a.triggered.connect(self.open_settings)
        a = m.addAction("Reload the repository")
        a.triggered.connect(lambda: self.check_async(True))
        a = m.addAction("Help")
        a.triggered.connect(self.show_help)

    def _pick(self):
        self._show_panel()
        self.when_ready(self._start_pick)

    def _start_pick(self):
        if self.panel.repo is not None:
            self.panel.rad_select.setChecked(True)
            self.panel.btn_pick.setChecked(True)

    def show_help(self):
        self.help_dialog().exec()

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
