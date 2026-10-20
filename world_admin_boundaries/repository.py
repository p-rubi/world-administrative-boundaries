# -*- coding: utf-8 -*-
"""Terrae data repository, as a client sees it.

Nothing in this module knows where a file is: it reads `repository.json`, then the manifest of a release, and builds
every path from the patterns the manifest gives. The repository can be a folder or the root URL of a bucket; reading
bytes is delegated to a *reader* (see `LocalReader` here and `QgisHttpReader` in `net.py`), so this module has no
dependency on QGIS and is tested without it.
"""
import json
import os
from collections import OrderedDict, namedtuple

SCHEMA = 1


class RepositoryError(Exception):
    """The repository cannot be read, or says something this client does not understand.

    `kind` says what a person can do about it: "network" (the service could not be reached), "server" (it answered with
    an error), "data" (what it holds cannot be understood by this version), "error" (anything else, such as a missing
    folder)."""

    def __init__(self, message="", kind="error"):
        super().__init__(message)
        self.kind = kind


def is_url(location):
    return str(location).lower().startswith(("http://", "https://"))


def normalise_location(location):
    """A folder path or a root URL, as typed by a person, in one form."""
    location = (location or "").strip().strip('"')
    if is_url(location):
        return location if location.endswith("/") else location + "/"
    if location.lower().startswith("file:///"):
        location = location[8:] if len(location) > 9 and location[9] == ":" else location[7:]
    return os.path.normpath(location) if location else ""


def join(location, rel):
    """The address of `rel` (always written with forward slashes) inside `location`."""
    if is_url(location):
        return location + rel.lstrip("/")
    return os.path.join(location, *rel.split("/"))


class LocalReader(object):
    """Reads the small files of a repository that is a folder."""

    def __init__(self, root):
        self.root = root

    def read(self, rel):
        path = join(self.root, rel)
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as e:
            raise RepositoryError("Cannot read {}: {}".format(path, e.strerror or e))


# Layers the plugin offers that are views over the files of a dataset of the repository: no new data, a different way of
# taking it. `base` is the dataset whose files are read; `fixed_geometries` the geometries the view is made of; `line_filter`
# keeps only some lines; `style` names the symbology the plugin gives it (loader.py).
VIEWS = OrderedDict([
    ("all_levels", {"title": "All levels", "base": "adm2", "fixed_geometries": ["lines"], "line_filter": "", "style": "all_levels",
                    "description": "Every administrative level in one layer of lines (country boundaries, first- and second-level boundaries "
                                   "and coastline), drawn without fill so that whatever lies beneath stays visible."}),
    ("coastline", {"title": "Coastline", "selector_title": "Coastline \u2014 the coast line only", "base": "adm0", "fixed_geometries": ["lines"],
                   "line_filter": '"line_kind" = \'coastline\'', "style": "coastline",
                   "description": "Only the coastline, as one layer of lines: the edge between land and sea of the reference partition, "
                                  "which fits the boundaries exactly."}),
    ("cartographic", {"title": "Cartographic base", "selector_title": "Cartographic base \u2014 land, country boundaries and coast",
                      "base": "adm0", "fixed_geometries": ["polygons", "lines"], "line_filter": "", "style": "cartographic",
                      "description": "A ready base map in two layers: the countries and territories as land (polygons), with the country "
                                     "boundaries and the coastline (lines) drawn over them."}),
])
# the selector: groups of layers, in order. A dataset the repository adds later that is not named here is listed last.
SELECTOR = [("Reference", ["adm0", "adm1", "adm2", "all_levels"]),
            ("Other level-0 views", ["adm0_de_facto", "adm0_disputes"]),
            ("Cartographic", ["coastline", "cartographic"])]

Level = namedtuple("Level", "id label tolerance_m scale open tree")
Asset = namedtuple("Asset", "dataset level geometry format key tree rel table bytes sha256")
# one layer to make: the file, the table in it, the filter, and what to call it
LoadItem = namedtuple("LoadItem", "asset title subset group")

LEVEL_NAMES = {"native": "Native", "high": "High", "medium": "Medium", "low": "Low"}
GEOMETRY_NAMES = {"polygons": "polygons", "lines": "lines"}
FORMAT_NAMES = OrderedDict([("gpkg", "GeoPackage"), ("geojson", "GeoJSON"), ("kml", "KML"), ("parquet", "GeoParquet")])


def sql_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def in_filter(fields, values):
    """("f1" IN (...) OR "f2" IN (...)) — the rows where any of `fields` is one of `values`."""
    if not fields or not values:
        return ""
    lst = ", ".join(sql_quote(v) for v in sorted(set(values)))
    return " OR ".join('"{}" IN ({})'.format(f, lst) for f in fields)


class Repository(object):
    """One release of the repository."""

    def __init__(self, location, reader, version=None):
        self.location = location
        self.reader = reader
        self.index = None          # repository.json
        self.manifest = None
        self.version = version
        self._checksums = {}       # tree -> {rel: sha256}
        self._hierarchy = {}       # unit key -> dict

    # ---- loading -------------------------------------------------------------------------------------------------
    def _json(self, rel):
        raw = self.reader.read(rel)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise RepositoryError("{} is not valid JSON ({})".format(rel, e), "data")

    def load(self):
        self.index = self._json("repository.json")
        releases = {r["version"]: r for r in self.index.get("releases", [])}
        if not releases:
            raise RepositoryError("The repository lists no release.", "data")
        version = self.version or self.index.get("latest") or sorted(releases)[-1]
        if version not in releases:
            raise RepositoryError("Release {} is not in the repository (it has: {}).".format(version, ", ".join(sorted(releases))))
        self.version = version
        manifest = self._json(releases[version]["manifest"])
        if manifest.get("schema") != SCHEMA:
            raise RepositoryError("The repository uses manifest schema {}; this plugin reads schema {}. Update the plugin."
                                  .format(manifest.get("schema"), SCHEMA), "data")
        self.manifest = manifest
        self._checksums.clear()
        self._hierarchy.clear()
        return self

    @property
    def releases(self):
        return sorted(r["version"] for r in (self.index or {}).get("releases", []))

    # ---- what there is -------------------------------------------------------------------------------------------
    @property
    def levels(self):
        """The levels, lightest first, as the manifest orders them."""
        out = []
        for lv in self.manifest["level_order"]:
            m = self.manifest["levels"][lv]
            out.append(Level(lv, m["label"], m["tolerance_m"], m["scale"], bool(m.get("open")), m.get("tree", "")))
        return out

    def level(self, level_id):
        for lv in self.levels:
            if lv.id == level_id:
                return lv
        raise RepositoryError("Unknown level: {}".format(level_id))

    @property
    def datasets(self):
        return self.manifest["datasets"]

    def dataset(self, dataset_id):
        """A dataset of the manifest, or one of the plugin's views, described the same way (a copy of its base dataset)."""
        for d in self.datasets:
            if d["id"] == dataset_id:
                return d
        view = VIEWS.get(dataset_id)
        if view is not None:
            for base in self.datasets:
                if base["id"] == view["base"]:
                    d = dict(base)
                    d.update(view)
                    d["id"] = dataset_id
                    d["view"] = True
                    return d
        raise RepositoryError("Unknown dataset: {}".format(dataset_id))

    def selector(self):
        """[(group title, [dataset, ...])]: what the layer selector lists, in order."""
        out, used = [], set()
        for title, ids in SELECTOR:
            found = []
            for i in ids:
                try:
                    found.append(self.dataset(i))
                except RepositoryError:
                    continue
                used.add(i)
            if found:
                out.append((title, found))
        rest = [d for d in self.datasets if d["id"] not in used]
        if rest:
            out.append(("Other", rest))
        return out

    @staticmethod
    def label(d):
        """How the selector and the menu name a dataset (a view may say more than the title its layers carry)."""
        return d.get("selector_title") or d["title"]

    def fixed_geometries(self, dataset_id):
        """The geometries a view is made of, or None when the user chooses them."""
        return self.dataset(dataset_id).get("fixed_geometries")

    @property
    def presets(self):
        return self.manifest.get("presets", [])

    def entities(self, dataset_id):
        return self.manifest["entities"][self.dataset(dataset_id)["entities"]]

    def entity(self, dataset_id, key):
        for e in self.entities(dataset_id):
            if e["key"] == key:
                return e
        raise RepositoryError("Unknown entity: {}".format(key))

    def depth(self, dataset_id):
        """How far the selection can go down: 0 countries, 1 first-level divisions, 2 second-level divisions."""
        d = self.dataset(dataset_id)
        return int(d.get("admin_level", 0)) if d["entities"] == "units" else 0

    def formats(self, dataset_id, level, coverage):
        d = self.dataset(dataset_id)
        return list(d["formats" if coverage == "world" else "place_formats"].get(level, []))

    def hierarchy(self, key):
        """The first- and second-level divisions of one unit (read once)."""
        if key not in self._hierarchy:
            self._hierarchy[key] = self._json(self.manifest["hierarchy"].format(key=key))
        return self._hierarchy[key]

    # ---- files ---------------------------------------------------------------------------------------------------
    def asset(self, dataset_id, level, geometry, fmt="gpkg", key=None):
        """The world file of a layer (key None) or the file of one entity."""
        d = self.dataset(dataset_id)
        if level not in d["trees"]:
            raise RepositoryError("{} is not available at level {}.".format(d["title"], level))
        tree = d["trees"][level]
        table = d["tables"][geometry]
        if key is None:
            info = d["world_files"][level][geometry].get(fmt)
            if info is None:
                raise RepositoryError("{} has no {} file of the whole world at level {}.".format(d["title"], fmt, level))
            rel = d["world"].format(level=level, geometry=geometry, format=fmt)
            return Asset(dataset_id, level, geometry, fmt, None, tree, rel, table, info.get("bytes"), info.get("sha256"))
        if fmt not in d["place_formats"].get(level, []):
            raise RepositoryError("{} has no {} file per entity at level {}.".format(d["title"], fmt, level))
        if key not in d["place_bytes"][level][geometry]:
            raise RepositoryError("{} has no file for {} at level {}.".format(d["title"], key, level))
        rel = d["place"].format(level=level, geometry=geometry, key=key, format=fmt)
        size = d["place_bytes"][level][geometry][key] if fmt == "gpkg" else None
        return Asset(dataset_id, level, geometry, fmt, key, tree, rel, table, size, None)

    def address(self, asset):
        return join(self.location, asset.tree + asset.rel)

    def checksum(self, asset):
        """The SHA-256 the repository states for a file, or None if it states none."""
        if asset.sha256:
            return asset.sha256
        tree = asset.tree
        if tree not in self._checksums:
            table = {}
            rel = self.manifest.get("checksums", {}).get(tree)
            if rel:
                for line in self.reader.read(rel).decode("utf-8").splitlines():
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        p = parts[1].strip().lstrip("*")
                        table[p[2:] if p.startswith("./") else p] = parts[0]
            self._checksums[tree] = table
        return self._checksums[tree].get(asset.rel)

    # ---- from a selection to the layers to make --------------------------------------------------------------------
    def subset(self, dataset_id, geometry, adm1_ids=(), adm2_ids=()):
        """The filter that keeps the chosen divisions of a country file ("" = the whole file)."""
        fields = self.dataset(dataset_id).get("filter_fields", {}).get(geometry, {})
        parts = [p for p in (in_filter(fields.get("adm1"), adm1_ids), in_filter(fields.get("adm2"), adm2_ids)) if p]
        return " OR ".join(parts)

    def _with_line_filter(self, d, geometry, subset):
        """The selection's filter, narrowed by what a view keeps of its lines."""
        extra = d.get("line_filter", "") if geometry == "lines" else ""
        if extra and subset:
            return "({}) AND ({})".format(subset, extra)
        return subset or extra

    def plan(self, dataset_id, level, geometries, fmt, selection=None):
        """The layers a request makes.

        selection None = the whole world. Otherwise a mapping {entity key: {"all": bool, "adm1": set, "adm2": set}}:
        the whole entity, or only some of its divisions (read from the entity's file with a filter)."""
        d = self.dataset(dataset_id)
        fixed = d.get("fixed_geometries")
        if fixed:
            geometries = [g for g in geometries if g in fixed] or list(fixed)
        lv_name = LEVEL_NAMES.get(level, level)
        group = "Terrae {} \u00b7 {}".format(self.version, d["title"].split(" \u2014 ")[0])
        short = d["title"].split(" \u2014 ")[-1]
        items = []
        if selection is None:
            for g in geometries:
                a = self.asset(dataset_id, level, g, fmt)
                items.append(LoadItem(a, "World \u2014 {} ({}, {})".format(short, lv_name.lower(), g), self._with_line_filter(d, g, ""), group))
            return items
        names = {e["key"]: e["name"] for e in self.entities(dataset_id)}
        for key in sorted(selection, key=lambda k: names.get(k, k)):
            sel = selection[key]
            adm1 = set() if sel.get("all") else set(sel.get("adm1", ()))
            adm2 = set() if sel.get("all") else set(sel.get("adm2", ()))
            if not sel.get("all") and not adm1 and not adm2:
                continue
            label = names.get(key, key)
            if adm1 or adm2:
                h = self.hierarchy(key)
                picked = [a["name"] for a in h["adm1"] if a["id"] in adm1]
                picked += [b["name"] for a in h["adm1"] for b in (a.get("adm2") or []) if b["id"] in adm2]
                label = "{}: {}".format(label, ", ".join(picked[:3]) + (" +{}".format(len(picked) - 3) if len(picked) > 3 else ""))
            for g in geometries:
                a = self.asset(dataset_id, level, g, fmt, key)
                items.append(LoadItem(a, "{} \u2014 {} ({}, {})".format(label, short, lv_name.lower(), g),
                                      self._with_line_filter(d, g, self.subset(dataset_id, g, adm1, adm2)), group))
        return items


def human_size(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("bytes", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return ("{:.0f} {}" if unit in ("bytes", "kB") else "{:.1f} {}").format(n, unit)
        n /= 1000.0
