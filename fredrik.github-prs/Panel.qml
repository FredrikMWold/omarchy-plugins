import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Commons
import qs.Ui
import "PullRequests.js" as PullRequests

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
  property bool editingRepositories: false
  property bool savingRepositories: false
  property string repositorySaveError: ""

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

  function showRepositories() {
    if (!hostWidget) return
    editingRepositories = true
    repositoryPicker.begin(
      hostWidget.repositories("myRepositories", hostWidget.defaultMyRepositories),
      hostWidget.repositories("reviewRepositories", hostWidget.defaultReviewRepositories))
  }

  function finishRepositorySave() {
    editingRepositories = false
    keyCatcher.forceActiveFocus()
  }

  function cancelRepositoryEditing() {
    editingRepositories = false
    keyCatcher.forceActiveFocus()
  }

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

  function reviewColor(status) {
    if (status === "changes") return root.bar.urgent
    if (status === "approved") return Color.accent
    return root.bar.foreground
  }

  function reviewIcon(status) {
    if (status === "approved") return "✓"
    if (status === "changes") return "!"
    if (status === "unknown") return "?"
    return "◷"
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
    focusTarget: root.editingRepositories ? repositoryPicker.focusItem : keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(500))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight,
      Style.space(root.editingRepositories ? 520 : 400))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.editingRepositories
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveSelection(dy) }
      onActivateRequested: root.activateSelection()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") root.refresh()
        else if (text === ",") root.showRepositories()
      }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(4)

        Row {
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width - refreshButton.width - repositoryButton.width - parent.spacing * 2
            anchors.verticalCenter: parent.verticalCenter
            text: root.editingRepositories ? "GitHub repositories" : "GitHub pull requests"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }

          Button {
            id: repositoryButton
            anchors.verticalCenter: parent.verticalCenter
            iconText: root.editingRepositories ? "←" : ""
            enabled: !root.savingRepositories
            tooltipText: root.editingRepositories ? "Back to pull requests" : "Choose repositories (,)"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.space(4)
            onClicked: {
              if (root.editingRepositories) root.cancelRepositoryEditing()
              else root.showRepositories()
            }
          }

          Button {
            id: refreshButton
            visible: !root.editingRepositories
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
          visible: !root.editingRepositories
          width: parent.width
          text: root.myCount + " yours · " + root.reviewCount + " to review"
          color: Qt.darker(root.bar.foreground, 1.35)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        PanelSeparator {
          foreground: root.bar.foreground
        }

        RepositoryPicker {
          id: repositoryPicker
          visible: root.editingRepositories
          width: parent.width
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          persistenceError: root.repositorySaveError
          saving: root.savingRepositories
          onSaveRequested: function(my, review) {
            if (root.hostWidget) root.hostWidget.saveRepositories(my, review)
          }
          onCancelled: root.cancelRepositoryEditing()
        }

        Text {
          visible: !root.editingRepositories && root.errorMessage !== ""
          width: parent.width
          text: root.errorMessage
          color: root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }

        Text {
          visible: !root.editingRepositories && root.loading && root.cacheReady && root.pullRequests.length === 0
          width: parent.width
          text: "Loading pull requests…"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          visible: !root.editingRepositories && !root.loading && root.pullRequests.length === 0 && root.errorMessage === ""
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
          visible: !root.editingRepositories && root.pullRequests.length > 0
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
            readonly property string reviewStatus: PullRequests.reviewStatus(modelData)
            readonly property color reviewColor: root.reviewColor(reviewStatus)
            readonly property string stackLabel: PullRequests.stackLabel(modelData)
            width: ListView.view.width
            height: rowContent.implicitHeight + Style.space(16)
              + (startsSection ? Style.space(28) : 0)

            Item {
              visible: delegateRoot.startsSection
              width: parent.width
              height: delegateRoot.startsSection ? Style.space(28) : 0

              Row {
                anchors.fill: parent
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
              height: rowContent.implicitHeight + Style.space(16)
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
                id: rowContent
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(8)
                anchors.rightMargin: Style.space(8)
                spacing: 0

                Text {
                  width: parent.width
                  text: delegateRoot.modelData.title
                  textFormat: Text.PlainText
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

                Row {
                  width: parent.width
                  spacing: Style.space(6)

                  Rectangle {
                    id: reviewBadge
                    width: reviewText.implicitWidth + Style.space(10)
                    height: reviewText.implicitHeight + Style.space(4)
                    radius: Math.min(4, Style.cornerRadius)
                    color: Qt.rgba(delegateRoot.reviewColor.r, delegateRoot.reviewColor.g,
                      delegateRoot.reviewColor.b, 0.12)

                    Text {
                      id: reviewText
                      anchors.centerIn: parent
                      text: root.reviewIcon(delegateRoot.reviewStatus) + " "
                        + PullRequests.reviewLabel(delegateRoot.reviewStatus)
                      color: delegateRoot.reviewColor
                      font.family: root.bar.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }

                  Text {
                    visible: delegateRoot.stackLabel !== ""
                    width: Math.max(0, parent.width - reviewBadge.width - parent.spacing)
                    anchors.verticalCenter: parent.verticalCenter
                    text: delegateRoot.stackLabel
                    textFormat: Text.PlainText
                    color: Color.accent
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.caption
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

                ToolTip {
                  id: stackTooltip
                  visible: rowMouse.containsMouse && delegateRoot.stackLabel !== ""
                  delay: 500
                  text: PullRequests.stackTooltip(delegateRoot.modelData)
                  width: Math.min(implicitWidth, panel.contentWidth)
                  padding: Style.space(8)

                  contentItem: Text {
                    text: stackTooltip.text
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: Color.tooltip.text
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.bodySmall
                  }

                  background: Rectangle {
                    color: Color.tooltip.background
                    border.color: Color.tooltip.border
                    radius: Math.min(4, Style.cornerRadius)
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}