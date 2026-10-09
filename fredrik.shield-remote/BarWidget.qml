pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui as Ui

Ui.BarWidget {
  id: root
  moduleName: "fredrik.shield-remote"

  property var backendState: null
  property string errorMessage: ""
  property string statusOutput: ""
  property string statusError: ""
  property string requestOutput: ""
  property string requestError: ""
  property var pendingRequest: ({})
  property string serviceError: ""
  readonly property bool busy: requestProcess.running || serviceProcess.running
  readonly property string pluginPath:
    Quickshell.env("HOME") + "/.config/omarchy/plugins/fredrik.shield-remote"
  readonly property string target:
    backendState ? backendState.target : "computer"
  readonly property bool opened: remotePanel.opened
  readonly property bool popoutSwitchClosing: remotePanel.popoutSwitchClosing

  function applyResponse(raw, clearError) {
    var response = JSON.parse(String(raw).trim())
    if (!response || typeof response.ok !== "boolean")
      throw new Error("Invalid Shield Remote response")
    if (response.state) {
      if (response.state.schema !== 1)
        throw new Error("Unsupported Shield Remote state")
      backendState = response.state
    }
    if (!response.ok) {
      errorMessage = String(response.error || "Shield Remote command failed")
      console.warn("Shield Remote: " + errorMessage)
    } else if (clearError) {
      errorMessage = ""
    }
  }

  function refresh() {
    if (statusProcess.running) return
    statusOutput = ""
    statusError = ""
    statusProcess.running = true
  }

  function request(payload) {
    if (busy) return
    errorMessage = ""
    pendingRequest = payload
    requestOutput = ""
    requestError = ""
    requestProcess.running = true
  }

  function service(action) {
    if (busy) return
    serviceError = ""
    serviceProcess.command = [
      "systemctl", "--user", action, "omarchy-shield-remote.service"
    ]
    serviceProcess.running = true
  }

  function setup() {
    if (!setupProcess.running) setupProcess.running = true
  }

  function open() { remotePanel.open() }
  function close() { remotePanel.close() }
  function closeForPopoutSwitch() { remotePanel.closeForPopoutSwitch() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Panel {
    id: remotePanel
    visible: false
    bar: root.bar
    settings: root.settings
    anchorItem: button
    hostWidget: root
  }

  Process {
    id: statusProcess
    command: ["bash", root.pluginPath + "/run.sh", "status"]
    stdout: StdioCollector { onStreamFinished: root.statusOutput = text }
    stderr: StdioCollector { onStreamFinished: root.statusError = text }
    onExited: function(exitCode) {
      try {
        root.applyResponse(root.statusOutput, false)
      } catch (error) {
        root.backendState = null
        root.errorMessage = root.statusError.trim() || String(error)
        console.warn("Shield Remote status: " + root.errorMessage)
      }
      if (exitCode !== 0 && root.errorMessage === "") {
        root.errorMessage = root.statusError.trim() || "Could not read Shield Remote status"
        console.warn(root.errorMessage)
      }
    }
  }

  Process {
    id: requestProcess
    command: ["bash", root.pluginPath + "/run.sh", "request"]
    stdinEnabled: true
    onStarted: {
      write(JSON.stringify(root.pendingRequest) + "\n")
      root.pendingRequest = ({})
    }
    stdout: StdioCollector { onStreamFinished: root.requestOutput = text }
    stderr: StdioCollector { onStreamFinished: root.requestError = text }
    onExited: function(exitCode) {
      try {
        root.applyResponse(root.requestOutput, true)
      } catch (error) {
        root.errorMessage = root.requestError.trim() || String(error)
        console.warn("Shield Remote request: " + root.errorMessage)
      }
      if (exitCode !== 0 && root.errorMessage === "")
        root.errorMessage = root.requestError.trim() || "Shield Remote command failed"
      root.refresh()
    }
  }

  Process {
    id: serviceProcess
    stderr: StdioCollector { onStreamFinished: root.serviceError = text }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.errorMessage = root.serviceError.trim() || "Could not change the user service"
        console.warn(root.errorMessage)
      } else {
        root.errorMessage = ""
      }
      root.refresh()
    }
  }

  Process {
    id: setupProcess
    command: [
      "omarchy", "launch", "floating", "terminal", "with", "presentation",
      "bash", root.pluginPath + "/setup.sh"
    ]
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.errorMessage = "Could not open the Omarchy setup terminal"
        console.warn(root.errorMessage)
      }
      root.refresh()
    }
  }

  Timer {
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Component {
    id: statusIcon
    Item {
      anchors.fill: parent
      Text {
        anchors.centerIn: parent
        text: root.target === "shield" ? "󰄛" : "󰍹"
        color: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.icon
      }
      Rectangle {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        width: Style.space(6)
        height: width
        radius: width / 2
        color: !root.backendState || !root.backendState.running
          ? Color.muted
          : !root.backendState.input.captured
            ? Color.urgent
            : root.target === "shield" && !root.backendState.shield.connected
              ? Color.urgent : Color.accent
      }
    }
  }

  Ui.BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: statusIcon
    slotSize: Style.bar.iconSlot
    tooltipText: "Shield Remote: " + (root.target === "shield" ? "Shield TV" : "Computer")
      + (root.backendState && root.backendState.input.captured ? "" : " (setup/input required)")
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton)
        root.request({command: "target", target: root.target === "shield" ? "computer" : "shield"})
      else {
        root.refresh()
        remotePanel.toggle()
      }
    }
  }
}
