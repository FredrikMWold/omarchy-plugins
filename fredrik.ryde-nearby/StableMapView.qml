// Based on Qt Location's MapView.qml.
// Copyright (C) 2023 The Qt Company Ltd.
// SPDX-License-Identifier: LicenseRef-Qt-Commercial OR LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only

import QtQuick
import QtLocation as QL
import QtPositioning as QP

Item {
  id: root

  property alias map: map
  property real minimumZoomLevel: map.minimumZoomLevel
  property real maximumZoomLevel: map.maximumZoomLevel

  QL.Map {
    id: map
    width: parent.width
    height: parent.height

    PinchHandler {
      id: pinch
      target: null
      rotationAxis.enabled: false
      property real startZoom: 0
      property QP.geoCoordinate lockedCenter

      onActiveChanged: {
        if (active) {
          startZoom = map.zoomLevel
          lockedCenter = map.center
        } else {
          var stableCenter = lockedCenter
          map.center = stableCenter
          Qt.callLater(function() { map.center = stableCenter })
        }
      }

      onScaleChanged: {
        if (!active || activeScale <= 0) return
        map.zoomLevel = Math.max(
          root.minimumZoomLevel,
          Math.min(root.maximumZoomLevel, startZoom + Math.log2(activeScale))
        )
        map.center = lockedCenter
      }

      grabPermissions: PointerHandler.TakeOverForbidden
    }

    WheelHandler {
      id: wheel
      acceptedDevices: Qt.platform.pluginName === "wayland"
        ? PointerDevice.Mouse | PointerDevice.TouchPad
        : PointerDevice.Mouse
      onWheel: function(event) {
        if (event.pixelDelta.x !== 0 || event.pixelDelta.y !== 0) {
          event.accepted = false
          return
        }
        var location = map.toCoordinate(point.position)
        map.zoomLevel = Math.max(
          root.minimumZoomLevel,
          Math.min(root.maximumZoomLevel, map.zoomLevel + event.angleDelta.y / 480)
        )
        map.alignCoordinateToPoint(location, point.position)
      }
    }

    DragHandler {
      id: drag
      target: null
      minimumPointCount: 1
      maximumPointCount: 1
      property QP.geoCoordinate draggedCoordinate
      onActiveChanged: {
        if (!active) return
        draggedCoordinate = map.toCoordinate(centroid.pressPosition)
        map.alignCoordinateToPoint(draggedCoordinate, centroid.position)
      }
      onTranslationChanged: {
        if (active)
          map.alignCoordinateToPoint(draggedCoordinate, centroid.position)
      }
    }
  }
}
