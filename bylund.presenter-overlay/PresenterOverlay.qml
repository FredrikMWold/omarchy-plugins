import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import qs.Commons
import "components"

// Presenter Overlay — the fullscreen annotation surface.
//
// Follows the Omarchy overlay-plugin contract: the shell injects `shell`,
// `manifest` and `omarchyPath`, then calls open(payloadJson) / close(). The
// plugin reports its own visibility through `opened` and dismisses itself via
// shell.hide().
//
// Every color comes from the theme singletons in qs.Commons, so the overlay
// follows whatever Omarchy theme is active instead of shipping its own palette.
Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  readonly property string pluginId: (manifest && manifest.id) || "bylund.presenter-overlay"

  // --------------------------------------------------------------- state
  property bool opened: false

  // Spotlight and click ripples are layers rather than exclusive modes: you
  // can keep annotating while the screen is dimmed, which is what presenters
  // actually do.
  property bool spotlight: false
  property bool ripples: false
  property bool helpVisible: false
  property bool toolbarVisible: true

  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME")
    || (Quickshell.env("HOME") + "/.local/state")
  readonly property string helpStatePath: stateHome + "/omarchy/presenter-overlay.json"

  // The cheat sheet shows itself once after installation. Its state is kept
  // on disk so restarting the shell does not turn every launch into first run.
  property bool helpSeen: false
  property bool helpStateLoaded: false
  property bool pendingHelpCheck: false

  property string tool: "pen"
  property int colorIndex: 0
  property int penWidth: 4
  property real spotlightRadius: 180

  property real cursorX: 0
  property real cursorY: 0

  // Wheel deltas arrive in 1/8-degree units; Util.wheelSteps folds them into
  // whole notches and hands back the remainder to carry into the next event.
  property int wheelRemainder: 0

  readonly property int minPenWidth: 2
  readonly property int maxPenWidth: 40
  readonly property real minSpotlightRadius: 60
  readonly property real maxSpotlightRadius: 900

  // The four foundational theme roles, in the order the number keys select
  // them. Themes that define an accent get a pen that matches their identity.
  readonly property var pens: [Color.accent, Color.urgent, Color.foreground, Color.muted]
  readonly property color penColor: pens[Math.max(0, Math.min(pens.length - 1, colorIndex))]

  // ------------------------------------------------------ shell contract
  function open(payloadJson) {
    var payload = {}
    try {
      if (payloadJson) payload = JSON.parse(payloadJson) || {}
    } catch (e) {
      console.warn("presenter-overlay: ignoring unparseable payload:", payloadJson)
    }
    if (!Util.isPlainObject(payload)) payload = {}

    // A payload may arrive while the overlay is already open (a second hotkey
    // switching mode). Never wipe annotations on that path — only apply what
    // the payload asks for.
    if (payload.mode === "spotlight") {
      root.spotlight = true
      root.ripples = true
    } else if (payload.mode === "draw") {
      root.spotlight = false
    }
    if (root.isTool(payload.tool)) root.tool = payload.tool

    root.opened = true
    root.maybeShowFirstRunHelp()

    Qt.callLater(function() {
      keyCatcher.forceActiveFocus()
      root.centerCursor()
    })
  }

  function close() {
    root.opened = false
    root.helpVisible = false
    drawLayer.cancel()
  }

  // Hiding with the toggle hotkey parks the overlay with annotations intact so
  // you can bring the same drawing back. Escape is the destructive exit.
  function dismiss() {
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  // ------------------------------------------------------- IPC surface
  // Reachable as: omarchy-shell shell call bylund.presenter-overlay <method> <arg>
  function setTool(name) {
    if (!root.isTool(name)) return "unknown tool"
    root.tool = name
    return root.tool
  }

  function toggleSpotlight() {
    root.spotlight = !root.spotlight
    return root.spotlight ? "on" : "off"
  }

  function toggleRipples() {
    root.ripples = !root.ripples
    return root.ripples ? "on" : "off"
  }

  function clearAnnotations() {
    drawLayer.clear()
  }

  // ------------------------------------------------------------ helpers
  function isTool(name) {
    return name === "pen" || name === "arrow" || name === "rect"
  }

  function maybeShowFirstRunHelp() {
    if (!root.helpStateLoaded) {
      root.pendingHelpCheck = true
      return
    }
    if (root.helpSeen) return

    root.helpSeen = true
    root.helpVisible = true
    helpStateFile.setText(JSON.stringify({ version: 1, helpSeen: true }, null, 2) + "\n")
  }

  function loadHelpState(raw) {
    if (root.helpStateLoaded) return

    try {
      var parsed = JSON.parse(raw || "{}")
      root.helpSeen = Util.isPlainObject(parsed)
        && parsed.version === 1
        && parsed.helpSeen === true
    } catch (e) {
      console.warn("presenter-overlay: help state parse failed:", e)
    }

    root.helpStateLoaded = true
    var shouldCheck = root.pendingHelpCheck && root.opened
    root.pendingHelpCheck = false
    if (shouldCheck) root.maybeShowFirstRunHelp()
  }

  function centerCursor() {
    if (panel.width > 0 && panel.height > 0 && root.cursorX === 0 && root.cursorY === 0) {
      root.cursorX = panel.width / 2
      root.cursorY = panel.height / 2
    }
  }

  function setColorIndex(index) {
    if (index < 0 || index >= root.pens.length) return
    root.colorIndex = index
  }

  function nudgePenWidth(steps) {
    root.penWidth = Util.clamp(root.penWidth + steps * 2, root.minPenWidth, root.maxPenWidth)
  }

  function nudgeSpotlightRadius(steps) {
    root.spotlightRadius = Util.clamp(root.spotlightRadius + steps * 24,
                                      root.minSpotlightRadius, root.maxSpotlightRadius)
  }

  // Escape is the "done presenting" key: wipe the annotations and get out of
  // the way in one press.
  function clearAndDismiss() {
    drawLayer.clear()
    root.spotlight = false
    root.dismiss()
  }

  FileView {
    id: helpStateFile
    path: root.helpStatePath
    watchChanges: false
    atomicWrites: true
    printErrors: false
    onLoaded: root.loadHelpState(text())
    onLoadFailed: root.loadHelpState("")
  }

  Component.onCompleted: Qt.callLater(function() { helpStateFile.reload() })

  PanelWindow {
    id: panel

    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-presenter-overlay"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    SpotlightLayer {
      anchors.fill: parent
      visible: root.spotlight
      centerX: root.cursorX
      centerY: root.cursorY
      radius: root.spotlightRadius
    }

    DrawLayer {
      id: drawLayer
      anchors.fill: parent
      tool: root.tool
      penColor: root.penColor
      penWidth: root.penWidth
    }

    // Sits above the drawing surface but below the chrome, so toolbar and
    // cheat-sheet clicks are never swallowed by the canvas.
    MouseArea {
      id: canvasArea
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      cursorShape: Qt.CrossCursor

      onPositionChanged: function(mouse) {
        root.cursorX = mouse.x
        root.cursorY = mouse.y
        drawLayer.extend(mouse.x, mouse.y)
      }

      onPressed: function(mouse) {
        root.cursorX = mouse.x
        root.cursorY = mouse.y
        if (root.ripples) rippleLayer.spawn(mouse.x, mouse.y)
        if (mouse.button === Qt.LeftButton) drawLayer.begin(mouse.x, mouse.y)
        else drawLayer.undo()
      }

      onReleased: function(mouse) {
        if (mouse.button === Qt.LeftButton) drawLayer.commit()
      }

      onCanceled: drawLayer.cancel()

      // Plain wheel resizes whatever the presenter is most likely aiming at:
      // the spotlight when it is up, the pen otherwise. Shift always means pen.
      onWheel: function(wheel) {
        var notches = Util.wheelSteps(root.wheelRemainder, wheel.angleDelta.y)
        root.wheelRemainder = notches.remainder
        if (notches.steps === 0) return
        if (root.spotlight && !(wheel.modifiers & Qt.ShiftModifier)) root.nudgeSpotlightRadius(notches.steps)
        else root.nudgePenWidth(notches.steps)
      }
    }

    ClickRipples {
      id: rippleLayer
      anchors.fill: parent
      rippleColor: root.penColor
    }

    Toolbar {
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.bottom: parent.bottom
      anchors.bottomMargin: Style.space(28)
      visible: root.toolbarVisible && !root.helpVisible

      tool: root.tool
      pens: root.pens
      colorIndex: root.colorIndex
      penColor: root.penColor
      penWidth: root.penWidth
      spotlight: root.spotlight
      ripples: root.ripples
      canUndo: drawLayer.canUndo
      canRedo: drawLayer.canRedo

      onToolPicked: function(name) { root.setTool(name) }
      onColorPicked: function(index) { root.setColorIndex(index) }
      onSpotlightToggled: root.toggleSpotlight()
      onRipplesToggled: root.toggleRipples()
      onUndoRequested: drawLayer.undo()
      onRedoRequested: drawLayer.redo()
      onClearRequested: drawLayer.clear()
      onHelpRequested: root.helpVisible = true
      onHideRequested: root.dismiss()
      onExitRequested: root.clearAndDismiss()
    }

    HelpOverlay {
      anchors.fill: parent
      visible: root.helpVisible
      onDismissed: root.helpVisible = false
    }

    Item {
      id: keyCatcher
      anchors.fill: parent
      focus: true

      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function(event) {
        var ctrl = (event.modifiers & Qt.ControlModifier) !== 0
        var shift = (event.modifiers & Qt.ShiftModifier) !== 0
        event.accepted = true

        switch (event.key) {
        case Qt.Key_Escape:
          if (root.helpVisible) root.helpVisible = false
          else root.clearAndDismiss()
          return
        case Qt.Key_Question:
        case Qt.Key_F1:
        case Qt.Key_H:
          root.helpVisible = !root.helpVisible
          return
        case Qt.Key_D:
          root.tool = "pen"
          return
        case Qt.Key_A:
          root.tool = "arrow"
          return
        case Qt.Key_R:
          root.tool = "rect"
          return
        case Qt.Key_1:
        case Qt.Key_2:
        case Qt.Key_3:
        case Qt.Key_4:
          root.setColorIndex(event.key - Qt.Key_1)
          return
        case Qt.Key_Plus:
        case Qt.Key_Equal:
        case Qt.Key_BracketRight:
          root.nudgePenWidth(1)
          return
        case Qt.Key_Minus:
        case Qt.Key_Underscore:
        case Qt.Key_BracketLeft:
          root.nudgePenWidth(-1)
          return
        case Qt.Key_S:
          root.toggleSpotlight()
          return
        case Qt.Key_P:
          root.toggleRipples()
          return
        case Qt.Key_T:
          root.toolbarVisible = !root.toolbarVisible
          return
        case Qt.Key_C:
          drawLayer.clear()
          return
        case Qt.Key_U:
          drawLayer.undo()
          return
        case Qt.Key_Z:
          if (ctrl && shift) drawLayer.redo()
          else drawLayer.undo()
          return
        case Qt.Key_Y:
          if (ctrl) drawLayer.redo()
          return
        }

        event.accepted = false
      }
    }
  }
}
