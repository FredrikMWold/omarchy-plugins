import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "fredrik.github-prs"
  ipcTarget: "fredrik.github-prs"

  property var anchorItem: null
  property var hostWidget: null
  property var pullRequests: []
  property bool loading: false
  property bool cacheReady: false
  property string errorMessage: ""
  property int myCount: 0
  property int reviewCount: 0
  property int selectedIndex: -1

  readonly property var barIdentity: hostWidget || root

  function refresh() {
    if (hostWidget && hostWidget.refresh) hostWidget.refresh()
  }

  function open() {
    root.controller.show()
    selectedIndex = pullRequests.length > 0 ? 0 : -1
    if (pullRequests.length === 0 && !loading) refresh()
  }

  function close() { root.controller.hide() }

  function moveSelection(delta) {
    if (pullRequests.length === 0) return
    selectedIndex = Math.max(0, Math.min(pullRequests.length - 1, selectedIndex + delta))
    Qt.callLater(function() {
      pullRequestList.positionViewAtIndex(selectedIndex, ListView.Contain)
    })
  }

  function activateSelection() {
    if (selectedIndex < 0 || selectedIndex >= pullRequests.length) return
    openPullRequest(pullRequests[selectedIndex])
  }

  function openPullRequest(pullRequest) {
    if (!pullRequest || !pullRequest.url) return
    Qt.openUrlExternally(pullRequest.url)
    close()
  }

  function authorLabel(pullRequest) {
    if (pullRequest.type === "my") return "Opened by you"
    var login = pullRequest.author && pullRequest.author.login ? pullRequest.author.login : "unknown"
    return "Review requested · @" + login
  }

  function sectionLabel(pullRequest) {
    return pullRequest.type === "my" ? "YOUR PULL REQUESTS" : "READY FOR YOUR REVIEW"
  }

  function typeColor(pullRequest) {
    return pullRequest.type === "review" ? root.bar.urgent : Color.accent
  }

  function scrollWheel(event) {
    var angleDelta = event.angleDelta.y
    var pixelDelta = angleDelta === 0 ? event.pixelDelta.y : 0
    var change = angleDelta !== 0
      ? angleDelta / 120 * pullRequestList.wheelStepSize
      : pixelDelta
    if (change === 0) return

    var minimumY = pullRequestList.originY - pullRequestList.topMargin
    var maximumY = Math.max(minimumY, pullRequestList.originY
      + pullRequestList.contentHeight + pullRequestList.bottomMargin - pullRequestList.height)
    var animationBase = wheelScrollAnimation.running
      ? pullRequestList.wheelTargetY
      : pullRequestList.contentY
    var targetY = Math.max(minimumY, Math.min(maximumY, animationBase - change))
    var distance = Math.abs(targetY - pullRequestList.contentY)

    wheelScrollAnimation.stop()
    pullRequestList.wheelTargetY = targetY
    if (distance > 2) {
      wheelScrollAnimation.from = pullRequestList.contentY
      wheelScrollAnimation.to = targetY
      wheelScrollAnimation.duration = Math.max(50, Math.min(200,
        Math.round(distance * 200 / pullRequestList.wheelStepSize)))
      wheelScrollAnimation.start()
    } else {
      pullRequestList.contentY = targetY
    }
    event.accepted = true
  }

  onPullRequestsChanged: {
    if (pullRequests.length === 0) selectedIndex = -1
    else if (selectedIndex < 0) selectedIndex = 0
    else if (selectedIndex >= pullRequests.length) selectedIndex = pullRequests.length - 1
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(500))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(400))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveSelection(dy) }
      onActivateRequested: root.activateSelection()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) { if (text === "r" || text === "R") root.refresh() }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(4)

        Row {
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width - refreshButton.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            text: "GitHub pull requests"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }

          Button {
            id: refreshButton
            anchors.verticalCenter: parent.verticalCenter
            iconText: root.loading ? "󰦖" : ""
            iconSpinning: root.loading
            tooltipText: "Refresh pull requests"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.space(4)
            onClicked: root.refresh()
          }
        }

        Text {
          width: parent.width
          text: root.myCount + " yours · " + root.reviewCount + " to review"
          color: Qt.darker(root.bar.foreground, 1.35)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        PanelSeparator {
          foreground: root.bar.foreground
        }

        Text {
          visible: root.errorMessage !== ""
          width: parent.width
          text: root.errorMessage
          color: root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }

        Text {
          visible: root.loading && root.cacheReady && root.pullRequests.length === 0
          width: parent.width
          text: "Loading pull requests…"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          visible: !root.loading && root.pullRequests.length === 0 && root.errorMessage === ""
          width: parent.width
          text: "No matching pull requests"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        ListView {
          id: pullRequestList
          readonly property real wheelStepSize: Style.space(60)
          property real wheelTargetY: contentY
          visible: root.pullRequests.length > 0
          width: parent.width
          height: Math.min(contentHeight, Style.space(280))
          spacing: 0
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          interactive: false
          model: root.pullRequests
          currentIndex: root.selectedIndex

          NumberAnimation {
            id: wheelScrollAnimation
            target: pullRequestList
            property: "contentY"
            easing.type: Easing.OutCubic
          }

          ScrollBar.vertical: ScrollBar {
            policy: ScrollBar.AsNeeded
            active: wheelScrollAnimation.running || hovered || pressed
          }

          delegate: Item {
            id: delegateRoot
            required property var modelData
            required property int index
            readonly property bool startsSection: index === 0
              || root.pullRequests[index - 1].type !== modelData.type
            width: ListView.view.width
            height: Style.space(48) + (startsSection ? Style.space(28) : 0)

            Row {
              visible: delegateRoot.startsSection
              width: parent.width
              height: delegateRoot.startsSection ? Style.space(28) : 0
              spacing: Style.space(8)

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.sectionLabel(delegateRoot.modelData)
                color: root.typeColor(delegateRoot.modelData)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }

              Rectangle {
                width: parent.width - parent.children[0].implicitWidth - parent.spacing
                height: Style.space(1)
                anchors.verticalCenter: parent.verticalCenter
                color: root.typeColor(delegateRoot.modelData)
                opacity: 0.35
              }

              MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.NoButton
                onWheel: function(wheel) { root.scrollWheel(wheel) }
              }
            }

            Rectangle {
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.bottom: parent.bottom
              height: Style.space(48)
              radius: Math.min(4, Style.cornerRadius)
              color: delegateRoot.index === root.selectedIndex || rowMouse.containsMouse
                ? Style.hoverFillFor(root.bar.foreground, Color.accent)
                : "transparent"

              Rectangle {
                width: Style.space(2)
                height: parent.height - Style.space(12)
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                color: root.typeColor(delegateRoot.modelData)
                opacity: delegateRoot.index === root.selectedIndex || rowMouse.containsMouse ? 1 : 0.55
              }

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(8)
                anchors.rightMargin: Style.space(8)
                spacing: 0

                Text {
                  width: parent.width
                  text: delegateRoot.modelData.title
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.body
                  elide: Text.ElideRight
                }

                Row {
                  width: parent.width
                  spacing: Style.space(4)

                  Text {
                    id: repositoryLabel
                    width: Math.min(implicitWidth, parent.width * 0.5)
                    text: delegateRoot.modelData.repositoryName + " #" + delegateRoot.modelData.number
                    color: Color.accent
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                    elide: Text.ElideRight
                  }

                  Text {
                    id: metadataSeparator
                    text: "·"
                    color: Qt.darker(root.bar.foreground, 1.35)
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.bodySmall
                  }

                  Text {
                    width: Math.max(0, parent.width - repositoryLabel.width
                      - metadataSeparator.implicitWidth - parent.spacing * 2)
                    text: root.authorLabel(delegateRoot.modelData)
                    color: delegateRoot.modelData.type === "review"
                      ? root.bar.foreground
                      : Qt.darker(root.bar.foreground, 1.2)
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    elide: Text.ElideRight
                  }
                }
              }

              MouseArea {
                id: rowMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.openPullRequest(delegateRoot.modelData)
                onWheel: function(wheel) { root.scrollWheel(wheel) }
              }
            }
          }
        }
      }
    }
  }
}