import QtQuick
import qs.Commons
import qs.Ui

// Bar entry point for the overlay.
//
// Presenter Overlay is one plugin wearing two hats: the `overlay` kind owns the
// fullscreen drawing surface, this `bar-widget` kind owns the button that
// summons it. The shell keeps those paths separate — `isBarWidgetPanelPlugin`
// explicitly hands plugins that are also overlays back to the panel loader —
// so summon/hide still route to the overlay and this file stays a plain
// button.
//
// The overlay covers the bar once it is up, so this widget is a one-way door:
// it opens, and the overlay's own toolbar (or Escape) closes. The active
// state below is therefore mostly for the moment before the layer surface
// paints, and for the case where another caller hides the overlay.
BarWidget {
  id: root
  moduleName: "bylund.presenter-overlay"

  readonly property string pluginId: "bylund.presenter-overlay"

  // The bar hands us the host Bar, which carries the shell root. Everything
  // this widget does is an in-process call on it — no `omarchy-shell`
  // subprocess round-trip just to talk to the process we are already in.
  readonly property var host: bar ? bar.shell : null

  // `openPanelIds` is a plain object the shell replaces wholesale on every
  // summon/hide, so reading it here is a live binding rather than a snapshot.
  readonly property bool overlayOpen: host && host.openPanelIds
    ? host.openPanelIds[pluginId] === true
    : false

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function summonWith(payload) {
    if (!host || typeof host.summon !== "function") return
    host.summon(pluginId, JSON.stringify(payload))
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar

    // nf-fa-pencil. The bar's own font already carries it, so the widget
    // inherits whatever family and theme colors the user has configured.
    text: "\uf040"
    active: root.overlayOpen
    tooltipText: root.overlayOpen
      ? "Presenter Overlay is open — Esc or Exit to close"
      : "Draw on screen · right: spotlight · middle: clear"

    onPressed: function(mouseButton) {
      if (!root.host) return

      if (mouseButton === Qt.RightButton) {
        // Spotlight is the other thing a presenter reaches for mid-demo, and
        // it is worth a click of its own rather than two clicks through the
        // toolbar.
        root.summonWith({ mode: "spotlight" })
        return
      }

      if (mouseButton === Qt.MiddleButton) {
        // Hiding the overlay keeps the annotations, which is the point — but
        // it also means a stale drawing can be waiting the next time you open
        // it. This wipes it without putting the overlay up first.
        if (typeof root.host.callIfLoaded === "function")
          root.host.callIfLoaded(root.pluginId, "clearAnnotations", "")
        return
      }

      // Left click is the whole feature: one click and you are drawing.
      if (root.overlayOpen) root.host.hide(root.pluginId)
      else root.summonWith({ mode: "draw", tool: "pen" })
    }
  }
}
