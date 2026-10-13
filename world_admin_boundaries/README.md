# World Administrative Boundaries

Adds the administrative boundaries of the world to your project: countries, states and provinces, districts and
counties, as polygons or lines. The data downloads by itself from the online repository; nothing needs setting up.

## Quick start

1. Click the toolbar button to open the panel.
2. Choose a **dataset**, a **level of detail** and a **geometry**.
3. Keep **Whole world**, or switch to **Chosen countries** and tick what you want.
4. Press **Add to map**.

For a ready base map, open the arrow beside the toolbar button and pick **World countries**.

## Choices

- **Dataset:** Level 0, countries and territories (255); Level 1, states, provinces and regions; Level 2, districts,
  counties and municipalities. Level 0 also comes in two other views: *de facto* (as administered) and
  *disputed areas*.
- **Level of detail:** Low (about 1,500 m, for world maps), Medium (150 m), High (15 m) or Native (the geometry of
  OpenStreetMap). Use Medium or finer for Level 2.
- **Geometry:** polygons, lines, or both. Lines are classified: border, internal boundary, coastline.

The three levels fit one another exactly, at every level of detail.

## Choosing places

- **In the list:** tick countries; type in the search box to find one.
- **Divisions:** with Level 1 or Level 2, open a country with the arrow beside it and tick single divisions.
- **On the map:** press **Pick on map** and click. With Level 1 or 2, the first click on a country shows its
  divisions and the next clicks choose among them. Press the button again to finish.

## Getting the data

- **Add to map** downloads what is missing and adds the layers, lines above polygons.
- **Save to folder…** saves the files instead, as GeoPackage.
- Only what you ask for is downloaded: one small file per country. A single division is read from the file of its
  country.
- Downloads are checked and kept, so they are not fetched twice and work offline afterwards.

## Settings

**Settings…** (in the panel and in the menu) is only needed to use a local copy of the data, choose an earlier
release, or move or empty the folder where downloads are kept.

## Good to know

- Several countries come as one layer each. To merge them: *Vector ▸ Data Management Tools ▸ Merge Vector Layers*.
- The search box finds countries, and the divisions of countries already opened in the list.
- An interrupted download starts again from the beginning.

## Data and licence

The data is [Terrae](https://github.com/p-rubi/terrae), an open dataset built from OpenStreetMap by the author of
this plugin. Open Database License 1.0: credit **© OpenStreetMap contributors; Terrae**. Boundaries and names shown
do not imply endorsement or acceptance.

Questions and bugs: <https://github.com/p-rubi/world-administrative-boundaries/issues>