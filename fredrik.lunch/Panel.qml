import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "fredrik.lunch"
  ipcTarget: "fredrik.lunch"

  property var anchorItem: null
  property var hostWidget: null
  property var menuData: null
  property bool loading: false
  property string errorMessage: ""
  property bool stale: false
  property int weekIndex: 0

  readonly property var barIdentity: hostWidget || root
  readonly property var weeks:
    menuData && Array.isArray(menuData.weeks) ? menuData.weeks : []
  readonly property var currentWeek:
    weekIndex >= 0 && weekIndex < weeks.length ? weeks[weekIndex] : null

  function refresh() {
    if (hostWidget && hostWidget.refresh) hostWidget.refresh()
  }

  function selectCurrentWeek() {
    for (var index = 0; index < weeks.length; index++) {
      if (weeks[index].isDefault === true
          || (menuData && weeks[index].start === menuData.defaultWeekStart)) {
        weekIndex = index
        return
      }
    }
    weekIndex = Math.max(0, weeks.length - 1)
  }

  function stepWeek(delta) {
    weekIndex = Math.max(0, Math.min(weeks.length - 1, weekIndex + delta))
  }

  function open() {
    root.controller.show()
    Qt.callLater(root.selectCurrentWeek)
    if (!menuData && !loading) refresh()
  }

  function close() { root.controller.hide() }

  onMenuDataChanged: Qt.callLater(root.selectCurrentWeek)
  onOpenedChanged: {
    if (opened) Qt.callLater(root.selectCurrentWeek)
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(520))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(500))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (dx !== 0) root.stepWeek(dx)
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") root.refresh()
      }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(8)

        Row {
          width: parent.width
          spacing: Style.space(8)

          Column {
            width: parent.width - refreshButton.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            spacing: 0

            Text {
              width: parent.width
              text: "Lunch at Kanalpiren"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              width: parent.width
              text: "Hinna Park · Stavanger"
              color: Qt.darker(root.bar.foreground, 1.3)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }

          Button {
            id: refreshButton
            anchors.verticalCenter: parent.verticalCenter
            iconText: root.loading ? "󰦖" : ""
            iconSpinning: root.loading
            tooltipText: "Refresh lunch menu"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.space(4)
            onClicked: root.refresh()
          }
        }

        Text {
          visible: root.stale
          width: parent.width
          text: "Showing cached menu - refresh failed"
          color: root.bar.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
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

        PanelSeparator {
          foreground: root.bar.foreground
        }

        Row {
          width: parent.width

          Button {
            iconText: ""
            enabled: root.weekIndex > 0
            tooltipText: "Previous week"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.space(4)
            onClicked: root.stepWeek(-1)
          }

          Text {
            width: parent.width - parent.children[0].width - parent.children[2].width
            anchors.verticalCenter: parent.verticalCenter
            text: root.currentWeek
              ? root.currentWeek.label.toUpperCase().replace(" - ", " · ")
              : "LOADING MENU..."
            color: Qt.darker(root.bar.foreground, 1.15)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            font.letterSpacing: 0.5
            horizontalAlignment: Text.AlignHCenter
          }

          Button {
            iconText: ""
            enabled: root.weekIndex < root.weeks.length - 1
            tooltipText: "Next week"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.space(4)
            onClicked: root.stepWeek(1)
          }
        }

        ListView {
          id: dayList
          visible: root.currentWeek !== null
          width: parent.width
          height: Math.min(contentHeight, Style.space(360))
          model: root.currentWeek ? root.currentWeek.days : []
          spacing: Style.space(3)
          clip: true
          boundsBehavior: Flickable.StopAtBounds

          ScrollBar.vertical: ScrollBar {
            policy: ScrollBar.AsNeeded
          }

          delegate: Rectangle {
            id: dayCard
            required property var modelData
            width: ListView.view.width
            height: dayContent.implicitHeight + Style.space(12)
            radius: Math.min(6, Style.cornerRadius)
            color: modelData.isToday
              ? Style.hoverFillFor(root.bar.foreground, Color.accent)
              : Style.hoverFillFor(root.bar.foreground, root.bar.background)

            Column {
              id: dayContent
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              spacing: Style.space(3)

              Row {
                width: parent.width

                Text {
                  width: parent.width - dateText.width
                  text: dayCard.modelData.weekday.toUpperCase()
                  color: dayCard.modelData.isToday
                    ? Color.accent : root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                  font.letterSpacing: 0.6
                }

                Text {
                  id: dateText
                  text: (dayCard.modelData.isToday ? "TODAY · " : "")
                    + dayCard.modelData.dateLabel.toUpperCase()
                  color: dayCard.modelData.isToday
                    ? Color.accent : Qt.darker(root.bar.foreground, 1.3)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: dayCard.modelData.isToday
                  font.letterSpacing: 0.4
                }
              }

              Repeater {
                model: dayCard.modelData.items

                Column {
                  id: menuItem
                  required property var modelData
                  width: dayContent.width
                  spacing: Style.space(1)

                  Text {
                    width: parent.width
                    text: menuItem.modelData.label.toUpperCase()
                    color: dayCard.modelData.isToday
                      ? Color.accent : Qt.darker(root.bar.foreground, 1.3)
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                    font.letterSpacing: 0.7
                  }

                  Text {
                    width: parent.width
                    text: menuItem.modelData.name
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.weight: Font.Medium
                    wrapMode: Text.Wrap
                  }
                }
              }

              Text {
                visible: dayCard.modelData.items.length === 0
                width: parent.width
                text: "No menu published"
                color: Qt.darker(root.bar.foreground, 1.35)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.italic: true
              }
            }
          }
        }
      }
    }
  }
}
