import QtQuick
import QtLocation
import QtPositioning
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "fredrik.ryde-nearby"
  ipcTarget: "fredrik.ryde-nearby"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property string errorMessage: ""
  property bool loading: false
  property bool usingCachedData: false
  property bool refreshFailed: false
  property var mapData: null
  property bool mapInitialized: false
  property var clusteredMarkers: []
  property var selectedMarker: null
  property double nowSeconds: Date.now() / 1000
  property var themePalette: ({})

  ListModel {
    id: markerModel
    dynamicRoles: true
  }

  readonly property var barIdentity: hostWidget || root
  readonly property bool darkTheme:
    (Color.background.r * 0.2126
      + Color.background.g * 0.7152
      + Color.background.b * 0.0722) < 0.5
  readonly property bool fallbackLocation:
    mapData && mapData.location
      && (mapData.location.isFallback
        || mapData.location.source === "Saved location")
  readonly property bool showStatusStrip:
    mapData && (errorMessage !== "" || fallbackLocation)
  readonly property color secondaryText: Color.muted
  readonly property color successColor:
    paletteColor("green", Color.accent)
  readonly property color warningColor:
    paletteColor("yellow", Color.accent)
  readonly property color cautionColor:
    paletteColor("orange", Color.urgent)
  readonly property color infoColor:
    paletteColor("cyan", Color.accent)
  readonly property color selectionColor:
    paletteColor("magenta", Color.accent)
  readonly property color softInfoColor: tonedColor(infoColor, 0.68)
  readonly property color softSelectionColor:
    tonedColor(selectionColor, 0.62)

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() {
    if (root.opened) close()
    else open()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function refresh() {
    if (hostWidget && hostWidget.refresh) hostWidget.refresh()
  }

  function coordinate(lat, lon) {
    return QtPositioning.coordinate(Number(lat), Number(lon))
  }

  function parseThemePalette(raw) {
    var palette = ({})
    var pattern = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]+)"/gm
    var match
    while ((match = pattern.exec(String(raw || ""))) !== null)
      palette[match[1]] = match[2]
    themePalette = palette
  }

  function paletteColor(name, fallback) {
    var value = themePalette[name]
    return typeof value === "string" && value !== "" ? value : fallback
  }

  function batteryColor(percent) {
    if (percent === null || percent === undefined) return Color.muted
    var value = Number(percent)
    if (value <= 15) return Color.urgent
    if (value <= 35) return cautionColor
    if (value <= 60) return warningColor
    return successColor
  }

  function tonedColor(value, strength) {
    return Qt.rgba(
      Color.background.r + (value.r - Color.background.r) * strength,
      Color.background.g + (value.g - Color.background.g) * strength,
      Color.background.b + (value.b - Color.background.b) * strength,
      1
    )
  }

  function resetMap() {
    if (!mapData || !mapData.location) return
    mapView.map.center = coordinate(mapData.location.lat, mapData.location.lon)
    mapView.map.zoomLevel = 15.4
    selectedMarker = null
    mapInitialized = true
  }

  function sourceVehicles() {
    if (!mapData) return []
    if (Array.isArray(mapData.vehicles)) return mapData.vehicles
    return Array.isArray(mapData.markers) ? mapData.markers : []
  }

  function markerMatches(left, right) {
    if (!left || !right || left.count > 1 || right.count > 1) return false
    if (left.vehicleId && right.vehicleId)
      return left.vehicleId === right.vehicleId
    return Number(left.lat) === Number(right.lat)
      && Number(left.lon) === Number(right.lon)
  }

  function nearestVehicle() {
    var vehicles = sourceVehicles()
    return vehicles.length > 0 ? vehicles[0] : null
  }

  function isNearest(marker) {
    return markerMatches(marker, nearestVehicle())
  }

  function isSelected(marker) {
    return markerMatches(marker, selectedMarker)
  }

  function approximateDistance(marker) {
    if (!marker || marker.distanceKm === null
        || marker.distanceKm === undefined)
      return "Unknown distance"
    if (marker.distanceKm < 1) {
      var meters = marker.distanceKm * 1000
      return "≈" + Math.max(10, Math.round(meters / 10) * 10) + " m"
    }
    return "≈" + marker.distanceKm.toFixed(1) + " km"
  }

  function markerRange(marker) {
    if (!marker || marker.rangeKm === null || marker.rangeKm === undefined)
      return "Estimated range unavailable"
    return marker.rangeKm.toFixed(1) + " km estimated range"
  }

  function dataAgeLabel() {
    if (!mapData || !mapData.fetchedAt) return ""
    var age = Math.max(0, nowSeconds - Number(mapData.fetchedAt))
    if (age < 45) return "updated just now"
    var minutes = Math.max(1, Math.round(age / 60))
    return "updated " + minutes + " min ago"
  }

  function freshnessLabel() {
    return loading && mapData ? "updating…" : dataAgeLabel()
  }

  function locationLabel() {
    if (!mapData || !mapData.location) return ""
    if (fallbackLocation) return "Saved location"
    var accuracy = Number(mapData.location.accuracy || 0)
    return accuracy > 0
      ? "Location ±" + Math.round(accuracy) + " m"
      : "Current location"
  }

  function nearestTitle() {
    var nearest = nearestVehicle()
    if (!nearest) return "No scooters nearby"
    return "Nearest " + approximateDistance(nearest)
  }

  function availabilityLabel() {
    if (!mapData) return ""
    var nearest = nearestVehicle()
    var range = nearest && nearest.rangeKm !== null
        && nearest.rangeKm !== undefined
      ? nearest.rangeKm.toFixed(1) + " km est. range · "
      : ""
    return range + mapData.count + " within "
      + Number(mapData.radiusKm || 1.5).toFixed(1) + " km"
  }

  function statusMessage() {
    if (errorMessage !== "")
      return "Refresh failed · data " + dataAgeLabel()
    if (fallbackLocation)
      return "Saved location · not your current position"
    return ""
  }

  function clusterRadius() {
    var zoom = mapView.map.zoomLevel
    if (zoom >= 18) return 0
    if (zoom >= 17) return 18
    if (zoom >= 16) return 24
    if (zoom >= 15) return 36
    if (zoom >= 14) return 48
    return 60
  }

  function markerRecord(marker, active) {
    return {
      active: active,
      vehicleId: active ? marker.vehicleId : null,
      lat: active ? Number(marker.lat) : 0,
      lon: active ? Number(marker.lon) : 0,
      count: active ? Number(marker.count) : 0,
      distanceKm: active ? marker.distanceKm : null,
      rangeKm: active ? marker.rangeKm : null,
      batteryPercent: active ? marker.batteryPercent : null,
      elevationM: active ? marker.elevationM : null,
      endpointGainM: active ? marker.endpointGainM : null,
      effectiveUphillM: active ? marker.effectiveUphillM : null,
      uphillAdjustedDistanceKm:
        active ? marker.uphillAdjustedDistanceKm : null,
      offsetX: active ? Number(marker.offsetX || 0) : 0,
      offsetY: active ? Number(marker.offsetY || 0) : 0
    }
  }

  function publishMarkers(markers) {
    var capacity = Math.max(markers.length, sourceVehicles().length + 16)
    while (markerModel.count < capacity)
      markerModel.append(markerRecord(null, false))

    for (var i = 0; i < markers.length; i++)
      markerModel.set(i, markerRecord(markers[i], true))
    for (var j = markers.length; j < markerModel.count; j++) {
      if (markerModel.get(j).active)
        markerModel.setProperty(j, "active", false)
    }
    clusteredMarkers = markers
  }

  function individualMarkers(vehicles) {
    var markers = []
    for (var i = 0; i < vehicles.length; i++) {
      var vehicle = vehicles[i]
      var point = mapView.map.fromCoordinate(
        coordinate(vehicle.lat, vehicle.lon), false
      )
      markers.push({
        vehicleId: vehicle.vehicleId,
        lat: vehicle.lat,
        lon: vehicle.lon,
        count: 1,
        distanceKm: vehicle.distanceKm,
        rangeKm: vehicle.rangeKm,
        batteryPercent: vehicle.batteryPercent,
        elevationM: vehicle.elevationM,
        endpointGainM: vehicle.endpointGainM,
        effectiveUphillM: vehicle.effectiveUphillM,
        uphillAdjustedDistanceKm: vehicle.uphillAdjustedDistanceKm,
        screenX: point.x,
        screenY: point.y,
        offsetX: 0,
        offsetY: 0
      })
    }

    var minimumDistance = 42
    var maximumOffset = 28
    for (var iteration = 0; iteration < 10; iteration++) {
      for (var first = 0; first < markers.length; first++) {
        for (var second = first + 1; second < markers.length; second++) {
          var dx = markers[second].screenX + markers[second].offsetX
            - markers[first].screenX - markers[first].offsetX
          var dy = markers[second].screenY + markers[second].offsetY
            - markers[first].screenY - markers[first].offsetY
          var distance = Math.sqrt(dx * dx + dy * dy)
          if (distance >= minimumDistance) continue
          if (distance < 0.01) {
            var angle = (first * 2.399963 + second * 0.618034)
              % (Math.PI * 2)
            dx = Math.cos(angle)
            dy = Math.sin(angle)
            distance = 1
          }
          var push = (minimumDistance - distance) * 0.24
          var pushX = dx / distance * push
          var pushY = dy / distance * push
          markers[first].offsetX -= pushX
          markers[first].offsetY -= pushY
          markers[second].offsetX += pushX
          markers[second].offsetY += pushY
        }
      }
      for (var markerIndex = 0; markerIndex < markers.length; markerIndex++) {
        var marker = markers[markerIndex]
        var offsetLength = Math.sqrt(
          marker.offsetX * marker.offsetX + marker.offsetY * marker.offsetY
        )
        if (offsetLength <= maximumOffset) continue
        marker.offsetX = marker.offsetX / offsetLength * maximumOffset
        marker.offsetY = marker.offsetY / offsetLength * maximumOffset
      }
    }
    return markers
  }

  function rebuildClusters() {
    var vehicles = sourceVehicles()
    if (!mapView.map.mapReady || vehicles.length === 0) {
      publishMarkers([])
      return
    }

    var radius = clusterRadius()
    if (radius === 0) {
      publishMarkers(individualMarkers(vehicles))
      return
    }

    var clusters = []
    for (var i = 0; i < vehicles.length; i++) {
      var vehicle = vehicles[i]
      var point = mapView.map.fromCoordinate(
        coordinate(vehicle.lat, vehicle.lon), false
      )
      var cluster = null
      for (var j = 0; j < clusters.length; j++) {
        var dx = point.x - clusters[j].screenX
        var dy = point.y - clusters[j].screenY
        if (Math.sqrt(dx * dx + dy * dy) <= radius) {
          cluster = clusters[j]
          break
        }
      }
      if (!cluster) {
        clusters.push({
          vehicleId: vehicle.vehicleId,
          lat: vehicle.lat,
          lon: vehicle.lon,
          count: 1,
          distanceKm: vehicle.distanceKm,
          rangeKm: vehicle.rangeKm,
          batteryPercent: vehicle.batteryPercent,
          elevationM: vehicle.elevationM,
          endpointGainM: vehicle.endpointGainM,
          effectiveUphillM: vehicle.effectiveUphillM,
          uphillAdjustedDistanceKm: vehicle.uphillAdjustedDistanceKm,
          screenX: point.x,
          screenY: point.y,
          offsetX: 0,
          offsetY: 0
        })
        continue
      }
      var count = cluster.count + 1
      cluster.vehicleId = null
      cluster.lat = (cluster.lat * cluster.count + vehicle.lat) / count
      cluster.lon = (cluster.lon * cluster.count + vehicle.lon) / count
      cluster.screenX = (cluster.screenX * cluster.count + point.x) / count
      cluster.screenY = (cluster.screenY * cluster.count + point.y) / count
      cluster.count = count
      cluster.elevationM = null
      cluster.endpointGainM = null
      cluster.effectiveUphillM = null
      cluster.uphillAdjustedDistanceKm = null
      cluster.distanceKm = Math.min(cluster.distanceKm, vehicle.distanceKm)
      if (vehicle.rangeKm !== null && vehicle.rangeKm !== undefined)
        cluster.rangeKm = Math.max(cluster.rangeKm || 0, vehicle.rangeKm)
      if (vehicle.batteryPercent !== null && vehicle.batteryPercent !== undefined)
        cluster.batteryPercent = Math.max(
          cluster.batteryPercent || 0, vehicle.batteryPercent
        )
    }
    publishMarkers(clusters)
  }

  function selectedMarkerIsRendered() {
    if (!selectedMarker) return false
    for (var i = 0; i < clusteredMarkers.length; i++) {
      if (clusteredMarkers[i].count === 1
          && markerMatches(clusteredMarkers[i], selectedMarker))
        return true
    }
    return false
  }

  function keepMarkerVisible(marker) {
    if (!marker || !mapView.map.mapReady) return
    var markerCoordinate = coordinate(marker.lat, marker.lon)
    var point = mapView.map.fromCoordinate(markerCoordinate, false)
    var half = Style.space(26)
    var gap = Style.space(8)
    var minimumX = half + gap
    var maximumX = mapView.width - half - gap
    var minimumY = (showStatusStrip ? Style.space(46) : 0) + half + gap
    var maximumY = detailCard.y - half - gap
    var targetX = Math.max(minimumX, Math.min(maximumX, point.x))
    var targetY = Math.max(minimumY, Math.min(maximumY, point.y))
    if (Math.abs(targetX - point.x) > 1
        || Math.abs(targetY - point.y) > 1)
      mapView.map.alignCoordinateToPoint(
        markerCoordinate, Qt.point(targetX, targetY)
      )
  }

  function selectMarker(marker) {
    selectedMarker = marker
    Qt.callLater(function() { keepMarkerVisible(marker) })
  }

  function activateMarker(marker) {
    if (!marker) return
    if (marker.count > 1) {
      mapView.map.center = coordinate(marker.lat, marker.lon)
      mapView.map.zoomLevel = Math.min(
        mapView.maximumZoomLevel, mapView.map.zoomLevel + 2
      )
      clusterTimer.restart()
      return
    }
    selectMarker(marker)
  }

  function selectRelative(direction) {
    var vehicles = sourceVehicles()
    if (vehicles.length === 0) return
    var index = -1
    for (var i = 0; i < vehicles.length; i++) {
      if (markerMatches(vehicles[i], selectedMarker)) {
        index = i
        break
      }
    }
    index = (index + direction + vehicles.length) % vehicles.length
    selectMarker(vehicles[index])
  }

  function reconcileSelection() {
    if (!selectedMarker) return
    var vehicles = sourceVehicles()
    for (var i = 0; i < vehicles.length; i++) {
      if (markerMatches(vehicles[i], selectedMarker)) {
        selectedMarker = vehicles[i]
        return
      }
    }
    selectedMarker = null
  }

  onMapDataChanged: {
    reconcileSelection()
    if (!mapInitialized) resetMap()
    clusterTimer.restart()
  }

  FileView {
    path: Quickshell.env("HOME")
      + "/.local/state/omarchy/current/theme/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: root.parseThemePalette(text())
  }

  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.nowSeconds = Date.now() / 1000
  }

  Timer {
    id: clusterTimer
    interval: 100
    repeat: false
    onTriggered: root.rebuildClusters()
  }

  Plugin {
    id: mapPlugin
    name: "osm"

    PluginParameter {
      name: "osm.useragent"
      value: "fredrik.ryde-nearby/1.1"
    }
    PluginParameter {
      name: "osm.mapping.providersrepository.disabled"
      value: true
    }
    PluginParameter {
      name: "osm.mapping.custom.host"
      value: root.darkTheme
        ? "https://a.basemaps.cartocdn.com/dark_all/"
        : "https://a.basemaps.cartocdn.com/light_all/"
    }
    PluginParameter {
      name: "osm.mapping.cache.directory"
      value: (Quickshell.env("XDG_CACHE_HOME")
        || Quickshell.env("HOME") + "/.cache")
        + "/fredrik.ryde-nearby/tiles-"
        + (root.darkTheme ? "dark" : "light")
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    padding: 0
    contentWidth: panel.fittedContentWidth(Style.space(560))
    contentHeight: panel.fittedContentHeight(Style.space(468), Style.space(520))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: {
        if (root.selectedMarker) root.selectedMarker = null
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) {
        mapView.map.pan(dx * Style.space(48), dy * Style.space(48))
        clusterTimer.restart()
      }
      onActivateRequested: {
        if (!root.selectedMarker) root.selectRelative(1)
      }
      onDeleteRequested: root.selectedMarker = null
      onTextKey: function(text) {
        if (text === "r" || text === "R") root.refresh()
        else if (text === "c" || text === "C") root.resetMap()
        else if (text === "+" || text === "=")
          mapView.map.zoomLevel = Math.min(
            mapView.maximumZoomLevel, mapView.map.zoomLevel + 1
          )
        else if (text === "-" || text === "_")
          mapView.map.zoomLevel = Math.max(
            mapView.minimumZoomLevel, mapView.map.zoomLevel - 1
          )
        else if (text === "n") root.selectRelative(1)
        else if (text === "N" || text === "p" || text === "P")
          root.selectRelative(-1)
      }

      Rectangle {
        anchors.fill: parent
        radius: Style.cornerRadius
        clip: true
        color: Color.popups.background

        Rectangle {
          id: header
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          height: Style.space(56)
          color: Color.popups.background
          z: 30

          Item {
            id: headerIcon
            anchors.left: parent.left
            anchors.leftMargin: Style.space(12)
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(32)
            height: width

            ScooterIcon {
              anchors.centerIn: parent
              width: Style.space(27)
              height: width
              strokeColor: root.softInfoColor
            }
          }

          Column {
            anchors.left: headerIcon.right
            anchors.leftMargin: Style.space(12)
            anchors.right: headerActions.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: 0

            Text {
              width: parent.width
              text: root.nearestTitle()
              color: Color.popups.text
              elide: Text.ElideRight
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              width: parent.width
              text: root.availabilityLabel()
              color: root.softInfoColor
              elide: Text.ElideRight
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Text {
              width: parent.width
              text: root.locationLabel()
                + (root.mapData ? " · " + root.mapData.systemName : "")
                + (root.freshnessLabel() !== ""
                  ? " · " + root.freshnessLabel() : "")
              color: root.secondaryText
              elide: Text.ElideRight
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Row {
            id: headerActions
            anchors.right: parent.right
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(4)

            PanelActionButton {
              size: Style.space(36)
              iconText: "◎"
              fontSize: Style.font.iconLarge
              tooltipText: "Center on current location (C)"
              foreground: Color.popups.text
              focusable: true
              bordered: true
              Accessible.name: tooltipText
              Accessible.role: Accessible.Button
              onClicked: root.resetMap()
            }

            PanelActionButton {
              size: Style.space(36)
              iconText: root.loading ? "󰦖" : ""
              tooltipText: root.loading
                ? "Updating nearby scooters"
                : "Refresh location and scooters (R)"
              foreground: Color.popups.text
              focusable: true
              bordered: true
              enabled: !root.loading
              Accessible.name: tooltipText
              Accessible.role: Accessible.Button
              onClicked: root.refresh()
            }
          }

          Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 1
            color: Util.alpha(Color.popups.text, 0.12)
          }
        }

        Item {
          id: mapFrame
          anchors.top: header.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          clip: true

          StableMapView {
            id: mapView
            anchors.fill: parent
            map.plugin: mapPlugin
            map.center: root.coordinate(58.9700, 5.7331)
            map.zoomLevel: 15.4
            map.copyrightsVisible: true

            Connections {
              target: mapView.map
              function onMapReadyChanged() {
                if (!mapView.map.mapReady) return
                for (var i = 0;
                    i < mapView.map.supportedMapTypes.length; i++) {
                  if (mapView.map.supportedMapTypes[i].name
                      .toLowerCase().indexOf("custom") !== -1) {
                    mapView.map.activeMapType =
                      mapView.map.supportedMapTypes[i]
                    root.resetMap()
                    clusterTimer.restart()
                    break
                  }
                }
              }
              function onZoomLevelChanged() { clusterTimer.restart() }
            }

            MapCircle {
              parent: mapView.map
              visible: root.mapData && root.mapData.location
                && root.mapData.location.accuracy > 0
              center: root.mapData && root.mapData.location
                ? root.coordinate(
                  root.mapData.location.lat, root.mapData.location.lon
                )
                : root.coordinate(58.9700, 5.7331)
              radius: root.mapData && root.mapData.location
                ? root.mapData.location.accuracy : 0
              color: Util.alpha(Color.accent, 0.08)
              border.color: Util.alpha(root.softInfoColor, 0.5)
              border.width: 1
              z: 2
            }

            MapQuickItem {
              parent: mapView.map
              visible: root.mapData && root.mapData.location
              coordinate: root.mapData && root.mapData.location
                ? root.coordinate(
                  root.mapData.location.lat, root.mapData.location.lon
                )
                : root.coordinate(58.9700, 5.7331)
              anchorPoint.x: locationDot.width / 2
              anchorPoint.y: locationDot.height / 2
              z: 20

              sourceItem: Rectangle {
                id: locationDot
                width: 16
                height: 16
                radius: 8
                color: Color.accent
                border.color: root.softInfoColor
                border.width: 3
              }
            }

            MapItemView {
              parent: mapView.map
              model: markerModel

              delegate: MapQuickItem {
                id: scooterPoint
                required property bool active
                required property var vehicleId
                required property real lat
                required property real lon
                required property int count
                required property var distanceKm
                required property var rangeKm
                required property var batteryPercent
                required property var elevationM
                required property var endpointGainM
                required property var effectiveUphillM
                required property var uphillAdjustedDistanceKm
                required property real offsetX
                required property real offsetY
                readonly property var markerData: ({
                  "vehicleId": vehicleId,
                  "lat": lat,
                  "lon": lon,
                  "count": count,
                  "distanceKm": distanceKm,
                  "rangeKm": rangeKm,
                  "batteryPercent": batteryPercent,
                  "elevationM": elevationM,
                  "endpointGainM": endpointGainM,
                  "effectiveUphillM": effectiveUphillM,
                  "uphillAdjustedDistanceKm": uphillAdjustedDistanceKm,
                  "offsetX": offsetX,
                  "offsetY": offsetY
                })
                readonly property bool cluster: active && count > 1
                readonly property bool nearest:
                  active && !cluster && root.isNearest(markerData)
                readonly property bool selected:
                  active && !cluster && root.isSelected(markerData)
                readonly property real markerSize:
                  cluster
                    ? (count >= 50 ? 44 : count >= 10 ? 40 : 36)
                    : selected ? 40 : nearest ? 40 : 36

                visible: active
                coordinate: root.coordinate(lat, lon)
                anchorPoint.x: markerHit.width / 2
                  - Number(offsetX || 0)
                anchorPoint.y: markerHit.height / 2
                  - Number(offsetY || 0)
                z: selected ? 30 : nearest ? 20 : 10

                sourceItem: Item {
                  id: markerHit
                  width: scooterPoint.selected ? 56 : 48
                  height: width
                  Accessible.role: Accessible.Button
                  Accessible.name: scooterPoint.cluster
                    ? scooterPoint.count
                      + " scooters, nearest "
                      + root.approximateDistance(scooterPoint.markerData)
                    : "Scooter, "
                      + root.approximateDistance(scooterPoint.markerData)
                      + " away, "
                      + root.markerRange(scooterPoint.markerData)

                  Rectangle {
                    visible: scooterPoint.selected || scooterPoint.nearest
                    anchors.centerIn: parent
                    width: scooterPoint.selected ? 48 : 44
                    height: width
                    radius: width / 2
                    color: scooterPoint.selected
                      ? Util.alpha(root.softSelectionColor, 0.08)
                      : "transparent"
                    border.color: scooterPoint.selected
                      ? Util.alpha(root.softSelectionColor, 0.7)
                      : root.softInfoColor
                    border.width: 1
                    opacity: scooterPoint.selected ? 0.72 : 0.55
                  }

                  Rectangle {
                    id: markerDisc
                    anchors.centerIn: parent
                    width: scooterPoint.markerSize
                    height: width
                    radius: width / 2
                    color: scooterPoint.cluster
                      ? root.softInfoColor : Color.popups.background
                    border.color: scooterPoint.selected
                      ? root.softSelectionColor : root.softInfoColor
                    border.width: 2

                    Text {
                      id: clusterCount
                      visible: scooterPoint.cluster
                      anchors.centerIn: parent
                      anchors.horizontalCenterOffset: Style.spaceReal(0.5)
                      anchors.verticalCenterOffset: Style.spaceReal(1)
                      text: scooterPoint.count > 99
                        ? "99+" : scooterPoint.count
                      color: Color.background
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: true
                      renderType: Text.NativeRendering
                    }

                    ScooterIcon {
                      visible: !scooterPoint.cluster
                      anchors.centerIn: parent
                      width: parent.width * 0.61
                      height: width
                      strokeColor: Color.popups.text
                    }

                    Rectangle {
                      visible: !scooterPoint.cluster
                        && scooterPoint.batteryPercent !== null
                        && scooterPoint.batteryPercent !== undefined
                      anchors.right: parent.right
                      anchors.bottom: parent.bottom
                      anchors.rightMargin: -Style.spaceReal(1)
                      anchors.bottomMargin: -Style.spaceReal(1)
                      width: Style.space(9)
                      height: width
                      radius: width / 2
                      color: root.batteryColor(scooterPoint.batteryPercent)
                      border.color: Color.popups.background
                      border.width: 1
                    }
                  }

                  TapHandler {
                    onTapped: root.activateMarker(scooterPoint.markerData)
                  }
                }
              }
            }

            MapQuickItem {
              parent: mapView.map
              visible: root.selectedMarker !== null
                && !root.selectedMarkerIsRendered()
              coordinate: root.selectedMarker
                ? root.coordinate(
                  root.selectedMarker.lat, root.selectedMarker.lon
                )
                : root.coordinate(58.9700, 5.7331)
              anchorPoint.x: selectedPin.width / 2
              anchorPoint.y: selectedPin.height / 2
              z: 31

              sourceItem: Item {
                id: selectedPin
                width: 56
                height: 56

                Rectangle {
                  anchors.centerIn: parent
                  width: 48
                  height: 48
                  radius: 24
                  color: Util.alpha(root.softSelectionColor, 0.08)
                  border.color: Util.alpha(root.softSelectionColor, 0.7)
                  border.width: 1
                }

                Rectangle {
                  anchors.centerIn: parent
                  width: 40
                  height: 40
                  radius: 20
                  color: Color.popups.background
                  border.color: root.softSelectionColor
                  border.width: 2

                  ScooterIcon {
                    anchors.centerIn: parent
                    width: parent.width * 0.61
                    height: width
                    strokeColor: Color.popups.text
                  }

                  Rectangle {
                    visible: root.selectedMarker
                      && root.selectedMarker.batteryPercent !== null
                      && root.selectedMarker.batteryPercent !== undefined
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.rightMargin: -Style.spaceReal(1)
                    anchors.bottomMargin: -Style.spaceReal(1)
                    width: Style.space(10)
                    height: width
                    radius: width / 2
                    color: root.batteryColor(
                      root.selectedMarker
                        ? root.selectedMarker.batteryPercent : null
                    )
                    border.color: Color.popups.background
                    border.width: 1
                  }
                }
              }
            }
          }

          Rectangle {
            visible: root.showStatusStrip
            anchors.top: parent.top
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.topMargin: Style.space(8)
            width: Math.min(parent.width - Style.space(24), Style.space(390))
            height: Style.space(30)
            radius: Style.cornerRadius
            color: Util.alpha(
              root.errorMessage !== ""
                ? Color.urgent : root.warningColor, 0.2
            )
            border.color: root.errorMessage !== ""
              ? Color.urgent : root.warningColor
            z: 40

            Text {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(10)
              anchors.right: retryAction.visible
                ? retryAction.left : parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              text: root.statusMessage()
              color: Color.popups.text
              elide: Text.ElideRight
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            PanelActionButton {
              id: retryAction
              visible: root.errorMessage !== ""
              anchors.right: parent.right
              anchors.rightMargin: Style.space(2)
              anchors.verticalCenter: parent.verticalCenter
              size: Style.space(26)
              iconText: ""
              tooltipText: "Try again"
              foreground: Color.popups.text
              Accessible.name: tooltipText
              Accessible.role: Accessible.Button
              onClicked: root.refresh()
            }
          }

          Rectangle {
            id: detailCard
            visible: root.selectedMarker !== null
            anchors.bottom: parent.bottom
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottomMargin: Style.space(8)
            width: parent.width - Style.space(24)
            height: Style.space(68)
            radius: Style.cornerRadius
            color: Color.popups.background
            border.color: root.selectedMarker
              ? Util.alpha(
                root.batteryColor(root.selectedMarker.batteryPercent), 0.38
              )
              : Color.popups.border
            z: 40

            Column {
              id: detailCopy
              anchors.left: parent.left
              anchors.leftMargin: Style.space(12)
              anchors.right: chargeValue.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(1)

              Text {
                width: parent.width
                text: root.selectedMarker
                  ? root.approximateDistance(root.selectedMarker) + " away"
                  : ""
                color: Color.popups.text
                elide: Text.ElideRight
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }
              Text {
                width: parent.width
                text: root.markerRange(root.selectedMarker)
                color: Color.popups.text
                elide: Text.ElideRight
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }

            Column {
              id: chargeValue
              anchors.right: closeAction.left
              anchors.rightMargin: Style.space(6)
              anchors.top: detailCopy.top
              visible: root.selectedMarker
                && root.selectedMarker.batteryPercent !== null
                && root.selectedMarker.batteryPercent !== undefined
              spacing: 0

              Text {
                anchors.right: parent.right
                text: root.selectedMarker
                  ? "≈" + Math.round(root.selectedMarker.batteryPercent) + "%"
                  : ""
                color: root.batteryColor(
                  root.selectedMarker
                    ? root.selectedMarker.batteryPercent : null
                )
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }
              Text {
                anchors.right: parent.right
                text: "range est."
                color: root.secondaryText
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            PanelActionButton {
              id: closeAction
              anchors.right: parent.right
              anchors.rightMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              size: Style.space(36)
              iconText: "×"
              fontSize: Style.font.iconLarge
              tooltipText: "Close scooter details (X)"
              foreground: Color.popups.text
              focusable: true
              Accessible.name: tooltipText
              Accessible.role: Accessible.Button
              onClicked: root.selectedMarker = null
            }
          }

          Rectangle {
            visible: root.mapData && root.mapData.count === 0
              && root.errorMessage === ""
            anchors.centerIn: parent
            width: Math.min(
              parent.width - Style.space(48), Style.space(300)
            )
            height: emptyColumn.implicitHeight + Style.space(24)
            radius: Style.cornerRadius
            color: Color.popups.background
            border.color: Color.popups.border
            z: 35

            Column {
              id: emptyColumn
              anchors.centerIn: parent
              width: parent.width - Style.space(28)
              spacing: Style.space(6)

              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: "No available scooters"
                color: Color.popups.text
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }
              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: "Nothing within "
                  + Number(
                    root.mapData ? root.mapData.radiusKm || 1.5 : 1.5
                  ).toFixed(1) + " km"
                color: Color.muted
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          Rectangle {
            visible: (root.loading && !root.mapData)
              || (root.errorMessage !== "" && !root.mapData)
            anchors.centerIn: parent
            width: Math.min(
              parent.width - Style.space(48), Style.space(310)
            )
            height: messageColumn.implicitHeight + Style.space(24)
            radius: Style.cornerRadius
            color: Color.popups.background
            border.color: Color.popups.border
            z: 50

            Column {
              id: messageColumn
              anchors.centerIn: parent
              width: parent.width - Style.space(28)
              spacing: Style.space(8)

              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: root.loading ? "󰦖" : ""
                color: root.loading ? Color.accent : Color.urgent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
              }
              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                text: root.loading
                  ? "Finding your location and nearby scooters…"
                  : root.errorMessage
                color: Color.popups.text
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Button {
                visible: !root.loading
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Try again"
                onClicked: root.refresh()
              }
            }
          }
        }
      }
    }
  }
}
