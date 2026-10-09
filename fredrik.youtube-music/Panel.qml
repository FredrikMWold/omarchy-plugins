import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "fredrik.youtube-music"
  ipcTarget: "fredrik.youtube-music"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var speedDialItems: []
  property var playlists: []
  property var libraryItems: []
  property var searchItems: []
  property bool libraryLoading: false
  property bool searchLoading: false
  property bool playbackLoading: false
  property string apiError: ""
  property bool hasActivePlayer: false
  property string currentTitle: ""
  property string currentArtist: ""
  property string currentAlbum: ""
  property string currentArtUrl: ""
  property bool currentPlaying: false
  property bool currentCanGoPrevious: false
  property bool currentCanGoNext: false
  property bool currentCanTogglePlaying: false
  property real currentPosition: 0
  property real currentDuration: 0
  property bool currentCanSeek: false
  property real smoothPosition: 0
  property string currentLoopMode: "none"
  property string currentItemUrl: ""
  property string activeTab: "now"
  property int selectedIndex: -1

  readonly property var barIdentity: hostWidget || root
  readonly property var displayedItems:
    activeTab === "speedDial" ? speedDialItems
      : activeTab === "playlists" ? playlists
      : activeTab === "library" ? libraryItems
      : activeTab === "search" ? searchItems : []

  function open() {
    controller.show()
    if (hostWidget && playlists.length === 0 && libraryItems.length === 0)
      hostWidget.refreshLibrary()
  }

  function switchTab(tab) {
    activeTab = tab
    selectedIndex = -1
    if ((tab === "speedDial" || tab === "playlists" || tab === "library")
        && hostWidget && speedDialItems.length === 0
        && playlists.length === 0 && libraryItems.length === 0)
      hostWidget.refreshLibrary()
  }

  function moveSelection(delta) {
    if (displayedItems.length === 0) return
    selectedIndex = Math.max(
      0,
      Math.min(displayedItems.length - 1, selectedIndex + delta)
    )
    Qt.callLater(function() {
      resultList.positionViewAtIndex(selectedIndex, ListView.Contain)
    })
  }

  function activateSelection() {
    if (selectedIndex < 0 || selectedIndex >= displayedItems.length) return
    openItem(displayedItems[selectedIndex])
  }

  function openItem(item) {
    if (!hostWidget || !hostWidget.playItem(item))
      apiError = "This item is not directly playable"
  }

  function runMedia(action) {
    if (hostWidget) hostWidget.runMedia(action)
  }

  function seekTo(position) {
    if (!hostWidget) return
    currentPosition = position
    smoothPosition = position
    hostWidget.runMedia("seek", position)
  }

  function formatTime(seconds) {
    var value = Math.max(0, Math.floor(Number(seconds) || 0))
    var minutes = Math.floor(value / 60)
    var remainder = value % 60
    return minutes + ":" + (remainder < 10 ? "0" : "") + remainder
  }

  onDisplayedItemsChanged: {
    selectedIndex = displayedItems.length > 0
      ? Math.min(selectedIndex, displayedItems.length - 1) : -1
  }
  onCurrentPositionChanged: smoothPosition = currentPosition
  onCurrentDurationChanged:
    smoothPosition = Math.min(smoothPosition, currentDuration)

  Timer {
    interval: 50
    running: root.currentPlaying
      && root.currentCanSeek
      && root.currentDuration > 0
    repeat: true
    onTriggered: {
      root.smoothPosition = Math.min(
        root.currentDuration,
        root.smoothPosition + interval / 1000
      )
    }
  }

  IpcHandler {
    target: "fredrik.youtube-music"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function status(): string {
      return JSON.stringify({
        hasActivePlayer: root.hasActivePlayer,
        title: root.currentTitle,
        artist: root.currentArtist,
        album: root.currentAlbum,
        artUrl: root.currentArtUrl,
        playing: root.currentPlaying,
        canTogglePlaying: root.currentCanTogglePlaying
      })
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(560))
    contentHeight: panel.fittedContentHeight(
      contentColumn.implicitHeight,
      Style.space(560)
    )

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: searchField.activeFocus
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveSelection(dy)
      }
      onActivateRequested: root.activateSelection()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") {
          if (root.hostWidget) root.hostWidget.refreshLibrary()
        }
      }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(10)

        Row {
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width - refreshButton.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            text: "YouTube Music"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }

          Button {
            id: refreshButton
            iconText: root.libraryLoading ? "󰦖" : ""
            iconSpinning: root.libraryLoading
            tooltipText: "Refresh library"
            foreground: root.bar.foreground
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.space(4)
            onClicked: if (root.hostWidget) root.hostWidget.refreshLibrary()
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(6)

          Repeater {
            model: [
              { id: "now", label: "Now playing" },
              { id: "speedDial", label: "Speed dial" },
              { id: "playlists", label: "Playlists" },
              { id: "library", label: "Library" },
              { id: "search", label: "Search" }
            ]

            Button {
              required property var modelData
              text: modelData.label
              foreground: root.bar.foreground
              active: root.activeTab === modelData.id
              horizontalPadding: Style.space(10)
              verticalPadding: Style.space(5)
              onClicked: root.switchTab(modelData.id)
            }
          }
        }

        PanelSeparator {
          foreground: root.bar.foreground
        }

        Column {
          visible: root.activeTab === "now"
          width: parent.width
          spacing: Style.space(12)

          Row {
            width: parent.width
            spacing: Style.space(12)

            BorderSurface {
              width: Style.space(80)
              height: width
              radius: Style.cornerRadius
              color: Style.normalFillFor(root.bar.foreground, Color.accent)
              borderSpec: Border.controlSpec(
                "normal", root.bar.foreground, Color.accent
              )

              Image {
                anchors.fill: parent
                anchors.margins: Style.space(2)
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                source: root.currentArtUrl
                visible: source !== ""
              }

              Text {
                anchors.centerIn: parent
                visible: !parent.children[0].visible
                text: "󰝚"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.displayLarge
              }
            }

            Column {
              width: parent.width - Style.space(92)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(3)

              Text {
                width: parent.width
                text: root.currentTitle || "Nothing playing"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.subtitle
                font.bold: true
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: root.currentArtist
                color: Qt.darker(root.bar.foreground, 1.3)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: root.currentAlbum
                color: Qt.darker(root.bar.foreground, 1.5)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(8)

            Button {
              iconText: "󰒮"
              foreground: root.bar.foreground
              enabled: root.currentCanGoPrevious
              tooltipText: "Previous song"
              onClicked: root.runMedia("previous")
            }
            Button {
              iconText: "󰑐"
              text: "Restart"
              foreground: root.bar.foreground
              enabled: root.hasActivePlayer
              tooltipText: "Start song from beginning"
              onClicked: root.runMedia("restart")
            }
            Button {
              iconText: root.currentPlaying ? "󰏤" : "󰐊"
              foreground: root.bar.foreground
              enabled: root.currentCanTogglePlaying
              tooltipText: root.currentPlaying ? "Pause" : "Play"
              onClicked: root.runMedia("playPause")
            }
            Button {
              iconText: "󰒭"
              foreground: root.bar.foreground
              enabled: root.currentCanGoNext
              tooltipText: "Next song"
              onClicked: root.runMedia("next")
            }
            Button {
              iconText: root.currentLoopMode === "song" ? "󰑘" : "󰑖"
              text: root.currentLoopMode === "none"
                ? "Loop: Off"
                : root.currentLoopMode === "playlist"
                  ? "Loop: Playlist"
                  : "Loop: Song"
              foreground: root.bar.foreground
              enabled: root.hasActivePlayer
              active: root.currentLoopMode !== "none"
              tooltipText: root.currentLoopMode === "none"
                ? "Loop off · click for playlist"
                : root.currentLoopMode === "playlist"
                  ? "Loop playlist · click for song"
                  : "Loop song · click to turn off"
              onClicked: root.runMedia("cycle-loop")
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Text {
              width: Style.space(38)
              anchors.verticalCenter: parent.verticalCenter
              text: root.formatTime(root.smoothPosition)
              color: Qt.darker(root.bar.foreground, 1.3)
              horizontalAlignment: Text.AlignRight
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }

            PanelSlider {
              width: parent.width - Style.space(92)
              anchors.verticalCenter: parent.verticalCenter
              bar: root.bar
              minimum: 0
              maximum: Math.max(1, root.currentDuration)
              value: Math.min(root.smoothPosition, maximum)
              step: 5
              enabled: root.currentCanSeek && root.currentDuration > 0
              opacity: enabled ? 1 : 0.45
              onReleased: function(value) { root.seekTo(value) }
            }

            Text {
              width: Style.space(38)
              anchors.verticalCenter: parent.verticalCenter
              text: root.formatTime(root.currentDuration)
              color: Qt.darker(root.bar.foreground, 1.3)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Text {
            visible: !root.hasActivePlayer
            width: parent.width
            text: "Choose a song, album, or playlist from Speed dial, Playlists, Library, or Search. Playback stays in a hidden YouTube Music window."
            color: Qt.darker(root.bar.foreground, 1.35)
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }

        TextField {
          id: searchField
          visible: root.activeTab === "search"
          width: parent.width
          placeholderText: "Search songs, albums, artists, and playlists"
          foreground: root.bar.foreground
          onAccepted: {
            if (root.hostWidget) root.hostWidget.search(text)
          }
        }

        Text {
          visible: root.apiError !== ""
          width: parent.width
          text: root.apiError
          color: Color.urgent
          wrapMode: Text.WordWrap
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          visible: root.activeTab !== "now"
            && (root.libraryLoading || root.searchLoading)
          width: parent.width
          text: root.searchLoading ? "Searching…" : "Loading your library…"
          color: Color.accent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          visible: root.activeTab !== "now"
            && !root.libraryLoading && !root.searchLoading
            && !root.playbackLoading
            && root.displayedItems.length === 0
            && root.apiError === ""
          width: parent.width
          text: root.activeTab === "search"
            ? "Enter a search above"
            : root.activeTab === "speedDial"
              ? "No personalized Speed dial items found"
              : "No items found"
          color: Qt.darker(root.bar.foreground, 1.35)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        ListView {
          id: resultList
          visible: root.activeTab !== "now"
            && root.displayedItems.length > 0
          width: parent.width
          height: Math.min(contentHeight, Style.space(370))
          clip: true
          spacing: Style.space(2)
          boundsBehavior: Flickable.StopAtBounds
          model: root.displayedItems
          currentIndex: root.selectedIndex
          ScrollBar.vertical: ScrollBar {
            policy: ScrollBar.AsNeeded
          }

          delegate: Rectangle {
            id: row
            required property var modelData
            required property int index
            readonly property bool isCurrentItem:
              root.currentItemUrl !== ""
                && String(modelData.url || "") === root.currentItemUrl
            width: ListView.view.width
            height: Style.space(54)
            radius: Style.cornerRadius
            color: row.isCurrentItem
              ? Style.selectedFillFor(root.bar.foreground, Color.accent)
              : index === root.selectedIndex || rowMouse.containsMouse
                ? Style.hoverFillFor(root.bar.foreground, Color.accent)
              : "transparent"

            Image {
              id: artwork
              anchors.left: parent.left
              anchors.leftMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(42)
              height: width
              source: row.modelData.thumbnail || ""
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
              visible: source !== ""
            }

            Text {
              visible: !artwork.visible
              anchors.centerIn: artwork
              text: row.modelData.type === "artist" ? "" : "󰝚"
              color: Color.accent
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.iconLarge
            }

            Column {
              anchors.left: artwork.right
              anchors.leftMargin: Style.space(10)
              anchors.right: currentIndicator.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter

              Text {
                width: parent.width
                text: row.modelData.title
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: row.isCurrentItem || row.index === root.selectedIndex
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: [
                  row.modelData.section,
                  row.modelData.subtitle,
                  row.modelData.metadata
                ].filter(function(value) { return value !== "" }).join(" · ")
                color: Qt.darker(root.bar.foreground, 1.35)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            Text {
              id: currentIndicator
              width: row.isCurrentItem ? Style.space(24) : 0
              anchors.right: parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              visible: row.isCurrentItem
              text: root.currentPlaying ? "󰐊" : "󰏤"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.icon
            }

            MouseArea {
              id: rowMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                root.selectedIndex = row.index
                root.openItem(row.modelData)
              }
            }
          }
        }
      }
    }
  }
}
