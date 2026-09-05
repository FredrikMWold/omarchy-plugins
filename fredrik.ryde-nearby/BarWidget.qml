import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "fredrik.ryde-nearby"

  property var mapData: null
  property string processOutput: ""
  property string processError: ""
  property string errorMessage: ""
  property bool loading: false
  property bool cacheLoaded: false
  property bool usingCachedData: false
  property bool refreshFailed: false

  property var requirementsData: null
  property string requirementsOutput: ""
  property string requirementsError: ""
  property bool requirementsChecking: true
  property bool requirementsReady: false
  property bool installLaunched: false
  property bool reopenAfterPanelLoad: false

  readonly property string pluginPath:
    Quickshell.env("HOME") + "/.config/omarchy/plugins/fredrik.ryde-nearby"
  readonly property string cacheHome:
    Quickshell.env("XDG_CACHE_HOME") || Quickshell.env("HOME") + "/.cache"
  readonly property string cachePath:
    cacheHome + "/fredrik.ryde-nearby/data.json"
  readonly property string requirementsScript:
    pluginPath + "/check-requirements.sh"
  readonly property string setupScript:
    pluginPath + "/setup.sh"
  readonly property string stateHome:
    Quickshell.env("XDG_STATE_HOME")
      || Quickshell.env("HOME") + "/.local/state"
  readonly property string setupResultPath:
    stateHome + "/fredrik.ryde-nearby/setup-result.json"

  readonly property real nearestDistanceKm:
    mapData && mapData.nearestDistanceKm !== null
      ? Number(mapData.nearestDistanceKm)
      : -1
  readonly property real proximityDistanceKm:
    mapData && mapData.nearestProximityDistanceKm !== null
      && mapData.nearestProximityDistanceKm !== undefined
      ? Number(mapData.nearestProximityDistanceKm)
      : nearestDistanceKm
  readonly property color indicatorColor:
    !requirementsReady ? Color.muted
    : proximityDistanceKm >= 0 && proximityDistanceKm <= 0.05 ? "#22c55e"
    : proximityDistanceKm >= 0 && proximityDistanceKm <= 0.1 ? "#eab308"
    : proximityDistanceKm >= 0 && proximityDistanceKm <= 0.25 ? "#f97316"
    : "#ef4444"
  readonly property bool indicatorHollow:
    !requirementsReady || !mapData || nearestDistanceKm < 0
      || usingCachedData || refreshFailed
      || (mapData.location && mapData.location.isFallback)
  readonly property string dataStatusLabel:
    refreshFailed ? " · refresh failed"
    : mapData && mapData.location && mapData.location.isFallback
      ? " · using saved location"
      : usingCachedData ? " · cached data" : ""
  readonly property string distanceLabel:
    !requirementsReady ? "Ryde setup required"
    : nearestDistanceKm < 0
      ? "No scooters within 1.5 km" + dataStatusLabel
      : nearestDistanceKm < 1
        ? "≈" + Math.round(nearestDistanceKm * 100) * 10
          + " m to nearest scooter" + dataStatusLabel
        : "≈" + nearestDistanceKm.toFixed(1)
          + " km to nearest scooter" + dataStatusLabel

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
    if ("mapData" in target) target.mapData = root.mapData
    if ("loading" in target) target.loading = root.loading
    if ("errorMessage" in target) target.errorMessage = root.errorMessage
    if ("usingCachedData" in target)
      target.usingCachedData = root.usingCachedData
    if ("refreshFailed" in target) target.refreshFailed = root.refreshFailed
    if ("dependencyData" in target)
      target.dependencyData = root.requirementsData
    if ("dependencyChecking" in target)
      target.dependencyChecking = root.requirementsChecking
    if ("installLaunched" in target)
      target.installLaunched = root.installLaunched
    if ("dependencyError" in target)
      target.dependencyError = root.requirementsError
  }

  function loadPayload(raw, fromCache) {
    try {
      var text = String(raw || "").trim()
      if (text === "") return false
      var payload = JSON.parse(text)
      if (!payload || payload.error || !payload.location
          || (!Array.isArray(payload.vehicles)
            && !Array.isArray(payload.markers)))
        return false
      mapData = payload
      usingCachedData = fromCache
      if (fromCache) cacheLoaded = true
      else refreshFailed = false
      syncPanel()
      return true
    } catch (error) {
      return false
    }
  }

  function payloadError(raw) {
    try {
      var payload = JSON.parse(String(raw || "").trim())
      return payload && payload.error ? String(payload.error) : ""
    } catch (error) {
      return ""
    }
  }

  function applyRequirements(raw) {
    try {
      var payload = JSON.parse(String(raw || "").trim())
      if (!payload || payload.schema !== 1
          || !Array.isArray(payload.checks))
        throw new Error("Invalid requirements response")
      var nextReady = true
      for (var index = 0; index < payload.checks.length; index++) {
        var check = payload.checks[index]
        if (check.id === "qt" && check.ready === true) {
          check.ready = probeQtMapRuntime()
          if (!check.ready) check.status = "Missing"
        }
        if (check.ready !== true) nextReady = false
      }
      payload.ready = nextReady
      if (nextReady !== requirementsReady)
        reopenAfterPanelLoad = panelLoader.item
          && panelLoader.item.opened === true
      requirementsData = payload
      requirementsError = ""
      requirementsReady = nextReady
      if (nextReady) installLaunched = false
    } catch (error) {
      requirementsError = "Could not check Ryde requirements"
      requirementsReady = false
    }
    requirementsChecking = false
    syncPanel()
  }

  function probeQtMapRuntime() {
    var probe = null
    try {
      probe = Qt.createQmlObject(
        "import QtQuick\nimport QtLocation\nimport QtPositioning\nQtObject {}",
        root,
        "RydeQtMapProbe"
      )
      probe.destroy()
      return true
    } catch (error) {
      if (probe) probe.destroy()
      return false
    }
  }

  function applySetupResult(raw) {
    try {
      var result = JSON.parse(String(raw || "").trim())
      if (!result || !result.status) return
      if (result.status === "running") {
        installLaunched = true
      } else if (result.status === "failure") {
        installLaunched = false
        requirementsError = "Setup did not finish. Review the terminal output and try again."
      } else if (result.status === "success") {
        installLaunched = false
        checkRequirements()
      }
      syncPanel()
    } catch (error) {
      requirementsError = "Could not read the setup result"
      syncPanel()
    }
  }

  function checkRequirements() {
    if (requirementsProcess.running) return
    requirementsChecking = true
    requirementsOutput = ""
    requirementsError = ""
    syncPanel()
    requirementsProcess.running = true
  }

  function installDependencies() {
    if (installerLauncher.running) return
    installLaunched = true
    requirementsError = ""
    syncPanel()
    installerLauncher.running = true
  }

  function togglePanel() {
    if (!requirementsReady) checkRequirements()
    if (panelLoader.item) panelLoader.item.toggle()
  }

  function refresh() {
    if (!requirementsReady || fetchProcess.running) return
    loading = true
    errorMessage = ""
    processOutput = ""
    processError = ""
    syncPanel()
    fetchProcess.running = true
  }

  readonly property bool opened:
    panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing:
    panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }
  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  onMapDataChanged: syncPanel()
  onLoadingChanged: syncPanel()
  onErrorMessageChanged: syncPanel()
  onRequirementsDataChanged: syncPanel()
  onRequirementsCheckingChanged: syncPanel()
  onInstallLaunchedChanged: syncPanel()
  onRequirementsErrorChanged: syncPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl(
      root.requirementsReady ? "Panel.qml" : "SetupPanel.qml"
    )
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(function() {
        root.injectPanel()
        if (root.reopenAfterPanelLoad && panelLoader.item) {
          root.reopenAfterPanelLoad = false
          panelLoader.item.open()
        }
      })
    }
  }

  FileView {
    id: cacheFile
    path: root.cachePath
    watchChanges: false
    printErrors: false
    onLoaded: {
      root.cacheLoaded = true
      if (!root.mapData) root.loadPayload(text(), true)
    }
    onLoadFailed: root.cacheLoaded = true
  }

  FileView {
    path: root.setupResultPath
    watchChanges: true
    printErrors: false
    onLoaded: root.applySetupResult(text())
  }

  Process {
    id: requirementsProcess
    command: ["bash", root.requirementsScript]

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.requirementsOutput = String(text || "")
    }
    onExited: function(exitCode) {
      if (exitCode === 0) root.applyRequirements(root.requirementsOutput)
      else {
        root.requirementsChecking = false
        root.requirementsError = "Could not check Ryde requirements"
        root.syncPanel()
      }
    }
  }

  Process {
    id: installerLauncher
    command: [
      "omarchy", "launch", "floating", "terminal", "with", "presentation",
      "bash", root.setupScript
    ]
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.installLaunched = false
        root.requirementsError = "Could not open the Omarchy setup terminal"
      }
      requirementRetry.restart()
      root.syncPanel()
    }
  }

  Process {
    id: fetchProcess
    command: ["python", root.pluginPath + "/ryde_data.py"]

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.processOutput = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.processError = String(text || "")
    }
    onExited: function(exitCode) {
      root.loading = false
      if (exitCode === 0 && root.loadPayload(root.processOutput, false)) {
        root.errorMessage = ""
      } else {
        root.refreshFailed = true
        root.errorMessage = root.payloadError(root.processOutput)
          || root.processError.trim()
          || "Could not refresh Ryde scooters"
      }
      root.syncPanel()
    }
  }

  Timer {
    interval: root.installLaunched ? 2000 : 15000
    running: !root.requirementsReady
    repeat: true
    triggeredOnStart: true
    onTriggered: root.checkRequirements()
  }

  Timer {
    id: requirementRetry
    interval: 2500
    repeat: false
    onTriggered: root.checkRequirements()
  }

  Timer {
    interval: 60000
    running: root.requirementsReady
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Component {
    id: scooterStatusIcon

    Item {
      anchors.fill: parent

      ScooterIcon {
        anchors.centerIn: parent
        width: parent.width
        height: parent.height
        strokeColor: root.bar ? root.bar.foreground : Color.foreground
      }

      Rectangle {
        width: Math.max(5, parent.width * 0.34)
        height: width
        radius: width / 2
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: -Style.space(1)
        anchors.bottomMargin: -Style.space(1)
        color: root.indicatorColor
        border.color: root.bar ? root.bar.background : Color.background
        border.width: 1

        Rectangle {
          visible: root.indicatorHollow
          anchors.centerIn: parent
          width: Math.max(2, parent.width - 4)
          height: width
          radius: width / 2
          color: root.bar ? root.bar.background : Color.background
        }
      }
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: scooterStatusIcon
    slotSize: Style.bar.iconSlot
    tooltipText: root.distanceLabel
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton || mouseButton === Qt.RightButton)
        root.refresh()
      else
        root.togglePanel()
    }
  }
}
