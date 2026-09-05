import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "fredrik.ryde-nearby"
  ipcTarget: "fredrik.ryde-nearby"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var dependencyData: null
  property bool dependencyChecking: true
  property bool installLaunched: false
  property string dependencyError: ""

  readonly property var barIdentity: hostWidget || root
  readonly property var checks:
    dependencyData && Array.isArray(dependencyData.checks)
      ? dependencyData.checks : []

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() {
    if (root.opened) close()
    else open()
  }
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(
      setupColumn.implicitHeight + Style.space(32),
      Style.space(460)
    )

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: {
        if (root.hostWidget && root.hostWidget.installDependencies)
          root.hostWidget.installDependencies()
      }

      Rectangle {
        anchors.fill: parent
        radius: Style.cornerRadius
        color: Color.popups.background

        Column {
          id: setupColumn
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: parent.top
          anchors.margins: Style.space(16)
          spacing: Style.space(12)

          Row {
            width: parent.width
            spacing: Style.space(12)

            ScooterIcon {
              width: Style.space(34)
              height: width
              anchors.verticalCenter: parent.verticalCenter
              strokeColor: Color.popups.text
            }

            Column {
              width: parent.width - Style.space(46)
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: "Ryde needs a quick setup"
                color: Color.popups.text
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }
              Text {
                width: parent.width
                text: "The map is paused until every local requirement is ready."
                color: Qt.darker(Color.popups.text, 1.35)
                wrapMode: Text.WordWrap
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          Repeater {
            model: root.checks

            delegate: Rectangle {
              required property var modelData
              width: setupColumn.width
              height: Style.space(42)
              radius: Style.cornerRadius
              color: Style.normalFillFor(Color.popups.text, Color.accent)
              border.color: Util.alpha(Color.popups.text, 0.16)

              Text {
                anchors.left: parent.left
                anchors.leftMargin: Style.space(12)
                anchors.verticalCenter: parent.verticalCenter
                text: modelData.ready ? "✓" : "!"
                color: modelData.ready ? Color.accent : Color.urgent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }

              Column {
                anchors.left: parent.left
                anchors.leftMargin: Style.space(38)
                anchors.right: statusText.left
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                spacing: 0

                Text {
                  width: parent.width
                  text: modelData.label
                  color: Color.popups.text
                  elide: Text.ElideRight
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
                Text {
                  width: parent.width
                  text: modelData.detail
                  color: Qt.darker(Color.popups.text, 1.35)
                  elide: Text.ElideRight
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              Text {
                id: statusText
                anchors.right: parent.right
                anchors.rightMargin: Style.space(12)
                anchors.verticalCenter: parent.verticalCenter
                text: modelData.status
                  ? modelData.status
                  : (modelData.ready ? "Ready" : "Missing")
                color: modelData.ready ? Color.accent : Color.urgent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
            }
          }

          Text {
            visible: root.dependencyChecking || root.installLaunched
            width: parent.width
            text: root.installLaunched
              ? "Setup is running in the terminal. Checks update automatically."
              : "Checking local requirements…"
            color: Color.accent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            visible: root.dependencyError !== ""
            width: parent.width
            text: root.dependencyError
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Button {
            width: parent.width
            text: root.installLaunched
              ? "Run setup again"
              : "Install & fix requirements"
            enabled: !root.dependencyChecking
            onClicked: {
              if (root.hostWidget && root.hostWidget.installDependencies)
                root.hostWidget.installDependencies()
            }
          }

          Text {
            width: parent.width
            text: "Opens an Omarchy terminal and may ask for your administrator password. Wi-Fi positioning uses BeaconDB; terrain data is cached later."
            color: Qt.darker(Color.popups.text, 1.35)
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
