# -*- coding: utf-8 -*-
"""From a file on disk to a layer in the project: the table, the filter, a plain style, the credit."""
import os

from qgis.core import (QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsLineSymbol, QgsProject, QgsProperty, QgsRendererCategory,
                       QgsRuleBasedRenderer, QgsSingleSymbolRenderer, QgsVectorLayer)

from .compat import STROKE_WIDTH
from .repository import RepositoryError

GROUP_PROPERTY = "terrae/group"

# line kinds: colour, width in mm, label; and the kinds each dataset's lines have, in the order of the legend
LINE_STYLES = {
    "coastline": ("#4a7c9b", 0.26, "Coastline"),
    "land_boundary": ("#555555", 0.5, "Country boundary"),
    "adm0_boundary": ("#555555", 0.5, "Country boundary"),
    "adm1_boundary": ("#6b5b95", 0.36, "First-level boundary"),
    "adm2_boundary": ("#9a9a9a", 0.18, "Second-level boundary"),
    "reference_boundary": ("#b5542d", 0.4, "Area edge on a country boundary"),
    "area_boundary": ("#b5542d", 0.4, "Area edge inside a country"),
}
LINE_KINDS = {
    "adm0": ["land_boundary", "coastline"],
    "adm0_de_facto": ["land_boundary", "coastline"],
    "adm0_disputes": ["reference_boundary", "area_boundary", "coastline"],
    "adm1": ["adm0_boundary", "adm1_boundary", "coastline"],
    "adm2": ["adm0_boundary", "adm1_boundary", "adm2_boundary", "coastline"],
}
FILLS = {  # dataset: fill, outline, outline width
    "adm0": ("#e9e4d8", "#6f6a5e", 0.26),
    "adm0_de_facto": ("#e4e9d8", "#5e6f62", 0.26),
    "adm0_disputes": ("#f0c9b8", "#b5542d", 0.3),
    "adm1": ("#e6e2ee", "#6b5b95", 0.2),
    "adm2": ("#eeeeee", "#8a8a8a", 0.12),
}


# The palette of the maps in the Terrae repository, for the layers made for cartography (the views).
PALETTE = {"land": "#f4efe4", "adm0": "#2f2c28", "adm1": "#7b5ea7", "adm2": "#b9a98c", "coast": "#4f89a6"}
# "All levels", from the lowest level to the highest, which is the order they are drawn in: kind, label, colour, width in mm
# at 1:250,000 or closer, width at the far scale, the far scale, and the scale beyond which the level is not drawn (0 = always).
# The widths fall smoothly with the scale and the finer levels disappear when zoomed out, so that the map never clogs.
ALL_LEVELS = [
    ("adm2_boundary", "Second-level boundary", PALETTE["adm2"], 0.26, 0.10, 6000000, 7000000),
    ("coastline", "Coastline", PALETTE["coast"], 0.40, 0.15, 100000000, 0),
    ("adm1_boundary", "First-level boundary", PALETTE["adm1"], 0.50, 0.18, 50000000, 50000000),
    ("adm0_boundary", "Country boundary", PALETTE["adm0"], 0.80, 0.30, 100000000, 0),
]
NEAR_SCALE = 250000


def layer_source(path, asset):
    """What QGIS opens: a GeoPackage is addressed by file and table; the other formats hold one layer."""
    if asset.format == "gpkg":
        return "{}|layername={}".format(path, asset.table)
    return path


def _line(colour, width, width_expression=None):
    symbol = QgsLineSymbol.createSimple({"line_color": colour, "line_width": str(width), "line_width_unit": "MM",
                                         "capstyle": "round", "joinstyle": "round"})
    if width_expression:
        symbol.symbolLayer(0).setDataDefinedProperty(STROKE_WIDTH, QgsProperty.fromExpression(width_expression))
    return symbol


def _style_all_levels(layer, asset):
    """One layer of lines, no fill: the levels in their own colours, thinner and fewer as the scale gets smaller."""
    root = QgsRuleBasedRenderer.Rule(None)
    for kind, label, colour, near, far, far_scale, hidden_beyond in ALL_LEVELS:
        expression = "scale_linear(@map_scale, {}, {}, {}, {})".format(NEAR_SCALE, far_scale, near, far)
        rule = QgsRuleBasedRenderer.Rule(_line(colour, near, expression), 0, hidden_beyond, '"line_kind" = \'{}\''.format(kind), label)
        root.appendChild(rule)
    renderer = QgsRuleBasedRenderer(root)
    renderer.setUsingSymbolLevels(True)          # the rules are drawn in the order above, whatever the order of the features
    layer.setRenderer(renderer)


def _style_coastline(layer, asset):
    layer.setRenderer(QgsSingleSymbolRenderer(_line(PALETTE["coast"], 0.2)))


def _style_cartographic(layer, asset):
    if asset.geometry == "polygons":
        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": PALETTE["land"], "outline_style": "no"})))
        return
    categories = [QgsRendererCategory("land_boundary", _line(PALETTE["adm0"], 0.35), "Country boundary"),
                  QgsRendererCategory("coastline", _line(PALETTE["coast"], 0.2), "Coastline")]
    layer.setRenderer(QgsCategorizedSymbolRenderer("line_kind", categories))


VIEW_STYLES = {"all_levels": _style_all_levels, "coastline": _style_coastline, "cartographic": _style_cartographic}


def style(layer, asset):
    if asset.dataset in VIEW_STYLES:
        VIEW_STYLES[asset.dataset](layer, asset)
        return
    if asset.geometry == "polygons":
        fill, line, width = FILLS.get(asset.dataset, FILLS["adm0"])
        symbol = QgsFillSymbol.createSimple({"color": fill, "outline_color": line, "outline_width": str(width),
                                             "outline_width_unit": "MM", "joinstyle": "round"})
        symbol.setOpacity(0.85)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))      # (a KML arrives with a renderer of its own kind)
        return
    if layer.fields().indexOf("line_kind") < 0:
        return
    categories = []
    for kind in LINE_KINDS.get(asset.dataset, list(LINE_STYLES)):
        colour, width, label = LINE_STYLES[kind]
        props = {"line_color": colour, "line_width": str(width), "line_width_unit": "MM", "capstyle": "round", "joinstyle": "round"}
        if kind == "area_boundary":
            props["line_style"] = "dash"
        categories.append(QgsRendererCategory(kind, QgsLineSymbol.createSimple(props), label))
    layer.setRenderer(QgsCategorizedSymbolRenderer("line_kind", categories))


def make_layer(path, item, repo, symbology=True):
    """The layer of one plan item, checked: a layer that cannot be read, or that the filter leaves empty, is an error.
    With symbology False the layer keeps the default look QGIS gives a new layer."""
    asset = item.asset
    if not os.path.isfile(path):
        raise RepositoryError("{} is not on this computer.".format(path))
    layer = QgsVectorLayer(layer_source(path, asset), item.title, "ogr")
    if not layer.isValid():
        raise RepositoryError("QGIS cannot read {} (table {}).".format(path, asset.table))
    if item.subset:
        if not layer.setSubsetString(item.subset):
            raise RepositoryError("The file {} does not accept the filter {}.".format(os.path.basename(path), item.subset))
        layer.updateExtents()
    if layer.featureCount() == 0:
        raise RepositoryError("{}: no feature for this selection.".format(item.title))
    if symbology:
        style(layer, asset)
    credit = repo.manifest.get("attribution", "")
    try:
        layer.serverProperties().setAttribution(credit)
    except AttributeError:
        layer.setAttribution(credit)
    for k, v in (("version", repo.version), ("dataset", asset.dataset), ("level", asset.level), ("geometry", asset.geometry),
                 ("key", asset.key or "world"), ("licence", repo.manifest.get("licence", ""))):
        layer.setCustomProperty("terrae/" + k, v)
    md = layer.metadata()
    md.setTitle(item.title)
    md.setAbstract("Terrae {} \u00b7 {} \u00b7 level of detail: {}. {}".format(repo.version, repo.dataset(asset.dataset)["title"], asset.level,
                                                                                 repo.dataset(asset.dataset).get("description", "")))
    md.setRights([credit])
    md.setLicenses([repo.manifest.get("licence", "")])
    layer.setMetadata(md)
    return layer


def group_for(name, project=None):
    """The layer-tree group of a release and level, made at the top the first time."""
    root = (project or QgsProject.instance()).layerTreeRoot()
    for child in root.children():
        if child.name() == name and child.customProperty(GROUP_PROPERTY):
            return child
    group = root.insertGroup(0, name)
    group.setCustomProperty(GROUP_PROPERTY, True)
    return group


def add_layers(layers_by_item, project=None):
    """Adds the layers under their groups: lines above polygons, so that boundaries are drawn over the shapes."""
    project = project or QgsProject.instance()
    added = []
    ordered = sorted(layers_by_item, key=lambda pair: (pair[0].group, 0 if pair[0].asset.geometry == "lines" else 1, pair[0].title))
    for item, layer in ordered:
        project.addMapLayer(layer, False)
        group = group_for(item.group, project)
        lines = sum(1 for n in group.children() if n.customProperty("terrae/geometry") == "lines")
        node = group.insertLayer(0 if item.asset.geometry == "lines" else lines, layer)
        node.setCustomProperty("terrae/geometry", item.asset.geometry)
        added.append(layer)
    return added
