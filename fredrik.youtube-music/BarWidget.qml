import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "fredrik.youtube-music"

  property var requirementsData: null
  property bool requirementsChecking: true
  property bool requirementsReady: false
  property bool setupLaunched: false
  property string requirementsOutput: ""
  property string requirementsError: ""
  property bool reopenAfterPanelLoad: false

  property var playlists: []
  property var speedDialItems: []
  property var libraryItems: []
  property var searchItems: []
  property string libraryOutput: ""
  property string searchOutput: ""
  property string apiError: ""
  property bool libraryLoading: false
  property bool searchLoading: false
  property string playbackOutput: ""
  property string playbackError: ""
  property var pendingPlaybackItem: null
  property string selectedPlaybackUrl: ""
  property string controlOutput: ""
  property string controlError: ""
  property string statusOutput: ""
  property string statusError: ""
  property int queueCount: 0
  property int queuePosition: -1
  property bool queueCanGoPrevious: false
  property bool queueCanGoNext: false
  property string loopMode: "none"
  property bool playbackRunning: false
  property bool playbackPlaying: false
  property bool playbackCanTogglePlaying: false
  property real playbackPosition: 0
  property real playbackDuration: 0
  property bool playbackCanSeek: false
  property string playbackTitle: ""
  property string playbackArtist: ""
  property string playbackAlbum: ""
  property string playbackArtUrl: ""

  readonly property string pluginPath:
    Quickshell.env("HOME") + "/.config/omarchy/plugins/fredrik.youtube-music"
  readonly property bool hasMedia:
    playbackTitle !== "" || playbackArtist !== ""
  readonly property string title:
    playbackTitle
  readonly property string artist:
    playbackArtist
  readonly property string album:
    playbackAlbum
  readonly property string artUrl:
    playbackArtUrl
  readonly property bool playing:
    playbackPlaying
  readonly property bool canGoPrevious:
    queueCanGoPrevious
  readonly property bool canGoNext:
    queueCanGoNext
  readonly property bool canTogglePlaying:
    playbackCanTogglePlaying
  readonly property real position:
    playbackPosition
  readonly property real duration:
    playbackDuration
  readonly property bool canSeek:
    playbackCanSeek
  readonly property string playIcon:
    playing ? "󰏤" : "󰐊"
  readonly property color indicatorColor:
    playing ? "#22c55e"
      : playbackRunning ? "#eab308"
      : Color.muted
  readonly property bool indicatorHollow:
    !playbackRunning

  function runMedia(action, value) {
    if (!playbackRunning
        || playbackProcess.running
        || controlProcess.running) return false
    controlOutput = ""
    controlError = ""
    controlProcess.command = [
      "python", pluginPath + "/playback.py", "control", action
    ]
    if (action === "seek") controlProcess.command.push(String(value))
    controlProcess.running = true
    return true
  }

  function applyPlaybackStatus(raw) {
    var payload = JSON.parse(String(raw || "").trim())
    if (!payload || payload.schema !== 1)
      throw new Error("Invalid playback status")
    queueCount = Number(payload.playlistCount || 0)
    queuePosition = Number(
      payload.playlistPosition === undefined ? -1 : payload.playlistPosition
    )
    queueCanGoPrevious = payload.canGoPrevious === true
    queueCanGoNext = payload.canGoNext === true
    playbackRunning = payload.running === true
    playbackPlaying = payload.playing === true
    playbackCanTogglePlaying = payload.canTogglePlaying === true
    playbackPosition = Math.max(0, Number(payload.position || 0))
    playbackDuration = Math.max(0, Number(payload.duration || 0))
    playbackCanSeek = payload.canSeek === true
    playbackTitle = String(payload.title || "")
    playbackArtist = String(payload.artist || "")
    playbackAlbum = String(payload.album || "")
    playbackArtUrl = String(payload.artUrl || "")
    if (String(payload.sourceUrl || "") !== "")
      selectedPlaybackUrl = String(payload.sourceUrl)
    loopMode = ["none", "playlist", "song"].indexOf(payload.loopMode) >= 0
      ? payload.loopMode : "none"
  }

  function refreshPlaybackStatus() {
    if (statusProcess.running
        || playbackProcess.running
        || controlProcess.running) return
    statusOutput = ""
    statusError = ""
    statusProcess.running = true
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
    if ("dependencyData" in target)
      target.dependencyData = requirementsData
    if ("dependencyChecking" in target)
      target.dependencyChecking = requirementsChecking
    if ("setupLaunched" in target)
      target.setupLaunched = setupLaunched
    if ("dependencyError" in target)
      target.dependencyError = requirementsError
    if ("playlists" in target) target.playlists = playlists
    if ("speedDialItems" in target)
      target.speedDialItems = speedDialItems
    if ("libraryItems" in target) target.libraryItems = libraryItems
    if ("searchItems" in target) target.searchItems = searchItems
    if ("libraryLoading" in target) target.libraryLoading = libraryLoading
    if ("searchLoading" in target) target.searchLoading = searchLoading
    if ("playbackLoading" in target)
      target.playbackLoading = playbackProcess.running
        || pendingPlaybackItem !== null
    if ("currentItemUrl" in target)
      target.currentItemUrl = selectedPlaybackUrl
    if ("apiError" in target) target.apiError = apiError
    if ("hasActivePlayer" in target)
      target.hasActivePlayer = playbackRunning
    if ("currentTitle" in target) target.currentTitle = title
    if ("currentArtist" in target) target.currentArtist = artist
    if ("currentAlbum" in target) target.currentAlbum = album
    if ("currentArtUrl" in target) target.currentArtUrl = artUrl
    if ("currentPlaying" in target) target.currentPlaying = playing
    if ("currentCanGoPrevious" in target)
      target.currentCanGoPrevious = canGoPrevious
    if ("currentCanGoNext" in target)
      target.currentCanGoNext = canGoNext
    if ("currentCanTogglePlaying" in target)
      target.currentCanTogglePlaying = canTogglePlaying
    if ("currentPosition" in target)
      target.currentPosition = position
    if ("currentDuration" in target)
      target.currentDuration = duration
    if ("currentCanSeek" in target)
      target.currentCanSeek = canSeek
    if ("currentLoopMode" in target)
      target.currentLoopMode = loopMode
  }

  function applyRequirements(raw) {
    try {
      var payload = JSON.parse(String(raw || "").trim())
      if (!payload || payload.schema !== 1 || !Array.isArray(payload.checks))
        throw new Error("Invalid requirements response")
      var nextReady = payload.ready === true
      if (nextReady !== requirementsReady)
        reopenAfterPanelLoad = panelLoader.item
          && panelLoader.item.opened === true
      requirementsData = payload
      requirementsReady = nextReady
      requirementsError = ""
      if (nextReady) {
        setupLaunched = false
        if (!browserProcess.running) browserProcess.running = true
        if (playlists.length === 0 && libraryItems.length === 0)
          refreshLibrary()
      }
    } catch (error) {
      requirementsReady = false
      requirementsError = "Could not check YouTube Music setup"
    }
    requirementsChecking = false
    syncPanel()
  }

  function checkRequirements() {
    if (requirementsProcess.running) return
    requirementsChecking = true
    requirementsOutput = ""
    requirementsError = ""
    syncPanel()
    requirementsProcess.running = true
  }

  function launchSetup() {
    if (setupLauncher.running) return
    setupLaunched = true
    requirementsError = ""
    setupLauncher.running = true
    syncPanel()
  }

  function refreshLibrary() {
    if (!requirementsReady || libraryProcess.running) return
    libraryLoading = true
    libraryOutput = ""
    apiError = ""
    libraryProcess.running = true
    syncPanel()
  }

  function search(query) {
    var value = String(query || "").trim()
    if (!requirementsReady || searchProcess.running || value === "") return
    searchLoading = true
    searchOutput = ""
    apiError = ""
    searchProcess.command = [
      "python", pluginPath + "/ytmusic_bridge.py", "search", value
    ]
    searchProcess.running = true
    syncPanel()
  }

  function playItem(item) {
    if (!item || !item.url) {
      apiError = "This item is not directly playable"
      syncPanel()
      return false
    }
    apiError = ""
    if (playbackProcess.running) {
      pendingPlaybackItem = item
      playbackProcess.running = false
      syncPanel()
      return true
    }
    startPlayback(item)
    return true
  }

  function startPlayback(item) {
    playbackOutput = ""
    playbackError = ""
    playbackPlaying = false
    playbackCanTogglePlaying = false
    playbackPosition = 0
    playbackDuration = 0
    playbackCanSeek = false
    playbackTitle = String(item.title || "")
    playbackArtist = String(item.subtitle || "")
    playbackAlbum = String(item.metadata || "")
    playbackArtUrl = String(item.thumbnail || "")
    playbackProcess.command = [
      "python", pluginPath + "/playback.py", "play",
      String(item.type || ""), String(item.url)
    ]
    playbackProcess.running = true
    syncPanel()
  }

  function parseLibrary(raw) {
    var payload = JSON.parse(String(raw || "").trim())
    if (!payload || payload.schema !== 1
        || !Array.isArray(payload.speedDial)
        || !Array.isArray(payload.playlists)
        || !Array.isArray(payload.library))
      throw new Error("Invalid library response")
    speedDialItems = payload.speedDial
    playlists = payload.playlists
    libraryItems = payload.library
  }

  function parseSearch(raw) {
    var payload = JSON.parse(String(raw || "").trim())
    if (!payload || payload.schema !== 1 || !Array.isArray(payload.items))
      throw new Error("Invalid search response")
    searchItems = payload.items
  }

  function togglePanel() {
    if (!requirementsReady) checkRequirements()
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

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  onPlaylistsChanged: syncPanel()
  onSpeedDialItemsChanged: syncPanel()
  onLibraryItemsChanged: syncPanel()
  onSearchItemsChanged: syncPanel()
  onLibraryLoadingChanged: syncPanel()
  onSearchLoadingChanged: syncPanel()
  onApiErrorChanged: syncPanel()
  onTitleChanged: syncPanel()
  onArtistChanged: syncPanel()
  onAlbumChanged: syncPanel()
  onArtUrlChanged: syncPanel()
  onPlayingChanged: syncPanel()
  onCanGoPreviousChanged: syncPanel()
  onCanGoNextChanged: syncPanel()
  onCanTogglePlayingChanged: syncPanel()
  onPositionChanged: syncPanel()
  onDurationChanged: syncPanel()
  onCanSeekChanged: syncPanel()
  onLoopModeChanged: syncPanel()
  onPlaybackRunningChanged: syncPanel()

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

  Process {
    id: browserProcess
    command: [
      "node", root.pluginPath + "/browser_server.mjs"
    ]
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.statusError = String(text || "").trim()
    }
    onExited: function(exitCode) {
      if (!root.requirementsReady) return
      if (exitCode !== 0)
        root.apiError = root.statusError || "The browser player stopped"
      browserRestart.restart()
      root.syncPanel()
    }
  }

  Process {
    id: requirementsProcess
    command: [
      "python", root.pluginPath + "/ytmusic_bridge.py", "check"
    ]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.requirementsOutput = String(text || "")
    }
    onExited: function(exitCode) {
      if (exitCode === 0) root.applyRequirements(root.requirementsOutput)
      else {
        root.requirementsChecking = false
        root.requirementsError = "Could not check YouTube Music setup"
        root.syncPanel()
      }
    }
  }

  Process {
    id: setupLauncher
    command: [
      "omarchy", "launch", "floating", "terminal", "with", "presentation",
      "bash", root.pluginPath + "/setup.sh"
    ]
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.setupLaunched = false
        root.requirementsError = "Could not open the setup terminal"
      }
      setupRetry.restart()
      root.syncPanel()
    }
  }

  Process {
    id: libraryProcess
    command: [
      "python", root.pluginPath + "/ytmusic_bridge.py", "library"
    ]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.libraryOutput = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.apiError = String(text || "").trim()
    }
    onExited: function(exitCode) {
      root.libraryLoading = false
      if (exitCode === 0) {
        try {
          root.parseLibrary(root.libraryOutput)
          root.apiError = ""
        } catch (error) {
          root.apiError = "YouTube Music returned invalid library data"
        }
      } else if (root.apiError === "") {
        root.apiError = "Could not load your YouTube Music library"
      }
      root.syncPanel()
      if (exitCode !== 0) root.checkRequirements()
    }
  }

  Process {
    id: searchProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.searchOutput = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.apiError = String(text || "").trim()
    }
    onExited: function(exitCode) {
      root.searchLoading = false
      if (exitCode === 0) {
        try {
          root.parseSearch(root.searchOutput)
          root.apiError = ""
        } catch (error) {
          root.apiError = "YouTube Music returned invalid search data"
        }
      } else if (root.apiError === "") {
        root.apiError = "Could not search YouTube Music"
      }
      root.syncPanel()
      if (exitCode !== 0) root.checkRequirements()
    }
  }

  Process {
    id: playbackProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.playbackOutput = String(text || "").trim()
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.playbackError = String(text || "").trim()
    }
    onExited: function(exitCode) {
      if (root.pendingPlaybackItem !== null) {
        var nextItem = root.pendingPlaybackItem
        root.pendingPlaybackItem = null
        Qt.callLater(function() { root.startPlayback(nextItem) })
        return
      }
      if (exitCode === 0) {
        try {
          root.applyPlaybackStatus(root.playbackOutput)
          root.apiError = ""
        } catch (error) {
          root.apiError = "Playback started but returned invalid queue data"
        }
      } else {
        root.apiError = root.playbackError
          || "Could not start local YouTube Music playback"
      }
      root.syncPanel()
    }
  }

  Process {
    id: controlProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.controlOutput = String(text || "").trim()
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.controlError = String(text || "").trim()
    }
    onExited: function(exitCode) {
      if (exitCode === 0) {
        try {
          root.applyPlaybackStatus(root.controlOutput)
          root.apiError = ""
        } catch (error) {
          root.apiError = "The player returned invalid control data"
        }
      } else {
        root.apiError = root.controlError || "Could not control local playback"
      }
      root.syncPanel()
    }
  }

  Process {
    id: statusProcess
    command: [
      "python", root.pluginPath + "/playback.py", "status"
    ]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.statusOutput = String(text || "").trim()
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.statusError = String(text || "").trim()
    }
    onExited: function(exitCode) {
      if (playbackProcess.running || controlProcess.running)
        return
      if (exitCode !== 0) {
        root.apiError = root.statusError || "Could not read playback status"
        root.syncPanel()
        return
      }
      try {
        root.applyPlaybackStatus(root.statusOutput)
        root.syncPanel()
      } catch (error) {
        root.apiError = "The player returned invalid status data"
        root.syncPanel()
      }
    }
  }

  Timer {
    interval: 1000
    running: root.opened || root.playbackRunning
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refreshPlaybackStatus()
  }

  Timer {
    id: browserRestart
    interval: 2000
    repeat: false
    onTriggered: {
      root.statusError = ""
      if (root.requirementsReady && !browserProcess.running)
        browserProcess.running = true
    }
  }

  Timer {
    interval: root.setupLaunched ? 2000 : 15000
    running: !root.requirementsReady
    repeat: true
    triggeredOnStart: true
    onTriggered: root.checkRequirements()
  }

  Timer {
    id: setupRetry
    interval: 2500
    repeat: false
    onTriggered: root.checkRequirements()
  }

  Component {
    id: youtubeMusicStatusIcon

    Item {
      anchors.fill: parent

      Text {
        anchors.centerIn: parent
        text: "󰝚"
        color: root.bar ? root.bar.foreground : Color.foreground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.icon
      }

      Rectangle {
        width: Math.max(5, parent.width * 0.3)
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
    iconComponent: youtubeMusicStatusIcon
    slotSize: Style.bar.iconSlot
    tooltipText: !root.requirementsReady
      ? "YouTube Music setup required"
      : root.playing ? "YouTube Music · Playing"
      : root.playbackRunning ? "YouTube Music · Paused"
      : "YouTube Music · Idle"
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton) root.runMedia("next")
      else if (mouseButton === Qt.RightButton) root.runMedia("playPause")
      else root.togglePanel()
    }
  }
}
