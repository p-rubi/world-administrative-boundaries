# -*- coding: utf-8 -*-
"""Choosing countries and divisions by clicking on the map.

The plugin has no map of its own: it lays an index of the countries (the lightest level, a few megabytes, fetched
once) on QGIS's canvas and listens to clicks. A click on a country selects it; when the dataset has divisions, the
first click on a country lays its divisions over it and the next clicks select those."""
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsFillSymbol, QgsGeometry,
                       QgsPointXY, QgsProject, QgsRectangle, QgsSingleSymbolRenderer, QgsVectorLayer)
from qgis.gui import QgsMapToolEmitPoint

from .loader import layer_source
from .net import log
from .repository import RepositoryError

INDEX_GROUP = "Terrae \u00b7 selection map"
WGS84 = "EPSG:4326"


class MapPicker(QObject):
    """picked(unit key, adm1 id or '', adm2 id or '') · opened(unit key) · message(text) · stopped()"""
    picked = pyqtSignal(str, str, str)
    opened = pyqtSignal(str)
    message = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.repo = None
        self.depth = 0
        self.index_layer = None        # the countries
        self.division_layers = {}      # unit key -> the layer of its divisions at the picking depth
        self.active = False
        self._previous_tool = None
        self.tool = QgsMapToolEmitPoint(self.canvas)
        self.tool.canvasClicked.connect(self._clicked)
        self.tool.deactivated.connect(self._tool_deactivated)

    # ---- layers of the selection map -------------------------------------------------------------------------------
    def _group(self, create=True):
        root = QgsProject.instance().layerTreeRoot()
        for child in root.children():
            if child.name() == INDEX_GROUP:
                return child
        return root.insertGroup(0, INDEX_GROUP) if create else None

    def _overlay(self, path, asset, title, fill, line, width):
        layer = QgsVectorLayer(layer_source(path, asset), title, "ogr")
        if not layer.isValid():
            raise RepositoryError("QGIS cannot read {}.".format(path))
        symbol = QgsFillSymbol.createSimple({"color": fill, "outline_color": line, "outline_width": str(width)})
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setCustomProperty("terrae/index", True)
        QgsProject.instance().addMapLayer(layer, False)
        self._group().insertLayer(0, layer)
        return layer

    def index_asset(self):
        """The file the countries are read from: the reference units at the lightest level."""
        return self.repo.asset("adm0", self.repo.levels[0].id, "polygons")

    def division_asset(self, key, depth):
        return self.repo.asset("adm1" if depth == 1 else "adm2", self.repo.levels[0].id, "polygons", "gpkg", key)

    def start(self, repo, depth, index_path):
        """Lays the countries on the canvas and takes the clicks."""
        self.repo = repo
        self.depth = depth
        if self.index_layer is None or not self._alive(self.index_layer):
            self.index_layer = self._overlay(index_path, self.index_asset(), "Countries (click to select)", "64,120,160,40", "#40647a", 0.2)
            if self.canvas.layerCount() <= 1:
                self.canvas.setExtent(self._to_canvas(self.index_layer.extent()))
        self._previous_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self.tool)
        self.active = True
        self.canvas.refresh()

    def set_depth(self, depth):
        self.depth = depth

    def stop(self, remove_layers=True):
        was = self.active
        self.active = False
        if self.canvas.mapTool() is self.tool:
            if self._previous_tool is not None and self._previous_tool is not self.tool:
                self.canvas.setMapTool(self._previous_tool)
            else:
                self.canvas.unsetMapTool(self.tool)
        if remove_layers:
            self.remove_layers()
        if was:
            self.stopped.emit()

    def remove_layers(self):
        group = self._group(False)
        if group is not None:
            ids = [n.layerId() for n in group.findLayers()]
            QgsProject.instance().removeMapLayers(ids)
            QgsProject.instance().layerTreeRoot().removeChildNode(group)
        self.index_layer = None
        self.division_layers = {}
        self.canvas.refresh()

    def _tool_deactivated(self):
        if self.active:                 # the user took another tool: picking is over, the layers stay until closed
            self.active = False
            self.stopped.emit()

    @staticmethod
    def _alive(layer):
        try:
            return layer is not None and layer.isValid() and QgsProject.instance().mapLayer(layer.id()) is not None
        except RuntimeError:
            return False

    # ---- clicks ----------------------------------------------------------------------------------------------------
    def _to_wgs(self, point):
        crs = self.canvas.mapSettings().destinationCrs()
        if crs.authid() == WGS84 or not crs.isValid():
            return QgsPointXY(point)
        return QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(WGS84), QgsProject.instance()).transform(point)

    def _to_canvas(self, rect):
        crs = self.canvas.mapSettings().destinationCrs()
        if crs.authid() == WGS84 or not crs.isValid():
            return rect
        return QgsCoordinateTransform(QgsCoordinateReferenceSystem(WGS84), crs, QgsProject.instance()).transformBoundingBox(rect)

    @staticmethod
    def feature_at(layer, point):
        """The feature of `layer` (WGS 84) that holds `point`, or None."""
        geom = QgsGeometry.fromPointXY(point)
        rect = QgsRectangle(point.x() - 1e-9, point.y() - 1e-9, point.x() + 1e-9, point.y() + 1e-9)
        for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
            if f.geometry().contains(geom) or f.geometry().intersects(geom):
                return f
        return None

    def _clicked(self, point, _button):
        if not self._alive(self.index_layer):
            self.message.emit("The selection map was closed. Start picking again.")
            self.stop(False)
            return
        try:
            wgs = self._to_wgs(point)
        except Exception:      # a click outside the projection's domain
            return
        unit = self.feature_at(self.index_layer, wgs)
        if unit is None:
            self.message.emit("No country here (sea, or outside the map).")
            return
        key = unit["unit_key"]
        entity = self.repo.entity("adm0", key)
        depth = self.depth if (entity.get("adm1_divisions", 0) > 0 or (self.depth == 2 and entity.get("adm2_divisions", 0) > 0)) else 0
        if depth == 0:
            self._flash(unit.geometry())
            self.picked.emit(key, "", "")
            return
        layer = self.division_layers.get((key, depth))
        if layer is None or not self._alive(layer):
            self.opened.emit(key)       # the panel fetches the file and calls show_divisions()
            return
        f = self.feature_at(layer, wgs)
        if f is None:
            self.message.emit("No division here.")
            return
        self._flash(f.geometry())
        if depth == 1:
            self.picked.emit(key, f["adm1_id"], "")
        else:
            self.picked.emit(key, f["adm1_id"], f["adm2_id"])

    def show_divisions(self, key, depth, path):
        """Lays the divisions of one country over it, so that the next clicks choose among them."""
        name = self.repo.entity("adm0", key)["name"]
        title = "{}: {} (click to select)".format(name, "first-level divisions" if depth == 1 else "second-level divisions")
        layer = self._overlay(path, self.division_asset(key, depth), title, "107,91,149,45", "#6b5b95", 0.2)
        self.division_layers[(key, depth)] = layer
        self.canvas.setExtent(self._to_canvas(layer.extent()))
        self.canvas.refresh()
        self.message.emit("{}: click a division to select it.".format(name))

    def _flash(self, geometry):
        try:
            self.canvas.flashGeometries([geometry], QgsCoordinateReferenceSystem(WGS84),
                                        QColor(255, 180, 0, 60), QColor(255, 180, 0, 200), 2, 250)
        except Exception as e:        # the flash only confirms a click: without it the selection still works
            log("Could not highlight the picked feature: {}".format(e))
