pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui as Ui

Ui.BarWidget {
  id: root
  moduleName: "fredrik.dreame"

  property var backendState: null
  property var devices: []
  property var deviceStatus: null
  property var confirmation: null
  property string rawResult: ""
  property string errorMessage: ""
  property string errorOperation: ""
  property string output: ""
  property string processError: ""
  property var pendingRequest: ({})
  property bool loginWaiting: false
  property double loginDeadline: 0
  property var loginOldExpiry: null
  property string waitingKind: ""
  property bool authBlocked: false
  property double clockNow: Date.now()
  readonly property bool busy: requestProcess.running
  readonly property string pluginPath:
    Quickshell.env("HOME") + "/.config/omarchy/plugins/fredrik.dreame"
  readonly property bool opened: dreamePanel.opened
  readonly property bool popoutSwitchClosing: dreamePanel.popoutSwitchClosing
  readonly property bool signedIn: backendState && backendState.sessionAvailable === true
  readonly property string selectedDid: backendState ? backendState.selectedDid || "" : ""
  readonly property var program: backendState ? backendState.program : null
  readonly property bool voiceStopping: backendState && backendState.voice && backendState.voice.phase === "stopping"
  readonly property bool voiceBusy: voiceStopping || (backendState && backendState.voice && backendState.voice.phase === "transcribing")
  readonly property bool programActive: program && [
    "queued", "planning", "validating", "applying", "starting", "running", "waiting"
  ].indexOf(program.phase) >= 0
  readonly property bool canEndProgram: !voiceStopping && program && (programActive
    || (["failed", "cancelled"].indexOf(program.phase) >= 0 && program.dispatched
      && !program.dockAttempted && (!program.stopAttempted || program.stopAcknowledged)))
  readonly property string endProgramLabel: !program || !program.dispatched ? "Cancel request"
    : program.stopAcknowledged ? "Return to dock" : "End cleaning and return to dock"
  readonly property string programControlMessage: !program ? ""
    : program.dockAttempted ? (program.dockAcknowledged
      ? "Return to dock requested. Check the live vacuum status."
      : voiceStopping ? "Requesting return to dock..." : "Return to dock could not be verified. Please check the vacuum.")
    : program.stopAttempted ? (program.stopAcknowledged
      ? voiceStopping ? "Stop acknowledged. Requesting return to dock..." : "Stop acknowledged. Check the live vacuum status."
      : voiceStopping ? "Ending cleaning..." : "Stop could not be verified. Please check the vacuum.") : ""

  function stopProgram() {
    if (busy || !canEndProgram) return
    request({op: "cancel-program", programId: program.id})
  }

  function request(payload) {
    if (busy) return
    pendingRequest = payload
    output = ""
    processError = ""
    requestProcess.running = true
  }

  function refresh() {
    if (!signedIn || authBlocked) request({op: "state"})
    else if (selectedDid === "") request({op: "devices"})
    else request({op: "status", deviceId: selectedDid})
  }

  function launch(script) {
    if (terminalLauncher.running) return
    loginOldExpiry = backendState ? backendState.expiresAt : null
    waitingKind = script
    loginWaiting = true
    loginDeadline = Date.now() + 300000
    terminalLauncher.command = [
      "omarchy", "launch", "floating", "terminal", "with", "presentation",
      "bash", pluginPath + "/" + script
    ]
    terminalLauncher.running = true
  }

  function previewCommand(text) {
    confirmation = null
    try {
      var value = JSON.parse(text)
      if (!value || Object.keys(value).sort().join(",") !== "method,params")
        throw new Error("Use exactly method and params in the command JSON.")
      request({op: "prepare", deviceId: selectedDid, method: value.method, params: value.params})
    } catch (error) {
      errorMessage = "Invalid command JSON: " + String(error)
    }
  }

  function readProperties(text) {
    try {
      var value = JSON.parse(text)
      request({op: "read-properties", deviceId: selectedDid, properties: value})
    } catch (error) {
      errorMessage = "Invalid property JSON: " + String(error)
    }
  }

  function confirmCommand() {
    if (!confirmation || busy) return
    var token = confirmation.token
    confirmation = null
    request({op: "confirm", token: token})
  }

  function open() { dreamePanel.open() }
  function close() { dreamePanel.close() }
  function closeForPopoutSwitch() { dreamePanel.closeForPopoutSwitch() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Panel {
    id: dreamePanel
    visible: false
    bar: root.bar
    settings: root.settings
    anchorItem: button
    hostWidget: root
  }

  Process {
    id: requestProcess
    command: ["bash", root.pluginPath + "/run.sh", "request"]
    stdinEnabled: true
    onStarted: write(JSON.stringify(root.pendingRequest) + "\n")
    stdout: StdioCollector { onStreamFinished: root.output = text }
    stderr: StdioCollector { onStreamFinished: root.processError = text }
    onExited: function(exitCode) {
      var op = root.pendingRequest.op
      var next = ""
      var previousProgram = root.program
      try {
        var response = JSON.parse(root.output.trim())
        if (!response || typeof response.ok !== "boolean")
          throw new Error("Invalid backend response")
        if (response.state) {
          if (response.state.schema !== 1) throw new Error("Unsupported backend state")
          var oldDid = root.selectedDid
          root.backendState = response.state
          if (oldDid !== root.selectedDid || !root.signedIn) {
            root.deviceStatus = null
            root.rawResult = ""
            root.confirmation = null
          }
          if (root.programActive) root.confirmation = null
        }
        if (!response.ok) {
          root.errorOperation = op
          root.errorMessage = response.error && response.error.message
            ? String(response.error.message) : "Dreame request failed."
          if (response.error && ["AUTH_REQUIRED", "AUTH_REJECTED"].indexOf(response.error.code) >= 0)
            root.authBlocked = true
          if (op === "confirm") root.deviceStatus = null
        } else {
          if (op !== "state" || root.errorOperation === "state") root.errorMessage = ""
          var data = response.data || {}
          if (data.devices) root.devices = data.devices
          if (op === "status") {
            if (data.device && data.device.did === root.selectedDid) {
              root.deviceStatus = data
              root.clockNow = Date.now()
            }
            else next = "status"
          }
          if (op === "read-properties" || op === "confirm")
            root.rawResult = JSON.stringify(data, null, 2)
          if (data.confirmation) root.confirmation = data.confirmation
          if (op === "cancel" || op === "logout" || op === "enable-mutations" || op === "select")
            root.confirmation = null
          if (op === "logout") {
            root.devices = []
            root.deviceStatus = null
            root.rawResult = ""
            root.authBlocked = false
          }
          if (op === "select") {
            root.deviceStatus = null
            root.rawResult = ""
            next = "status"
          }
          if (op === "devices" && root.selectedDid !== "") next = "status"
          if (op === "cancel-program" && data.dockAcknowledged && root.selectedDid !== "") next = "status"
          if (op !== "status" && root.program && root.program.dockAcknowledged && root.selectedDid !== ""
              && (!previousProgram || previousProgram.id !== root.program.id || !previousProgram.dockAcknowledged))
            next = "status"
          if (op === "state" && root.signedIn && !root.authBlocked && root.devices.length === 0)
            next = "devices"
          if (root.loginWaiting && root.backendState) {
            if (root.signedIn && root.backendState.expiresAt !== root.loginOldExpiry) {
              root.loginWaiting = false
              root.authBlocked = false
              root.deviceStatus = null
              root.devices = []
              next = "devices"
            } else if (root.waitingKind === "setup.sh" && root.backendState.profileReady) {
              root.loginWaiting = false
              root.errorMessage = "Setup is ready. Choose Sign in to connect your account."
            }
          }
        }
        if (exitCode !== 0 && response.ok)
          throw new Error("Backend exited unsuccessfully")
      } catch (error) {
        root.errorOperation = op
        root.errorMessage = "Dreame backend failed. Run Setup, then refresh."
        console.warn("Dreame backend response failed: " + String(error))
      }
      root.pendingRequest = ({})
      if (next !== "") {
        var follow = {op: next}
        if (next === "status") follow.deviceId = root.selectedDid
        Qt.callLater(function() { root.request(follow) })
      }
    }
  }

  Process {
    id: terminalLauncher
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.loginWaiting = false
        root.errorMessage = "Could not launch the private Omarchy setup/login terminal."
      }
    }
  }

  Timer {
    interval: 3000
    repeat: true
    running: root.loginWaiting
    onTriggered: {
      if (Date.now() >= root.loginDeadline) {
        root.loginWaiting = false
        root.errorMessage = "Login/setup has not completed. Review its terminal, then refresh."
      } else {
        root.request({op: "state"})
      }
    }
  }

  Timer {
    interval: root.programActive ? 10000 : 30000
    repeat: true
    running: root.signedIn && !root.authBlocked && !root.loginWaiting
    onTriggered: root.refresh()
  }

  Timer {
    interval: 2000
    repeat: true
    running: root.signedIn && (root.programActive || root.voiceBusy || root.opened) && !root.loginWaiting
    onTriggered: root.request({op: "state"})
  }

  Timer {
    interval: 10000
    repeat: true
    running: root.opened
    onTriggered: root.clockNow = Date.now()
  }

  Component {
    id: icon
    Item {
      anchors.fill: parent
      Text {
        anchors.centerIn: parent
        text: "󰚩"
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
        color: root.errorMessage !== "" || (root.program && root.program.phase === "failed")
          ? Color.urgent : root.deviceStatus || root.programActive || root.voiceBusy ? Color.accent : Color.muted
      }
    }
  }

  Ui.BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: icon
    slotSize: Style.bar.iconSlot
    tooltipText: root.voiceStopping ? "Dreame: ending cleaning"
      : root.voiceBusy ? "Dreame: transcribing voice locally"
      : root.programActive ? "Dreame: " + root.program.phase
      + (root.program.currentTarget ? " · " + root.program.currentTarget : "")
      : root.deviceStatus
      ? "Dreame: " + root.deviceStatus.stateLabel
      : root.signedIn ? "Dreame: choose or refresh a vacuum" : "Dreame: sign in required"
    onPressed: function(mouseButton) {
      root.refresh()
      if (mouseButton !== Qt.RightButton) dreamePanel.toggle()
    }
  }

  Component.onCompleted: request({op: "state"})
}
