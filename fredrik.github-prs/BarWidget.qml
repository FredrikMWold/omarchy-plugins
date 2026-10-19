import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "PullRequests.js" as PullRequests
import "Repositories.js" as Repositories

BarWidget {
  id: root
  moduleName: "fredrik.github-prs"
  visible: cacheLoaded

  property var pullRequests: []
  property var refreshResults: []
  property var queryQueue: []
  property var activeQuery: null
  property string processOutput: ""
  property string processError: ""
  property string errorMessage: ""
  property bool loading: false
  property bool refreshQueued: false
  property bool refreshFailed: false
  property bool cacheLoaded: false
  property bool hasCompletedRefresh: false
  property var repositorySettings: null
  property var pendingRepositorySettings: null
  property bool repositorySettingsLoaded: false
  property bool savingRepositories: false
  property string repositorySettingsError: ""
  property int repositoryRevision: 0
  property int refreshRepositoryRevision: 0

  readonly property var defaultMyRepositories: [
    "equinor/prisma-decision-web",
    "equinor/subsurface-portal-web",
    "equinor/pressure-db-ingestion-web",
    "equinor/design-system",
    "equinor/warp-ui"
  ]
  readonly property var defaultReviewRepositories: [
    "equinor/prisma-decision-web",
    "equinor/subsurface-portal-web",
    "equinor/pressure-db-ingestion-web",
    "equinor/warp-ui"
  ]
  readonly property string defaultFilter: "-label:dependencies -label:\"autorelease: pending\""
  readonly property string cacheHome: Quickshell.env("XDG_CACHE_HOME") || Quickshell.env("HOME") + "/.cache"
  readonly property string cachePath: cacheHome + "/fredrik-github-prs.json"
  readonly property string configHome: Quickshell.env("XDG_CONFIG_HOME") || Quickshell.env("HOME") + "/.config"
  readonly property string repositorySettingsPath: configHome + "/omarchy/github-prs-repositories.json"
  readonly property int myCount: countForType("my")
  readonly property int reviewCount: countForType("review")
  readonly property bool showLoading: loading && cacheLoaded && pullRequests.length === 0
  readonly property int refreshMinutes: Math.max(1, parseInt(setting("refreshMinutes", 5), 10) || 5)

  function repositories(name, fallback) {
    if (repositorySettings) return repositorySettings[name]
    var configured = setting(name, fallback)
    return configured && configured.length !== undefined ? configured : fallback
  }

  function applyRepositorySettings(value) {
    var changed = JSON.stringify(repositorySettings) !== JSON.stringify(value)
    repositorySettings = value
    repositorySettingsLoaded = true
    if (changed || refreshQueued) {
      repositoriesChanged()
    }
  }

  function loadRepositorySettings(raw) {
    try {
      var value = Repositories.parseSettings(raw)
      repositorySettingsError = ""
      applyRepositorySettings(value)
    } catch (error) {
      repositorySettingsError = "Could not read repository settings: " + String(error.message || error)
      repositorySettingsLoaded = true
      refresh()
    }
  }

  function repositoriesChanged() {
    repositoryRevision++
    pullRequests = Repositories.filterPullRequests(pullRequests,
      repositories("myRepositories", defaultMyRepositories),
      repositories("reviewRepositories", defaultReviewRepositories))
    refresh()
  }

  function saveRepositories(my, review) {
    if (savingRepositories) return
    try {
      pendingRepositorySettings = Repositories.selections(my, review)
      repositorySettingsError = ""
      savingRepositories = true
      repositorySettingsFile.setText(JSON.stringify(pendingRepositorySettings, null, 2) + "\n")
    } catch (error) {
      savingRepositories = false
      pendingRepositorySettings = null
      repositorySettingsError = "Could not save repositories: " + String(error.message || error)
    }
  }

  function countForType(type) {
    var count = 0
    for (var i = 0; i < pullRequests.length; i++) {
      if (pullRequests[i].type === type) count++
    }
    return count
  }

  function enqueue(repositories, type) {
    for (var i = 0; i < repositories.length; i++) {
      var repository = String(repositories[i] || "").trim()
      if (repository !== "") queryQueue.push({ repository: repository, type: type })
    }
  }

  function loadCache(raw) {
    if (cacheLoaded) return

    try {
      var text = String(raw || "").trim()
      var payload = text === "" ? null : JSON.parse(text)
      if (!payload || payload.version !== 1 || !Array.isArray(payload.pullRequests)) {
        cacheLoaded = true
        return
      }

      var cached = []
      for (var i = 0; i < payload.pullRequests.length; i++) {
        var item = payload.pullRequests[i]
        if (!item || (item.type !== "my" && item.type !== "review")) continue
        if (!item.title || !item.url || !item.repositoryName || item.number === undefined) continue
        cached.push(item)
      }

      if (!hasCompletedRefresh) {
        pullRequests = repositorySettingsLoaded
          ? Repositories.filterPullRequests(cached,
            repositories("myRepositories", defaultMyRepositories),
            repositories("reviewRepositories", defaultReviewRepositories))
          : cached
      }
    } catch (error) {
      console.warn("github-prs: cache parse failed:", error)
    }
    cacheLoaded = true
  }

  function persistCache() {
    var payload = {
      version: 1,
      updatedAt: new Date().toISOString(),
      pullRequests: pullRequests
    }
    cacheFile.setText(JSON.stringify(payload, null, 2) + "\n")
  }

  function refresh() {
    if (!repositorySettingsLoaded || loading) {
      refreshQueued = true
      return
    }

    refreshQueued = false
    refreshRepositoryRevision = repositoryRevision
    refreshResults = []
    queryQueue = []
    errorMessage = ""
    refreshFailed = false
    loading = true
    enqueue(repositories("myRepositories", defaultMyRepositories), "my")
    enqueue(repositories("reviewRepositories", defaultReviewRepositories), "review")
    runNextQuery()
  }

  function runNextQuery() {
    if (refreshRepositoryRevision !== repositoryRevision) {
      loading = false
      refresh()
      return
    }
    if (queryQueue.length === 0) {
      if (!refreshFailed || pullRequests.length === 0) {
        pullRequests = refreshResults
      }
      if (!refreshFailed) persistCache()
      hasCompletedRefresh = true
      loading = false
      syncPanel()
      if (refreshQueued) {
        refreshQueued = false
        Qt.callLater(refresh)
      }
      return
    }

    activeQuery = queryQueue.shift()
    processOutput = ""
    processError = ""
    var authorFilter = activeQuery.type === "my" ? "author:@me" : "-author:@me"
    var search = String(setting("filter", defaultFilter)).trim()
    if (search !== "") search += " "
    search += authorFilter + " is:open is:pr repo:" + activeQuery.repository
    fetchProcess.command = [
      "gh", "api", "graphql",
      "-f", "query=" + PullRequests.query(),
      "-f", "search=" + search
    ]
    fetchProcess.running = true
  }

  function completeQuery(exitCode) {
    if (refreshRepositoryRevision !== repositoryRevision) {
      loading = false
      refresh()
      return
    }
    if (exitCode === 0) {
      try {
        var response = PullRequests.parseResponse(processOutput)
        var next = refreshResults.slice()
        for (var i = 0; i < response.length; i++) {
          var item = response[i]
          item.repository = activeQuery.repository
          item.repositoryName = activeQuery.repository.split("/").pop()
          item.type = activeQuery.type
          next.push(item)
        }
        refreshResults = next
      } catch (error) {
        refreshFailed = true
        errorMessage = String(error.message || error)
      }
    } else if (errorMessage === "") {
      refreshFailed = true
      errorMessage = processError.trim() || "Could not load GitHub pull requests"
    } else {
      refreshFailed = true
    }

    syncPanel()
    Qt.callLater(runNextQuery)
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
    target.pullRequests = root.pullRequests
    target.loading = root.loading
    target.cacheReady = root.cacheLoaded
    target.errorMessage = root.repositorySettingsError || root.errorMessage
    target.repositorySaveError = root.repositorySettingsError
    target.savingRepositories = root.savingRepositories
    target.myCount = root.myCount
    target.reviewCount = root.reviewCount
  }

  function togglePanel() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: {
    injectPanel()
    refreshTimer.restart()
    repositoriesChanged()
  }
  onPullRequestsChanged: syncPanel()
  onLoadingChanged: syncPanel()
  onCacheLoadedChanged: syncPanel()
  onErrorMessageChanged: syncPanel()
  onRepositorySettingsErrorChanged: syncPanel()
  onSavingRepositoriesChanged: syncPanel()

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

  FileView {
    id: repositorySettingsFile
    path: root.repositorySettingsPath
    watchChanges: true
    atomicWrites: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.loadRepositorySettings(text())
    onLoadFailed: function(error) {
      if (error === FileViewError.FileNotFound) {
        root.repositorySettingsError = ""
        root.applyRepositorySettings(null)
      } else {
        root.repositorySettingsLoaded = true
        root.repositorySettingsError = "Could not read repository settings: " + FileViewError.toString(error)
        root.refresh()
      }
    }
    onSaved: {
      root.applyRepositorySettings(root.pendingRepositorySettings)
      root.pendingRepositorySettings = null
      root.savingRepositories = false
      if (panelLoader.item) panelLoader.item.finishRepositorySave()
    }
    onSaveFailed: function(error) {
      root.savingRepositories = false
      root.pendingRepositorySettings = null
      root.repositorySettingsError = "Could not save repositories: " + FileViewError.toString(error)
    }
  }

  FileView {
    id: cacheFile
    path: root.cachePath
    watchChanges: false
    atomicWrites: true
    printErrors: false
    onLoaded: root.loadCache(text())
    onLoadFailed: root.loadCache("")
  }

  Process {
    id: fetchProcess

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.processOutput = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.processError = String(text || "")
    }
    onExited: function(exitCode) { root.completeQuery(exitCode) }
  }

  Timer {
    id: refreshTimer
    interval: root.refreshMinutes * 60000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.showLoading
      ? " loading"
      : " my " + root.myCount + " | review " + root.reviewCount
    tooltipText: root.repositorySettingsError || root.errorMessage || "GitHub pull requests"
    fontSize: Style.font.baseSize

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton || mouseButton === Qt.RightButton) root.refresh()
      else root.togglePanel()
    }
  }
}