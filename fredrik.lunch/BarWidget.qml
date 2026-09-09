import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "fredrik.lunch"

  property var menuData: null
  property string processOutput: ""
  property string processError: ""
  property string errorMessage: ""
  property bool loading: false
  property bool stale: false

  readonly property string pluginPath:
    Quickshell.env("HOME") + "/.config/omarchy/plugins/fredrik.lunch"

  function refresh() {
    if (fetchProcess.running) return
    loading = true
    processOutput = ""
    processError = ""
    fetchProcess.running = true
  }

  function applyResponse(raw, exitCode) {
    loading = false

    try {
      var payload = JSON.parse(String(raw || "").trim())
      if (!payload || payload.error) {
        errorMessage = payload && payload.error
          ? String(payload.error)
          : "Could not load today's lunch menu"
        return
      }

      menuData = payload
      stale = payload.stale === true
      errorMessage = ""
      syncPanel()
    } catch (error) {
      errorMessage = processError.trim()
        || (exitCode === 0
          ? "The lunch service returned invalid data"
          : "Could not load today's lunch menu")
      syncPanel()
    }
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.settings = root.settings
    target.anchorItem = button
    target.hostWidget = root
    syncPanel()
  }

  function syncPanel() {
    var target = panelLoader.item
    if (!target) return
    target.menuData = root.menuData
    target.loading = root.loading
    target.errorMessage = root.errorMessage
    target.stale = root.stale
  }

  function togglePanel() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  readonly property bool opened:
    panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing:
    panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: refresh()
  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  onMenuDataChanged: syncPanel()
  onLoadingChanged: syncPanel()
  onErrorMessageChanged: syncPanel()
  onStaleChanged: syncPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  Process {
    id: fetchProcess
    command: ["python", root.pluginPath + "/lunch.py"]

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.processOutput = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.processError = String(text || "")
    }
    onExited: function(exitCode) {
      root.applyResponse(root.processOutput, exitCode)
    }
  }

  Timer {
    interval: 30 * 60 * 1000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""

    onPressed: function(button) {
      if (button === Qt.RightButton || button === Qt.MiddleButton) root.refresh()
      else root.togglePanel()
    }
  }
}
