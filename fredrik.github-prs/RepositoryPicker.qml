import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Repositories.js" as Repositories

Column {
  id: root

  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property var myRepositories: []
  property var reviewRepositories: []
  property var discoveredRepositories: []
  property string persistenceError: ""
  property bool saving: false
  property string searchError: ""
  property string searchOutput: ""
  property string searchStderr: ""
  property string activeSearch: ""
  property var pendingSearch: null
  property bool moreResults: false
  property bool searched: false

  readonly property var repositoryRows:
    Repositories.rows(myRepositories, reviewRepositories, discoveredRepositories)
  readonly property bool searching: discoveryProcess.running
  property alias focusItem: searchField

  signal saveRequested(var myRepositories, var reviewRepositories)
  signal cancelled()

  spacing: Style.space(6)
  Keys.onEscapePressed: root.cancelled()

  function begin(my, review) {
    myRepositories = Repositories.names(my)
    reviewRepositories = Repositories.names(review)
    discoveredRepositories = []
    searchField.text = ""
    searchError = ""
    searched = false
    search()
    Qt.callLater(function() { searchField.forceActiveFocus() })
  }

  function search() {
    var query = searchField.text.trim()
    if (discoveryProcess.running) {
      pendingSearch = query
      return
    }
    startSearch(query)
  }

  function startSearch(query) {
    activeSearch = query
    discoveredRepositories = []
    searched = false
    moreResults = false
    searchOutput = ""
    searchStderr = ""
    searchError = ""
    discoveryProcess.command = Repositories.discoveryCommand(activeSearch)
    discoveryProcess.running = true
  }

  function completeSearch(exitCode) {
    if (pendingSearch !== null) {
      var query = pendingSearch
      pendingSearch = null
      startSearch(query)
      return
    }
    if (exitCode !== 0) {
      searchError = searchStderr.trim() || "Could not load GitHub repositories"
      return
    }
    try {
      var response = Repositories.parseDiscovery(searchOutput, activeSearch)
      discoveredRepositories = response.repositories
      moreResults = response.more
      searched = true
    } catch (error) {
      searchError = String(error.message || error)
    }
  }

  Text {
    width: parent.width
    text: "Choose repositories separately for your PRs and PRs to review."
    wrapMode: Text.Wrap
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  Row {
    width: parent.width
    spacing: Style.space(6)

    TextField {
      id: searchField
      objectName: "repositorySearch"
      width: parent.width - searchButton.width - parent.spacing
      enabled: !root.saving
      placeholderText: "Search GitHub or enter owner/repository"
      foreground: root.foreground
      font.family: root.fontFamily
      onAccepted: root.search()
    }

    Button {
      id: searchButton
      anchors.verticalCenter: parent.verticalCenter
      text: "Search"
      iconText: root.searching ? "󰦖" : ""
      iconSpinning: root.searching
      enabled: !root.searching && !root.saving
      focusable: true
      foreground: root.foreground
      fontFamily: root.fontFamily
      onClicked: root.search()
    }
  }

  Text {
    width: parent.width
    text: root.myRepositories.length + " yours · " + root.reviewRepositories.length
      + " to review · selected repositories stay in the list"
    wrapMode: Text.Wrap
    color: Color.muted
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
  }

  Text {
    visible: root.persistenceError !== "" || root.searchError !== ""
    width: parent.width
    text: [root.persistenceError, root.searchError].filter(function(text) {
      return text !== ""
    }).join("\n")
    textFormat: Text.PlainText
    color: Color.urgent
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.Wrap
  }

  Text {
    visible: root.searching || root.searched
    width: parent.width
    text: root.searching ? "Loading repositories…"
      : root.discoveredRepositories.length === 0 ? "No repositories found. Try another search."
      : root.activeSearch === "" ? "Recently updated repositories you have access to."
      : "Search results and your selected repositories."
    color: Color.muted
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
  }

  ListView {
    id: repositoryList
    objectName: "repositoryList"
    width: parent.width
    height: Math.min(contentHeight, Style.space(220))
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    model: root.repositoryRows
    spacing: Style.space(2)
    enabled: !root.saving

    ScrollBar.vertical: ScrollBar {
      policy: ScrollBar.AsNeeded
    }

    delegate: Row {
      id: repositoryRow
      required property string modelData
      required property int index
      width: ListView.view.width
      spacing: Style.space(4)

      Text {
        width: Math.max(0, parent.width - mineButton.width - reviewButton.width
          - parent.spacing * 2 - Style.space(10))
        anchors.verticalCenter: parent.verticalCenter
        text: repositoryRow.modelData
        textFormat: Text.PlainText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideMiddle
      }

      Button {
        id: mineButton
        width: Style.space(72)
        text: "Yours"
        iconText: selected ? "✓" : ""
        selected: Repositories.includes(root.myRepositories, repositoryRow.modelData)
        bordered: true
        focusable: true
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        verticalPadding: Style.space(4)
        onActiveFocusChanged: {
          if (activeFocus) repositoryList.positionViewAtIndex(repositoryRow.index, ListView.Contain)
        }
        onClicked: root.myRepositories = Repositories.toggle(root.myRepositories, repositoryRow.modelData)
      }

      Button {
        id: reviewButton
        width: Style.space(84)
        text: "Review"
        iconText: selected ? "✓" : ""
        selected: Repositories.includes(root.reviewRepositories, repositoryRow.modelData)
        bordered: true
        focusable: true
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        verticalPadding: Style.space(4)
        onActiveFocusChanged: {
          if (activeFocus) repositoryList.positionViewAtIndex(repositoryRow.index, ListView.Contain)
        }
        onClicked: root.reviewRepositories = Repositories.toggle(root.reviewRepositories, repositoryRow.modelData)
      }
    }
  }

  Text {
    visible: root.moreResults && !root.searching
    width: parent.width
    text: "More repositories are available. Narrow your search or enter owner/repository."
    color: Color.muted
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
  }

  Row {
    width: parent.width
    spacing: Style.space(8)

    Button {
      text: root.saving ? "Saving…" : "Save"
      enabled: !root.saving
      bordered: true
      focusable: true
      foreground: Color.accent
      fontFamily: root.fontFamily
      onClicked: root.saveRequested(root.myRepositories, root.reviewRepositories)
    }

    Button {
      text: "Cancel"
      enabled: !root.saving
      focusable: true
      foreground: root.foreground
      fontFamily: root.fontFamily
      onClicked: root.cancelled()
    }
  }

  Process {
    id: discoveryProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.searchOutput = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.searchStderr = String(text || "")
    }
    onExited: function(exitCode) { root.completeSearch(exitCode) }
  }
}
