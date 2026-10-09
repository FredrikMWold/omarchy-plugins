import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "fredrik.youtube-music"
  ipcTarget: "fredrik.youtube-music"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var dependencyData: null
  property bool dependencyChecking: true
  property bool setupLaunched: false
  property string dependencyError: ""

  readonly property var barIdentity: hostWidget || root
  readonly property var checks:
    dependencyData && Array.isArray(dependencyData.checks)
      ? dependencyData.checks : []

  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function")
      return bar.switchPanelFrom(barIdentity, direction)
    return false
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(
      contentColumn.implicitHeight,
      Style.space(500)
    )

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: {
        if (root.hostWidget) root.hostWidget.launchSetup()
      }

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.space(12)

        Row {
          width: parent.width
          spacing: Style.space(12)

          Text {
            text: "󰝚"
            color: Color.accent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.display
          }

          Column {
            width: parent.width - Style.space(48)
            spacing: Style.space(2)

            Text {
              width: parent.width
              text: "Connect YouTube Music"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              width: parent.width
              text: "Account data stays local and is stored with private file permissions."
              color: Qt.darker(root.bar.foreground, 1.35)
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
            width: contentColumn.width
            height: Style.space(44)
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.bar.foreground, Color.accent)
            border.color: Util.alpha(root.bar.foreground, 0.16)

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
              anchors.right: statusLabel.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter

              Text {
                width: parent.width
                text: modelData.label
                color: root.bar.foreground
                elide: Text.ElideRight
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }
              Text {
                width: parent.width
                text: modelData.detail
                color: Qt.darker(root.bar.foreground, 1.35)
                elide: Text.ElideMiddle
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Text {
              id: statusLabel
              anchors.right: parent.right
              anchors.rightMargin: Style.space(12)
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.status
              color: modelData.ready ? Color.accent : Color.urgent
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
          }
        }

        Text {
          visible: root.dependencyChecking || root.setupLaunched
          width: parent.width
          text: root.setupLaunched
            ? "Setup is running in the terminal. This panel updates automatically."
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
          text: root.setupLaunched ? "Open setup again" : "Start account setup"
          enabled: !root.dependencyChecking
          onClicked: if (root.hostWidget) root.hostWidget.launchSetup()
        }

        Text {
          width: parent.width
          text: "Setup installs the API and Chrome-session importer, reads only YouTube cookies from your local Chrome profile and keyring, then validates the account. Nothing is uploaded. Manual header copying is available only if automatic import fails."
          color: Qt.darker(root.bar.foreground, 1.35)
          wrapMode: Text.WordWrap
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
