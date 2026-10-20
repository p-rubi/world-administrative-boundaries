# -*- coding: utf-8 -*-
"""The panel: what to get (dataset, level of detail, geometry, format), for where (the world, or countries and their
divisions chosen in a list, by search or on the map), and the two things to do with it (add to the map, or save)."""
import os
import traceback

from qgis.PyQt.QtCore import Qt, QTimer, QCoreApplication, QPointF, QSize
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QFileDialog, QFormLayout, QFrame, QGroupBox,
                                 QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                                 QRadioButton, QScrollArea, QSizePolicy, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from qgis.core import QgsCoordinateTransform, QgsProject, QgsRectangle

from .compat import INFO, WARNING, CRITICAL, SUCCESS
from .loader import add_layers, make_layer
from .map_pick import MapPicker
from .net import copy_to, log
from .repository import FORMAT_NAMES, LEVEL_NAMES, RepositoryError, human_size, is_url

ROLE = Qt.ItemDataRole.UserRole
CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked
PARTIAL = Qt.CheckState.PartiallyChecked
LOADABLE = ("gpkg", "geojson", "kml")          # the formats QGIS opens here; the others are saved, not added
LARGE = 400 * 1000 * 1000                       # ask before downloading more than this
PICK_DEPTHS = ["chooses countries", "chooses first-level divisions", "chooses second-level divisions"]


# a manifest preset the plugin runs as one of its views: preset id -> (view, level of detail)
PRESET_VIEWS = {"world_countries": ("cartographic", "low")}
STATE_COLOURS = {"checking": "#8a8f98", "connected": "#2e9e5b", "local": "#4a7c9b", "offline": "#d99a1a",
                 "not_connected": "#d9534f", "error": "#c0392b"}
RETRY_STATES = ("offline", "not_connected", "error")      # the states in which trying again can change something


def status_icon(state):
    """A small round icon for the state of the connection: colour and a sign inside, so it reads without colour too."""
    size = 32
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(STATE_COLOURS.get(state, STATE_COLOURS["checking"])))
    p.drawEllipse(2, 2, 28, 28)
    white = QColor("white")
    pen = QPen(white, 3.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    if state in ("connected", "local"):                                   # a tick
        p.drawLine(QPointF(9, 17), QPointF(14, 22))
        p.drawLine(QPointF(14, 22), QPointF(23, 11))
    elif state == "offline":                                              # a dash
        p.drawLine(QPointF(9, 16), QPointF(23, 16))
    elif state == "not_connected":                                        # a cross
        p.drawLine(QPointF(10, 10), QPointF(22, 22))
        p.drawLine(QPointF(22, 10), QPointF(10, 22))
    elif state == "error":                                                # an exclamation mark
        p.drawLine(QPointF(16, 8), QPointF(16, 18))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(white)
        p.drawEllipse(QPointF(16, 23.5), 2.2, 2.2)
    else:                                                                 # checking: three dots
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(white)
        for x in (9, 16, 23):
            p.drawEllipse(QPointF(x, 16), 2.4, 2.4)
    p.end()
    return QIcon(px)


def level_note(lv):
    """The label of a level, as it reads after its name: about 150 m, or a phrase that keeps its capital."""
    return lv.label[0].lower() + lv.label[1:] if lv.label.startswith("About") else lv.label


class TerraePanel(QDockWidget):

    def __init__(self, iface, app, parent=None):
        super().__init__("World Administrative Boundaries", parent or iface.mainWindow())
        self.setObjectName("TerraeBoundariesPanel")
        self.iface = iface
        self.app = app                      # the plugin: repository(), fetcher(), open_settings()
        self.repo = None
        self.fetcher = None
        self._job = None                    # (callback, description) of the running fetch
        self._building = False
        self._geometry_choice = None        # the geometry chosen before a layer that fixes its own (a view) was selected
        self.state = "checking"
        self.confirm = self._confirm        # replaced in tests
        self.picker = MapPicker(iface, self)
        self.picker.picked.connect(self._picked)
        self.picker.opened.connect(self._open_divisions)
        self.picker.message.connect(lambda t: self._status(t))
        self.picker.stopped.connect(self._pick_stopped)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._update_estimate)
        self._build()

    # ---- widgets ---------------------------------------------------------------------------------------------------
    def _build(self):
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(8, 8, 8, 8)

        head = QHBoxLayout()
        self.btn_state = QToolButton()
        self.btn_state.setAutoRaise(True)
        self.btn_state.setIconSize(QSize(16, 16))
        self.btn_state.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_state.clicked.connect(self._state_clicked)
        self.lbl_release = QLabel("Checking connection\u2026")
        self.lbl_release.setWordWrap(True)
        self.lbl_release.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)     # a long address must not widen the panel
        self.btn_retry = QToolButton()
        self.btn_retry.setText("Retry")
        self.btn_retry.setToolTip("Try to reach the data repository again")
        self.btn_retry.clicked.connect(lambda: self.app.check_async(True))
        self.btn_source = QToolButton()
        self.btn_source.setText("Settings\u2026")
        self.btn_source.setToolTip("Data source, release and where downloads are kept")
        self.btn_source.clicked.connect(lambda: self.app.open_settings())
        head.addWidget(self.btn_state)
        head.addWidget(self.lbl_release, 1)
        head.addWidget(self.btn_retry)
        head.addWidget(self.btn_source)
        col.addLayout(head)

        form = QFormLayout()
        self.cmb_dataset = QComboBox()
        self.cmb_level = QComboBox()
        self.cmb_geometry = QComboBox()
        for text, value in (("Polygons (areas)", "polygons"), ("Lines (boundaries)", "lines"), ("Polygons and lines", "both")):
            self.cmb_geometry.addItem(text, value)
        self.cmb_format = QComboBox()
        for combo in (self.cmb_dataset, self.cmb_level, self.cmb_geometry, self.cmb_format):
            # the panel is a side panel: the selectors take the width there is instead of asking for their longest entry
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(22)
        form.addRow("Layer", self.cmb_dataset)
        form.addRow("Level of detail", self.cmb_level)
        form.addRow("Geometry", self.cmb_geometry)
        form.addRow("Format", self.cmb_format)
        self.chk_style = QCheckBox("Apply Terrae symbology")
        self.chk_style.setToolTip("Style the new layers like the Terrae maps. Turn it off to get QGIS's default look.")
        self.chk_style.setChecked(self.app.setting("symbology", True, bool))
        self.chk_style.toggled.connect(lambda on: self.app.set_setting("symbology", on))
        form.addRow("", self.chk_style)
        col.addLayout(form)
        self.set_state("checking")
        self.lbl_about = QLabel("")
        self.lbl_about.setWordWrap(True)
        small = self.lbl_about.font()
        small.setItalic(True)
        self.lbl_about.setFont(small)
        col.addWidget(self.lbl_about)

        cov = QHBoxLayout()
        self.rad_world = QRadioButton("Whole world")
        self.rad_select = QRadioButton("Chosen countries")
        self.rad_world.setChecked(True)
        cov.addWidget(self.rad_world)
        cov.addWidget(self.rad_select)
        cov.addStretch(1)
        col.addLayout(cov)

        self.grp_select = QGroupBox()
        g = QVBoxLayout(self.grp_select)
        g.setContentsMargins(6, 6, 6, 6)
        row = QHBoxLayout()
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search a country by name or code")
        self.txt_search.setClearButtonEnabled(True)
        self.btn_pick = QPushButton("Pick on map")
        self.btn_pick.setCheckable(True)
        self.btn_pick.setToolTip("Click countries on the map to choose them. With a layer of divisions, the first click on a "
                                 "country shows its divisions and the next clicks choose among them.")
        self.cmb_pick = QComboBox()
        self.cmb_pick.setToolTip("What a click on the map chooses")
        self.cmb_pick.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        row.addWidget(self.txt_search, 1)
        g.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.btn_pick)
        row.addWidget(self.cmb_pick)
        row.addStretch(1)
        g.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Name", "Divisions"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setMinimumHeight(150)
        g.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.lbl_selected = QLabel("Nothing chosen")
        self.btn_clear = QToolButton()
        self.btn_clear.setText("Clear")
        row.addWidget(self.lbl_selected, 1)
        row.addWidget(self.btn_clear)
        g.addLayout(row)
        col.addWidget(self.grp_select, 1)

        self.lbl_estimate = QLabel("")
        self.lbl_estimate.setWordWrap(True)
        col.addWidget(self.lbl_estimate)
        row = QHBoxLayout()
        self.btn_add = QPushButton("Add to map")
        self.btn_add.setDefault(True)
        self.btn_save = QPushButton("Save to folder\u2026")
        self.btn_save.setToolTip("Download the files into a folder of yours, without adding them to the map")
        row.addWidget(self.btn_add, 1)
        row.addWidget(self.btn_save)
        col.addLayout(row)
        row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(10)
        self.btn_cancel = QToolButton()
        self.btn_cancel.setText("Cancel")
        row.addWidget(self.bar, 1)
        row.addWidget(self.btn_cancel)
        col.addLayout(row)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.lbl_status)
        # in a short window the panel scrolls instead of squeezing its rows into one another
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        scroll.setMinimumWidth(min(body.minimumSizeHint().width() + 26, 460))       # wide enough for its rows: nothing is cut
        self.setWidget(scroll)
        self._set_busy(False)

        self.cmb_dataset.currentIndexChanged.connect(self._dataset_changed)
        self.cmb_level.currentIndexChanged.connect(self._level_changed)
        self.cmb_geometry.currentIndexChanged.connect(self._schedule)
        self.cmb_format.currentIndexChanged.connect(self._format_changed)
        self.rad_world.toggled.connect(self._coverage_changed)
        self.txt_search.textChanged.connect(self._filter)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemChanged.connect(self._item_changed)
        self.btn_clear.clicked.connect(self.clear_selection)
        self.btn_pick.toggled.connect(self._pick_toggled)
        self.cmb_pick.currentIndexChanged.connect(lambda i: self.picker.set_depth(max(i, 0)))
        self.btn_add.clicked.connect(lambda: self.add_to_map())
        self.btn_save.clicked.connect(lambda: self.save_to_folder())
        self.btn_cancel.clicked.connect(self.cancel)
        self._coverage_changed()

    # ---- the repository --------------------------------------------------------------------------------------------
    def _controls(self):
        return (self.cmb_dataset, self.cmb_level, self.cmb_geometry, self.cmb_format, self.rad_world, self.rad_select,
                self.btn_add, self.btn_save)

    def set_checking(self):
        """The repository is being read: the controls wait, and the indicator says so."""
        for w in self._controls():
            w.setEnabled(False)
        self.grp_select.setEnabled(False)
        self.set_state("checking")

    def set_state(self, state, detail=""):
        """The connection indicator: an icon, a short text, and a tooltip with the details."""
        self.state = state
        repo = self.repo if state in ("connected", "local", "offline") else None
        where = ""
        if repo is not None:
            where = repo.location if is_url(repo.location) else "folder " + repo.location
        release = "Terrae {}".format(repo.version) if repo is not None else "Terrae"
        text = {"checking": "Checking connection\u2026", "not_connected": "Not connected", "error": "Connection error",
                "local": release + " (local folder)", "offline": release + " (offline)"}.get(state, release)
        credit = repo.manifest.get("attribution", "") if repo is not None else ""
        tips = {
            "checking": "Checking connection\u2026\nReading the list of data from the repository.",
            "connected": "Connected\nRelease {} \u00b7 {}\n{}".format(repo.version if repo else "", credit, where),
            "local": "Local folder\nRelease {} \u00b7 {}\n{}".format(repo.version if repo else "", credit, where),
            "offline": "Offline \u2014 the repository could not be reached; the saved copy of its list is used (release {}).\n"
                       "Files already downloaded stay available. Click to try again.\n{}".format(repo.version if repo else "", where),
            "not_connected": "Not connected \u2014 the data service could not be reached. Check the internet connection, "
                             "or the proxy settings of QGIS. Click to try again.{}".format("\n" + detail if detail else ""),
            "error": "Connection error \u2014 {}\nClick to try again.".format(detail or "the repository could not be read."),
        }
        self.btn_state.setIcon(status_icon(state))
        self.lbl_release.setText(text)
        for w in (self.btn_state, self.lbl_release):
            w.setToolTip(tips.get(state, text))
        self.btn_state.setEnabled(True)
        self.btn_retry.setVisible(state in RETRY_STATES)

    def _state_clicked(self):
        if self.state in RETRY_STATES:
            self.app.check_async(True)

    def set_repository(self, repo, fetcher, error=None, state=None, detail=""):
        if self.fetcher is not None and self.fetcher is not fetcher:
            for signal in (self.fetcher.progress, self.fetcher.finished, self.fetcher.failed, self.fetcher.canceled):
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass
        self.picker.stop(True)
        self.btn_pick.setChecked(False)
        self.repo, self.fetcher = repo, fetcher
        self._building = True
        self.cmb_dataset.clear()
        self.tree.clear()
        self._building = False
        ok = repo is not None
        for w in self._controls():
            w.setEnabled(ok)
        self.grp_select.setEnabled(ok and self.rad_select.isChecked())
        if not ok:
            self.set_state(state or "error", detail or error or "")
            self.lbl_about.setText("")
            self.lbl_estimate.setText("")
            self._status(error or "Set the data source to start.", WARNING)
            return
        fetcher.progress.connect(self._on_progress)
        fetcher.finished.connect(self._on_finished)
        fetcher.failed.connect(self._on_failed)
        fetcher.canceled.connect(self._on_canceled)
        self.set_state(state or ("connected" if is_url(repo.location) else "local"), detail)
        self._building = True
        first = -1
        for title, entries in repo.selector():
            self.cmb_dataset.addItem(title)                 # a heading: bold, and not a choice
            heading = self.cmb_dataset.model().item(self.cmb_dataset.count() - 1)
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            font = QFont(self.cmb_dataset.font())
            font.setBold(True)
            heading.setFont(font)
            for d in entries:
                if first < 0:
                    first = self.cmb_dataset.count()
                self.cmb_dataset.addItem(repo.label(d), d["id"])
                self.cmb_dataset.setItemData(self.cmb_dataset.count() - 1, d.get("description", ""), Qt.ItemDataRole.ToolTipRole)
        self.cmb_dataset.setCurrentIndex(max(first, 0))
        self._building = False
        self._dataset_changed()
        self._status("{} \u00b7 {} countries and territories, {:,} first-level and {:,} second-level divisions."
                     .format(repo.manifest.get("licence", ""), repo.manifest["counts"]["units"], repo.manifest["counts"]["adm1"],
                             repo.manifest["counts"]["adm2"]))

    def dataset_id(self):
        return self.cmb_dataset.currentData()

    def level_id(self):
        return self.cmb_level.currentData()

    def geometries(self):
        v = self.cmb_geometry.currentData()
        return ["polygons", "lines"] if v == "both" else [v]

    def format_id(self):
        return self.cmb_format.currentData() or "gpkg"

    def coverage(self):
        return "world" if self.rad_world.isChecked() else "places"

    def _fixed(self):
        """Does the selected layer fix its own geometry (a view does)?"""
        return bool(self.repo is not None and self.dataset_id() and self.repo.fixed_geometries(self.dataset_id()))

    def _apply_fixed_geometry(self, d):
        """A view is made of given geometries: the selector shows them and is locked; the user's own choice comes back after."""
        fixed = d.get("fixed_geometries")
        self._building = True
        if fixed:
            if self._geometry_choice is None:
                self._geometry_choice = self.cmb_geometry.currentData()
            self.cmb_geometry.setCurrentIndex(max(self.cmb_geometry.findData("both" if len(fixed) > 1 else fixed[0]), 0))
        elif self._geometry_choice is not None:
            self.cmb_geometry.setCurrentIndex(max(self.cmb_geometry.findData(self._geometry_choice), 0))
            self._geometry_choice = None
        self._building = False
        self.cmb_geometry.setEnabled(not fixed and self.repo is not None and self._job is None)

    def _dataset_changed(self, *_):
        if self._building or self.repo is None or self.dataset_id() is None:
            return
        d = self.repo.dataset(self.dataset_id())
        self._apply_fixed_geometry(d)
        keep = self.level_id()
        self._building = True
        self.cmb_level.clear()
        for lv in self.repo.levels:
            if lv.id not in d["trees"]:
                continue
            text = "{} \u2014 {}".format(LEVEL_NAMES.get(lv.id, lv.id), level_note(lv))
            self.cmb_level.addItem(text, lv.id)
        i = self.cmb_level.findData(keep)
        self.cmb_level.setCurrentIndex(i if i >= 0 else 0)
        depth = self.repo.depth(d["id"])
        self.cmb_pick.clear()
        for name in PICK_DEPTHS[:depth + 1]:
            self.cmb_pick.addItem(name)
        self.cmb_pick.setCurrentIndex(depth)
        self.cmb_pick.setVisible(depth > 0)
        self._building = False
        self.picker.set_depth(depth)
        self._fill_tree()
        self._level_changed()

    def _level_changed(self, *_):
        if self._building or self.repo is None or self.level_id() is None:
            return
        d = self.repo.dataset(self.dataset_id())
        lv = self.repo.level(self.level_id())
        note = "" if lv.open else " Not part of the open release."
        if d.get("level_notes", {}).get(lv.id):
            note += " " + d["level_notes"][lv.id]
        self.lbl_about.setText("{} {}: {}.{}".format(d.get("description", ""), LEVEL_NAMES.get(lv.id, lv.id), lv.scale, note))
        self._fill_formats()

    def _coverage_changed(self, *_):
        self.grp_select.setEnabled(self.rad_select.isChecked() and self.repo is not None)
        if self.rad_world.isChecked() and self.btn_pick.isChecked():
            self.btn_pick.setChecked(False)
        self._fill_formats()

    def _fill_formats(self):
        if self.repo is None or self.dataset_id() is None or self.level_id() is None:
            return
        keep = self.cmb_format.currentData()
        self._building = True
        self.cmb_format.clear()
        for fmt in self.repo.formats(self.dataset_id(), self.level_id(), self.coverage()):
            self.cmb_format.addItem(FORMAT_NAMES.get(fmt, fmt) + ("" if fmt in LOADABLE else " (save only)"), fmt)
        i = self.cmb_format.findData(keep)
        self.cmb_format.setCurrentIndex(i if i >= 0 else 0)
        self._building = False
        self._format_changed()

    def _format_changed(self, *_):
        if self._building:
            return
        self.btn_add.setEnabled(self.repo is not None and self.format_id() in LOADABLE and self._job is None)
        self._schedule()

    # ---- the list of countries and divisions -------------------------------------------------------------------------
    def _fill_tree(self):
        self._building = True
        self.tree.clear()
        d = self.repo.dataset(self.dataset_id())
        depth = self.repo.depth(d["id"])
        self.tree.setHeaderLabels(["Name", "Divisions" if depth else "Code"])
        items = []
        for e in self.repo.entities(d["id"]):
            it = QTreeWidgetItem()
            it.setText(0, e["name"])
            it.setData(0, ROLE, ("unit", e["key"]))
            it.setToolTip(0, "{} ({})".format(e["name"], e["key"]))
            flags = it.flags() | Qt.ItemFlag.ItemIsUserCheckable
            n = 0
            if depth == 1:
                n = e.get("adm1_divisions", 0)
            elif depth == 2:
                n = e.get("adm2_divisions", 0) or e.get("adm1_divisions", 0)
            if depth and n:
                flags |= Qt.ItemFlag.ItemIsAutoTristate
                it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                it.setText(1, "{:,}".format(e.get("adm1_divisions", 0) if depth == 1 else e.get("adm2", 0)))
            else:
                it.setText(1, e["key"] if not depth else "\u2014")
            it.setFlags(flags)
            it.setCheckState(0, UNCHECKED)
            items.append(it)
        self.tree.addTopLevelItems(items)
        self._building = False
        self._filter(self.txt_search.text())
        self._schedule()

    def _top(self, key):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, ROLE)[1] == key:
                return it
        return None

    def _load_children(self, item):
        """Fills a country with its divisions the first time it is opened (read from its hierarchy file)."""
        kind, key = item.data(0, ROLE)
        depth = self.repo.depth(self.dataset_id())
        if kind != "unit" or item.childCount() or depth == 0:
            return
        if item.childIndicatorPolicy() != QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator:
            return
        try:
            h = self.repo.hierarchy(key)
        except RepositoryError as e:
            self._status(str(e), WARNING)
            return
        state = CHECKED if item.checkState(0) == CHECKED else UNCHECKED
        self._building = True
        kids = []
        for a in h["adm1"]:
            c = QTreeWidgetItem()
            c.setText(0, a["name"] + ("" if a["kind"] == "division" else "  [{}]".format(a["kind"].replace("_", " "))))
            c.setToolTip(0, "{}{} ({})".format(a["name"], " / " + a["local_name"] if a.get("local_name") else "", a["id"]))
            c.setData(0, ROLE, ("adm1", a["id"]))
            flags = c.flags() | Qt.ItemFlag.ItemIsUserCheckable
            subs = a.get("adm2") or []
            if depth == 2 and len(subs) > 1:
                flags |= Qt.ItemFlag.ItemIsAutoTristate
                c.setText(1, "{:,}".format(len(subs)))
                for b in subs:
                    s = QTreeWidgetItem(c)
                    s.setText(0, b["name"] + ("" if b["kind"] == "division" else "  [{}]".format(b["kind"].replace("_", " "))))
                    s.setToolTip(0, "{}{} ({})".format(b["name"], " / " + b["local_name"] if b.get("local_name") else "", b["id"]))
                    s.setData(0, ROLE, ("adm2", b["id"]))
                    s.setFlags(s.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    s.setCheckState(0, state)
            c.setFlags(flags)
            c.setCheckState(0, state)
            kids.append(c)
        item.addChildren(kids)
        self._building = False

    def _expanded(self, item):
        if item.parent() is None and not item.childCount():
            self._load_children(item)

    def _item_changed(self, _item, _column):
        if not self._building:
            self._schedule()

    def _filter(self, text):
        words = text.lower().split()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            hay = (it.text(0) + " " + it.data(0, ROLE)[1]).lower()
            show = all(w in hay for w in words)
            if not show and words and it.childCount():
                for j in range(it.childCount()):
                    c = it.child(j)
                    if all(w in c.text(0).lower() for w in words) or any(all(w in c.child(k).text(0).lower() for w in words) for k in range(c.childCount())):
                        show = True
                        break
            it.setHidden(not show)

    def selection(self):
        """{unit key: {"all": bool, "adm1": set, "adm2": set}} of what is ticked."""
        out = {}
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            st = it.checkState(0)
            if st == UNCHECKED:
                continue
            key = it.data(0, ROLE)[1]
            if st == CHECKED or not it.childCount():
                out[key] = {"all": True, "adm1": set(), "adm2": set()}
                continue
            sel = {"all": False, "adm1": set(), "adm2": set()}
            for j in range(it.childCount()):
                c = it.child(j)
                cs = c.checkState(0)
                if cs == CHECKED:
                    sel["adm1"].add(c.data(0, ROLE)[1])
                elif cs == PARTIAL:
                    for k in range(c.childCount()):
                        if c.child(k).checkState(0) == CHECKED:
                            sel["adm2"].add(c.child(k).data(0, ROLE)[1])
            if sel["adm1"] or sel["adm2"]:
                out[key] = sel
        return out

    def clear_selection(self):
        self._building = True
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, UNCHECKED)
        self._building = False
        self._schedule()

    def select(self, key, adm1=None, adm2=None, toggle=False):
        """Ticks (or, with toggle, flips) a country, or one division of it. Returns the new state, None if not found."""
        top = self._top(key)
        if top is None:
            return None
        if not self.rad_select.isChecked():
            self.rad_select.setChecked(True)
        target = top
        if adm1:
            self._load_children(top)
            target = None
            for j in range(top.childCount()):
                c = top.child(j)
                if c.data(0, ROLE)[1] == adm1:
                    target = c
                    if adm2 and c.childCount():
                        target = None
                        for k in range(c.childCount()):
                            if c.child(k).data(0, ROLE)[1] == adm2:
                                target = c.child(k)
                                break
                    break
            if target is None:
                return None
            top.setExpanded(True)
            if target.parent() is not top:
                target.parent().setExpanded(True)
        new = UNCHECKED if (toggle and target.checkState(0) == CHECKED) else CHECKED
        target.setCheckState(0, new)
        top.setHidden(False)
        self.tree.scrollToItem(target)
        self.tree.setCurrentItem(target)
        return new == CHECKED

    # ---- the plan and what it costs --------------------------------------------------------------------------------
    def plan(self):
        if self.repo is None:
            return []
        sel = None if self.coverage() == "world" else self.selection()
        if sel is not None and not sel:
            return []
        return self.repo.plan(self.dataset_id(), self.level_id(), self.geometries(), self.format_id(), sel)

    def _schedule(self, *_):
        if not self._building:
            self._timer.start()

    def _update_estimate(self):
        if self.repo is None or self.fetcher is None:
            return
        sel = self.selection() if self.coverage() == "places" else None
        if sel is not None:
            whole = sum(1 for s in sel.values() if s["all"])
            n_parts = sum(len(s["adm1"]) + len(s["adm2"]) for s in sel.values() if not s["all"])
            partial = len(sel) - whole
            parts = []
            if whole:
                parts.append("{} {}".format(whole, "country" if whole == 1 else "countries"))
            if n_parts:
                parts.append("{} division{} of {} {}".format(n_parts, "" if n_parts == 1 else "s", partial,
                                                              "country" if partial == 1 else "countries"))
            self.lbl_selected.setText(", ".join(parts) if parts else "Nothing chosen")
        try:
            items = self.plan()
        except RepositoryError as e:
            self.lbl_estimate.setText(str(e))
            return
        if not items:
            self.lbl_estimate.setText("Choose countries in the list or on the map." if self.coverage() == "places" else "")
            return
        todo, size, here = self.fetcher.estimate([i.asset for i in items])
        n = len({(i.asset.tree, i.asset.rel) for i in items})
        if not self.fetcher.through_cache:
            text = "{} file{}, read from the repository folder.".format(n, "" if n == 1 else "s")
        elif todo == 0:
            text = "{} file{}, already on this computer.".format(n, "" if n == 1 else "s")
        else:
            text = "{} file{} to {} ({}){}.".format(todo, "" if todo == 1 else "s", "download" if self.fetcher.remote else "copy from the repository folder",
                                                     human_size(size) if size is not None else "size unknown",
                                                     ", {} already here".format(here) if here else "")
        if any(i.subset for i in items):
            text += " Divisions are read from their country file with a filter."
        self.lbl_estimate.setText(text)

    # ---- running ---------------------------------------------------------------------------------------------------
    def _status(self, text, level=INFO):
        self.lbl_status.setText(text)
        if level in (WARNING, CRITICAL):
            log(text, level)

    def _set_busy(self, busy):
        self.bar.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        for w in (self.btn_save, self.cmb_dataset, self.cmb_level, self.cmb_format):
            w.setEnabled(not busy and self.repo is not None)
        self.cmb_geometry.setEnabled(not busy and self.repo is not None and not self._fixed())
        self.btn_add.setEnabled(not busy and self.repo is not None and self.format_id() in LOADABLE)

    def _confirm(self, text):
        return QMessageBox.question(self, "Terrae", text) == QMessageBox.StandardButton.Yes

    def _run(self, assets, callback, what):
        """Fetches `assets`, then calls callback({asset: path}). One job at a time."""
        if self.fetcher is None:
            return False
        if self._job is not None or self.fetcher.busy:
            self._status("Wait for the running download to finish, or cancel it.", WARNING)
            return False
        todo, size, _here = self.fetcher.estimate(assets)
        if size is not None and size > LARGE and not self.confirm("This will download {} in {} file{}. Continue?"
                                                                    .format(human_size(size), todo, "" if todo == 1 else "s")):
            return False
        self._job = (callback, what)
        self.bar.setRange(0, 0)
        self._set_busy(True)
        self._status(what)
        self.fetcher.fetch(assets)
        return True

    def cancel(self):
        if self.fetcher is not None and self.fetcher.busy:
            self.fetcher.cancel()

    def _on_progress(self, text, done, total):
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * min(done / total, 1.0)))
        else:
            self.bar.setRange(0, 0)
        self.lbl_status.setText(text)

    def _end_job(self):
        job, self._job = self._job, None
        self._set_busy(False)
        self._schedule()
        return job

    def _on_finished(self, paths):
        job = self._end_job()
        if job is None:
            return
        try:
            job[0](paths)
        except RepositoryError as e:
            self._problem(str(e))
        except Exception as e:      # a bug must not leave the panel locked, nor go unseen
            log(traceback.format_exc(), CRITICAL)
            self._problem("Unexpected error: {} (details in the log panel, tab Terrae)".format(e))

    def _on_failed(self, message):
        self._end_job()
        self._problem(message)
        if self.btn_pick.isChecked() and not self.picker.active:
            self.btn_pick.setChecked(False)

    def _on_canceled(self):
        self._end_job()
        self._status("Cancelled. Nothing was added.")
        if self.btn_pick.isChecked() and not self.picker.active:
            self.btn_pick.setChecked(False)

    def _problem(self, message):
        self._status(message, WARNING)
        self.iface.messageBar().pushMessage("Terrae", message, WARNING, 12)

    # ---- the two actions -------------------------------------------------------------------------------------------
    def add_to_map(self, items=None):
        try:
            items = items if items else self.plan()
        except RepositoryError as e:
            self._problem(str(e))
            return False
        if not items:
            self._status("Nothing to add: choose countries in the list or on the map, or take the whole world.", WARNING)
            return False

        def done(paths):
            layers, problems = [], []
            for item in items:
                try:
                    layers.append((item, make_layer(paths[item.asset], item, self.repo, self.chk_style.isChecked())))
                except RepositoryError as e:
                    problems.append(str(e))
            was_empty = not QgsProject.instance().mapLayers()
            added = add_layers(layers)
            if added and (was_empty or items[0].asset.key is not None):
                self._zoom(added)
            if problems:
                self._problem(" ".join(problems))
            if added:
                text = "Added {} layer{} \u00b7 Terrae {} \u00b7 {}".format(len(added), "" if len(added) == 1 else "s", self.repo.version,
                                                                             self.repo.manifest.get("attribution", ""))
                self._status(text)
                self.iface.messageBar().pushMessage("Terrae", text, SUCCESS, 6)

        return self._run([i.asset for i in items], done, "Getting {} file{}\u2026".format(len(items), "" if len(items) == 1 else "s"))

    def save_to_folder(self, folder=None):
        try:
            items = self.plan()
        except RepositoryError as e:
            self._problem(str(e))
            return False
        if not items:
            self._status("Nothing to save: choose countries in the list or on the map, or take the whole world.", WARNING)
            return False
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Save the files in", self.app.setting("last_folder", os.path.expanduser("~")))
        if not folder:
            return False
        self.app.set_setting("last_folder", folder)

        def done(paths):
            self._status("Copying\u2026")
            out = copy_to(paths, folder, self.repo, QCoreApplication.processEvents)
            text = "Saved {} file{} in {}".format(len(out), "" if len(out) == 1 else "s", folder)
            self._status(text)
            self.iface.messageBar().pushMessage("Terrae", text, SUCCESS, 10)

        return self._run([i.asset for i in items], done, "Getting {} file{}\u2026".format(len(items), "" if len(items) == 1 else "s"))

    def quick_add(self, dataset_id, level, geometries, fmt="gpkg"):
        """Adds a whole-world layer without touching what the panel shows (the toolbar menu)."""
        if self.repo is None:
            self._problem("No repository: set the data source first.")
            return False
        try:
            return self.add_to_map(self.repo.plan(dataset_id, level, geometries, fmt, None))
        except RepositoryError as e:
            self._problem(str(e))
            return False

    def run_preset(self, preset):
        if self.repo is None:
            self._problem("No repository: set the data source first.")
            return False
        view = PRESET_VIEWS.get(preset.get("id"))
        if view and view[1] in [lv.id for lv in self.repo.levels]:      # the preset is one of the plugin's views, with its own symbology
            return self.quick_add(view[0], view[1], ["polygons", "lines"])
        items = []
        try:
            for part in preset["load"]:
                items += self.repo.plan(part["dataset"], part["level"], [part["geometry"]], part.get("format", "gpkg"), None)
        except (RepositoryError, KeyError) as e:
            self._problem(str(e))
            return False
        return self.add_to_map(items)

    def _zoom(self, layers):
        canvas = self.iface.mapCanvas()
        dest = canvas.mapSettings().destinationCrs()
        box = QgsRectangle()
        box.setNull() if hasattr(box, "setNull") else box.setMinimal()
        for layer in layers:
            ext = layer.extent()
            if ext.isEmpty():
                continue
            if dest.isValid() and layer.crs() != dest:
                try:
                    ext = QgsCoordinateTransform(layer.crs(), dest, QgsProject.instance()).transformBoundingBox(ext)
                except Exception as e:     # a layer whose extent cannot be transformed is left out of the zoom
                    log("Zoom to the added layers: {} left out ({})".format(layer.name(), e), WARNING)
                    continue
            box.combineExtentWith(ext)
        if not box.isNull() and not box.isEmpty():
            box.scale(1.08)
            canvas.setExtent(box)
            canvas.refresh()

    # ---- picking on the map ----------------------------------------------------------------------------------------
    def _pick_toggled(self, on):
        if not on:
            if self.picker.active:
                self.picker.stop(True)
            else:
                self.picker.remove_layers()
            return
        if self.repo is None:
            self.btn_pick.setChecked(False)
            return
        if not self.rad_select.isChecked():
            self.rad_select.setChecked(True)
        depth = max(self.cmb_pick.currentIndex(), 0)
        try:
            self.picker.repo = self.repo
            asset = self.picker.index_asset()
        except RepositoryError as e:
            self._problem(str(e))
            self.btn_pick.setChecked(False)
            return

        def done(paths):
            self.picker.start(self.repo, depth, paths[asset])
            self._status("Click a country on the map to choose it. Click it again to remove it.")

        if not self._run([asset], done, "Getting the selection map\u2026"):
            self.btn_pick.setChecked(False)

    def _pick_stopped(self):
        if self.btn_pick.isChecked():
            self.btn_pick.blockSignals(True)
            self.btn_pick.setChecked(False)
            self.btn_pick.blockSignals(False)

    def _open_divisions(self, key):
        depth = self.picker.depth
        try:
            asset = self.picker.division_asset(key, depth)
        except RepositoryError as e:
            self._problem(str(e))
            return
        self._run([asset], lambda paths: self.picker.show_divisions(key, depth, paths[asset]),
                  "Getting the divisions of {}\u2026".format(self.repo.entity("adm0", key)["name"]))

    def _picked(self, key, adm1, adm2):
        if self.repo.dataset(self.dataset_id())["entities"] != "units":
            self._status("Picking on the map chooses countries; this layer is chosen in the list.", WARNING)
            return
        state = self.select(key, adm1 or None, adm2 or None, toggle=True)
        if state is None:
            self._status("This place is not in the list of the layer.", WARNING)
            return
        name = self.tree.currentItem().text(0) if self.tree.currentItem() else key
        self._status("{} {}.".format(name, "chosen" if state else "removed"))

    def closeEvent(self, event):
        self.picker.stop(True)
        if self.btn_pick.isChecked():
            self.btn_pick.setChecked(False)
        super().closeEvent(event)
