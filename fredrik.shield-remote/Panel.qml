import QtQuick
import qs.Commons
import qs.Ui as Ui

Ui.Panel {
  id: root
  moduleName: "fredrik.shield-remote"
  manageIpc: false
  property var anchorItem: null
  property var hostWidget: null
  property bool fieldsInitialized: false
  property bool repairConfirmation: false
  readonly property var backend: hostWidget ? hostWidget.backendState : null
  readonly property bool running: backend && backend.running === true
  readonly property bool busy: (hostWidget && hostWidget.busy) || (backend && backend.input.pairing)
  readonly property var barIdentity: hostWidget || root
  readonly property string errorText: hostWidget && hostWidget.errorMessage ? hostWidget.errorMessage : backend ? backend.error || backend.shield.error || backend.input.error : ""

  onBackendChanged: {
    if (backend && (!fieldsInitialized
        || (hostField.text === "" && !hostField.activeFocus))) {
      hostField.text = backend.shield.host;
      fieldsInitialized = true;
    }
  }

  Ui.KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(700))

    Ui.PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: hostField.activeFocus || codeField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function (direction) {
        if (root.bar)
          root.bar.switchPanelFrom(root.barIdentity, direction);
      }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: content
          width: scroll.width
          spacing: Style.space(10)

          Text {
            text: "Shield Remote"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Text {
            width: parent.width
            text: "Double-press Menu to switch targets. Shield uses native remote keys; computer Home opens Stremio."
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            width: parent.width
            text: root.backend ? "Backend: " + (root.running ? "running" : "stopped") + "  |  Remote: " + (root.backend.input.captured ? "captured" : "not captured") + "\nShield: " + (root.backend.shield.connected ? "connected" : root.backend.shield.paired ? "paired, offline" : "not paired") + "\nRemote address: " + (root.backend.remote_address || "not selected") : "Checking local setup..."
            color: root.bar.foreground
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Row {
            spacing: Style.space(8)
            Ui.Button {
              text: "Computer"
              selected: root.backend && root.backend.target === "computer"
              enabled: root.running && !root.busy
              onClicked: root.hostWidget.request({
                command: "target",
                target: "computer"
              })
            }
            Ui.Button {
              text: "Shield TV"
              selected: root.backend && root.backend.target === "shield"
              enabled: root.running && !root.busy
              onClicked: root.hostWidget.request({
                command: "target",
                target: "shield"
              })
            }
            Ui.Button {
              text: root.running ? "Stop" : "Start"
              enabled: !root.busy
              onClicked: root.hostWidget.service(root.running ? "stop" : "start")
            }
          }
          Ui.PanelSeparator {
            foreground: root.bar.foreground
          }
          Text {
            text: "Shield TV pairing"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }
          Ui.TextField {
            id: hostField
            width: parent.width
            placeholderText: "Shield IP address or hostname"
            enabled: root.running && !root.busy && !(root.backend && root.backend.shield.pairing)
            onAccepted: root.hostWidget.request({
              command: "pair_start",
              host: text.trim()
            })
          }
          Row {
            spacing: Style.space(8)
            Ui.Button {
              text: "Show pairing code on TV"
              enabled: root.running && !root.busy && hostField.text.trim() !== "" && !(root.backend && root.backend.shield.pairing)
              onClicked: root.hostWidget.request({
                command: "pair_start",
                host: hostField.text.trim()
              })
            }
            Ui.Button {
              text: "Cancel"
              visible: root.backend && root.backend.shield.pairing
              enabled: !root.busy
              onClicked: {
                codeField.text = "";
                root.hostWidget.request({
                  command: "pair_cancel"
                });
              }
            }
          }
          Row {
            width: parent.width
            visible: root.backend && root.backend.shield.pairing
            spacing: Style.space(8)
            Ui.TextField {
              id: codeField
              width: parent.width - pairButton.width - parent.spacing
              placeholderText: "Six-character TV code"
              password: true
              maximumLength: 6
              enabled: !root.busy
              onAccepted: pairButton.clicked()
            }
            Ui.Button {
              id: pairButton
              text: "Pair"
              enabled: !root.busy && codeField.text.length === 6
              onClicked: {
                root.hostWidget.request({
                  command: "pair_finish",
                  code: codeField.text.trim()
                });
                codeField.text = "";
              }
            }
          }
          Text {
            width: parent.width
            text: "Uses Android TV Remote Service over your LAN, without ADB. Credentials stay private on this computer."
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Ui.PanelSeparator {
            foreground: root.bar.foreground
          }
          Text {
            text: "Physical Bluetooth remote"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }
          Text {
            width: parent.width
            text: root.backend && root.backend.input.pairing ? root.backend.input.pairing_stage
              : root.backend && root.backend.input.captured ? "Already paired and captured. Reconnecting: wake with an arrow; do not enter pairing mode."
              : "Pairing: hold Home + Back until the indicator blinks, then Pair remote. Reconnecting: wake with an arrow; do not enter pairing mode."
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Row {
            spacing: Style.space(8)
            Ui.Button {
              text: "Setup input / service"
              visible: !root.backend || !root.backend.setup_complete
              enabled: !root.busy
              onClicked: root.hostWidget.setup()
            }
            Ui.Button {
              text: "Reconnect remote"
              enabled: root.running && !root.busy && root.backend.input.setup
              onClicked: root.hostWidget.request({
                command: "connect_remote"
              })
            }
          }
          Row {
            visible: root.backend && !root.backend.input.captured
            spacing: Style.space(8)
            Ui.Button {
              text: root.backend && root.backend.input.pairing ? "Pairing..." : "Pair remote"
              enabled: root.running && !root.busy && root.backend.input.setup
              onClicked: {
                root.repairConfirmation = false;
                root.hostWidget.request({ command: "pair_remote" });
              }
            }
            Ui.Button {
              text: "Repair pairing"
              enabled: root.running && !root.busy && root.backend.input.setup
              onClicked: root.repairConfirmation = true
            }
          }
          Text {
            width: parent.width
            visible: root.repairConfirmation
            text: "This forgets ONLY the selected remote's Linux bond and pairs it again. Shield TV credentials are unchanged. Hold Home + Back until it blinks before confirming."
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Row {
            visible: root.repairConfirmation
            spacing: Style.space(8)
            Ui.Button {
              text: "Forget bond and pair"
              enabled: root.running && !root.busy && root.backend.input.setup
              onClicked: {
                root.repairConfirmation = false;
                root.hostWidget.request({ command: "repair_remote", confirm: true });
              }
            }
            Ui.Button {
              text: "Cancel"
              enabled: !root.busy
              onClicked: root.repairConfirmation = false
            }
          }
          Text {
            width: parent.width
            visible: root.errorText !== ""
            text: root.errorText
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            width: parent.width
            text: root.backend && root.backend.last_action ? root.backend.last_action : "Power, microphone and Netflix are ignored. Single Menu is reserved."
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
