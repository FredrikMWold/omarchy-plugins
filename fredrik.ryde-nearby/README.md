# Ryde nearby

An Omarchy bar widget that opens a native QML panel of available Ryde scooters
near the device's current location.

The install roots are `geoclue`, `libnotify`, `python-gobject`, and
`qt6-location`. Python and Qt Positioning are pulled transitively. The plugin
checks functional requirements before loading the map and presents an
Omarchy-native setup view when anything is missing. Its setup button opens a
visible Omarchy terminal, uses `omarchy pkg add`, and installs a UID-scoped
GeoClue authorization file. `libnotify` is required by GeoClue's authorization
agent, which keeps accurate Wi-Fi positioning reliable across shell restarts.

Click the scooter icon in the bar to open the panel. The map uses
GeoClue with BeaconDB Wi-Fi positioning, CARTO tiles using OpenStreetMap data,
and
Ryde's public GBFS feed through Entur.

GeoClue sends nearby Wi-Fi access-point information to BeaconDB to determine
the position. Coordinates are then used locally to select the closest Ryde
service area and calculate distances; they are not sent to Entur. If GeoClue
cannot return a result within one kilometre of accuracy, the panel falls back
to Stavanger city center.

Qt Location and Ryde's GBFS feed do not provide elevation. The plugin therefore
caches a broad one-degree SRTM1 terrain tile from the public AWS Terrain Tiles
dataset and performs elevation sampling locally. It never sends the precise
user or scooter coordinates to an elevation API.

The map displays nearby available scooters within 1.5 kilometres.
Ryde/Entur data is fetched with the required `ET-Client-Name` header.

Data is prefetched every minute and cached under
`~/.cache/fredrik.ryde-nearby/data.json`, so the panel opens immediately. The
bar uses a centered scooter icon with a proximity dot: green within 50 m,
yellow within 100 m, orange within 250 m, and red beyond 250 m or when no
scooter is available. A hollow dot means the visible data is cached or the
latest refresh failed.

When local terrain confidence is sufficient, the dot applies an uphill-only
effort penalty based on Naismith's rule: one metre of confident ascent counts
as roughly 8.33 metres of flat distance after a 5 m terrain allowance. This
can promote a proximity band, such as orange to red, but downhill never makes
a scooter appear closer. The adjustment affects only the indicator color and
is not presented as route distance or route grade.

Panel surfaces, markers, text, and light/dark map tiles follow the active
Omarchy theme. Geofence polygons are not shown because the feed's zone rules
must be interpreted before a boundary can be labelled accurately.

Map interaction is based on Qt Location's `MapView`. Mouse dragging pans
directly without release acceleration, and mouse-wheel zoom handles Wayland's
mouse/touchpad wheel-device ambiguity. Pinch rotation and tilt are disabled,
and the geographic center is locked for the complete pinch gesture so
releasing it cannot move the map.

Individual scooter coordinates are cached without clustering. The QML panel
clusters them by screen distance at the current zoom level, expands clusters
as the map zooms in, and applies a capped, gentle repulsion to overlapping
individual markers from zoom level 18. Clicking a cluster zooms into it. The
nearest and selected scooters receive additional visual emphasis.

Ryde publishes `max_range_meters: 56300` for its standing scooter type, so the
panel treats 0 km as 0% and 56.3 km as 100%. The displayed percentage is the
remaining range as a proportion of that published maximum; it is not a direct
battery state-of-charge field. It is shown only in selected-scooter details,
while the feed's remaining range is the primary value.

Keyboard controls: arrows or H/J/K/L pan, `+` and `-` zoom, `C` recentres,
`R` refreshes, `N`/`P` move through scooters, and `X` clears a selection.
Cached refresh failures and saved-location fallback remain visible without
hiding otherwise usable map data.
