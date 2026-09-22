# Map sources and licenses

FotoArchive displays OpenStreetMap-based vector tiles using MapLibre GL JS.
All renderer scripts ship with the application. The following public map data
is downloaded into the user's persistent cache, not committed to this repo:

- Protomaps daily basemap builds: https://docs.protomaps.com/basemaps/downloads
- Build metadata: https://build-metadata.protomaps.dev/builds.json
- Fonts and sprites: https://github.com/protomaps/basemaps-assets

The application uses bounded HTTP range requests to a pinned PMTiles build.
It downloads world zoom levels 0–4 automatically and detail levels through 15
only as viewed. Existing tile, font and sprite cache entries are reused without
revalidation or expiry. This is a partial local copy of map data, not a promise
that every new location is available offline.

Bundled software/assets:

| Component | Version | License file |
| --- | --- | --- |
| MapLibre GL JS | 5.24.0 | [BSD 3-Clause](map/maplibre-LICENSE.txt) |
| @protomaps/basemaps | 5.7.2 | [Code and design licenses](map/basemaps-LICENSE.md) |
| Protomaps basemap data | schema v4, pinned at first use | [Data licenses](map/basemaps-LICENSE_DATA.md) |
| Noto Sans glyphs | Regular, Medium, Italic | [SIL Open Font License](map/NotoSans-OFL.txt) |

Map data contains OpenStreetMap (ODbL), Natural Earth (public domain), and
ESA WorldCover landcover. Icons derive from Mapzen (MIT). See the copied
upstream data license for complete attribution. The on-map attribution names
OpenStreetMap contributors, Protomaps and ESA WorldCover.

No map keys, accounts or photo uploads are required. Cached map resources are
served to the embedded renderer only by a token-scoped loopback HTTP endpoint.
The renderer cannot load external URLs; the application fetches public map
resources using a fixed set of permitted resource paths.
