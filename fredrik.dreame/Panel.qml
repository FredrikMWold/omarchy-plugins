pragma ComponentBehavior: Bound
import QtQuick
import qs.Commons
import qs.Ui as Ui

Ui.Panel {
  id: root
  moduleName: "fredrik.dreame"
  manageIpc: false
  property var anchorItem: null
  property var hostWidget: null
  property bool logoutConfirmation: false
  property bool diagnosticsOpen: false
  property bool planDetailsOpen: false
  property bool settingsOpen: false
  readonly property var barIdentity: hostWidget || root
  readonly property var accountState: hostWidget ? hostWidget.backendState : null
  readonly property bool signedIn: hostWidget && hostWidget.signedIn
  readonly property bool busy: hostWidget && hostWidget.busy
  readonly property var status: hostWidget ? hostWidget.deviceStatus : null
  readonly property var confirmation: hostWidget ? hostWidget.confirmation : null
  readonly property var program: hostWidget ? hostWidget.program : null
  readonly property bool programActive: hostWidget && hostWidget.programActive
  readonly property bool stale: status && hostWidget.clockNow - status.observedAt * 1000 > 90000

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() {
    if (root.opened) close()
    else open()
  }

  Ui.KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(body.implicitHeight, Style.space(720))

    Ui.PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: propertiesField.activeFocus || commandField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) {
        if (root.bar) root.bar.switchPanelFrom(root.barIdentity, direction)
      }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: body.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: body
          width: scroll.width
          spacing: Style.space(16)
          Row {
            width: parent.width
            spacing: Style.space(8)
            Text {
              width: parent.width - settingsButton.implicitWidth - refreshButton.implicitWidth - parent.spacing * 2
              height: settingsButton.implicitHeight
              verticalAlignment: Text.AlignVCenter
              text: "Dreame"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.heading
              font.bold: true
            }
            Ui.Button {
              id: refreshButton
              iconText: "󰑐"
              tooltipText: "Refresh vacuum status"
              focusable: true
              enabled: root.hostWidget && !root.busy
              onClicked: root.hostWidget.refresh()
            }
            Ui.Button {
              id: settingsButton
              text: root.settingsOpen ? "Back" : "Settings"
              focusable: true
              onClicked: root.settingsOpen = !root.settingsOpen
            }
          }
          Dashboard {
            id: dashboard
            width: parent.width
            visible: !root.settingsOpen
            hostWidget: root.hostWidget
            bar: root.bar
            onSettingsRequested: root.settingsOpen = true
          }
        Column {
          id: content
          visible: root.settingsOpen
          width: scroll.width
          spacing: Style.space(10)

          Text {
            text: "Device settings and diagnostics"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Text {
            width: parent.width
            text: "Manage your account, selected vacuum and advanced controls. Credentials are entered only in the private sign-in terminal."
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Row {
            spacing: Style.space(8)
            Ui.Button {
              text: "Setup"
              enabled: !root.busy
              onClicked: root.hostWidget.launch("setup.sh")
            }
            Ui.Button {
              text: root.signedIn ? "Sign in again" : "Sign in"
              enabled: !root.busy && root.accountState && root.accountState.profileReady
              onClicked: root.hostWidget.launch("login.sh")
            }
            Ui.Button {
              text: root.busy ? "Working..." : "Refresh"
              enabled: !root.busy
              onClicked: {
                root.hostWidget.authBlocked = false
                root.hostWidget.request({op: root.signedIn ? "devices" : "state"})
              }
            }
          }
          Text {
            visible: root.hostWidget && root.hostWidget.loginWaiting
            width: parent.width
            text: "Complete setup/login in the private terminal. No password is requested in this panel."
            color: Color.accent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            visible: root.hostWidget && root.hostWidget.errorMessage !== ""
            textFormat: Text.PlainText
            width: parent.width
            text: root.hostWidget ? root.hostWidget.errorMessage : ""
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            visible: root.signedIn
            textFormat: Text.PlainText
            width: parent.width
            text: "Account region: " + (root.accountState ? root.accountState.region : "")
              + ". Manual API commands are " + (root.accountState && root.accountState.mutationsEnabled ? "enabled." : "disabled.")
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Repeater {
            model: root.hostWidget ? root.hostWidget.devices : []
            delegate: Ui.Button {
              required property var modelData
              width: content.width
              text: modelData.name + " (" + modelData.model + ")"
              selected: root.accountState && root.accountState.selectedDid === modelData.did
              enabled: !root.busy
              onClicked: root.hostWidget.request({op: "select", deviceId: modelData.did})
            }
          }
          Text {
            visible: root.signedIn && root.hostWidget && root.hostWidget.devices.length === 0 && !root.busy
            width: parent.width
            text: "No vacuums loaded. Refresh to discover devices; verify the account region if the list is empty."
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Column {
            visible: root.status !== null
            width: parent.width
            spacing: Style.space(8)
            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: root.status ? root.status.stateLabel : ""
              color: root.stale ? Color.urgent : Color.accent
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              width: parent.width
              text: root.status ? (root.stale ? "Stale · last successful read " : "Updated ")
                + new Date(root.status.observedAt * 1000).toLocaleTimeString() : ""
              color: root.stale ? Color.urgent : Color.muted
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
            Flow {
              width: parent.width
              spacing: Style.space(10)
              Repeater {
                model: root.status ? root.status.display || [] : []
                delegate: Column {
                  required property var modelData
                  width: (content.width - Style.space(10)) / 2
                  spacing: Style.space(2)
                  Text {
                    width: parent.width
                    text: modelData.label
                    color: Color.muted
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: modelData.text
                    color: !modelData.available ? Color.muted
                      : modelData.key === "error" && modelData.text !== "None" ? Color.urgent : root.bar.foreground
                    wrapMode: Text.WordWrap
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.body
                  }
                }
              }
            }
          }
          Ui.PanelSeparator { foreground: root.bar.foreground }
          Text {
            text: "Cleaning program"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }
          Text {
            width: parent.width
            text: "Use Speak on your remote for cleaning requests. Say \"stop\" to end the current program and return to base."
            color: Color.muted
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Ui.Button {
            visible: root.hostWidget && root.hostWidget.canEndProgram
            text: root.hostWidget ? root.hostWidget.endProgramLabel : ""
            enabled: !root.busy
            onClicked: root.hostWidget.stopProgram()
          }
          Text {
            visible: root.program !== null
            width: parent.width
            textFormat: Text.PlainText
            text: root.program ? "Program: " + root.program.phase
              + (root.program.deviceName ? " · " + root.program.deviceName : "")
              + (root.program.stepCount ? " · Task " + Math.min(root.program.stepIndex + 1, root.program.stepCount)
                  + " of " + root.program.stepCount : "")
              + (root.program.currentTarget ? "\n" + root.program.currentTarget : "")
              + (root.program.error ? "\n" + root.program.error.message : "")
              + (root.hostWidget && root.hostWidget.programControlMessage
                ? "\n" + root.hostWidget.programControlMessage : "") : ""
            color: root.program && (root.program.phase === "failed" || root.program.error) ? Color.urgent : root.bar.foreground
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Repeater {
            model: root.program && root.program.packet && root.program.packet.type === "clarification"
              ? root.program.packet.issues : []
            delegate: Text {
              required property var modelData
              width: content.width
              textFormat: Text.PlainText
              text: modelData.question + "\nUse Speak to send a complete request with this detail."
              color: Color.accent
              wrapMode: Text.WordWrap
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }
          Ui.Button {
            visible: !!(root.program && root.program.packet)
            text: root.planDetailsOpen ? "Hide Luna response" : "Show Luna response"
            onClicked: root.planDetailsOpen = !root.planDetailsOpen
          }
          Text {
            visible: root.planDetailsOpen && root.program && root.program.packet
            width: parent.width
            textFormat: Text.PlainText
            text: root.program && root.program.packet ? JSON.stringify(root.program.packet, null, 2) : ""
            color: Color.muted
            wrapMode: Text.WrapAnywhere
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Ui.Button {
            text: root.diagnosticsOpen ? "Hide protocol tools" : "Protocol tools"
            onClicked: root.diagnosticsOpen = !root.diagnosticsOpen
          }
          Column {
            visible: root.diagnosticsOpen
            width: parent.width
            spacing: Style.space(10)
          Text {
            visible: root.status !== null
            width: parent.width
            textFormat: Text.PlainText
            text: root.status ? JSON.stringify(root.status.properties, null, 2) : ""
            color: Color.muted
            wrapMode: Text.WrapAnywhere
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Ui.PanelSeparator { foreground: root.bar.foreground }
          Text {
            text: "Read protocol properties"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }
          Ui.TextField {
            id: propertiesField
            width: parent.width
            text: '[{"siid":2,"piid":1},{"siid":3,"piid":1}]'
            enabled: root.signedIn && !root.busy
          }
          Ui.Button {
            text: "Read properties"
            enabled: root.signedIn && !root.busy && root.accountState && root.accountState.selectedDid !== ""
            onClicked: root.hostWidget.readProperties(propertiesField.text)
          }
          Ui.PanelSeparator { foreground: root.bar.foreground }
          Text {
            text: "Advanced commands"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }
          Text {
            width: parent.width
            text: "Raw protocol access, not verified cleaning presets. Actions can start cleaning, docking, mapping or other operations. Only use model-verified identifiers. Commands require idle state and a separate, single-use confirmation; failures are never automatically replayed or rolled back."
            color: Color.urgent
            wrapMode: Text.WordWrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Ui.Button {
            text: root.accountState && root.accountState.mutationsEnabled ? "Disable advanced commands" : "Enable advanced commands"
            selected: root.accountState && root.accountState.mutationsEnabled
            enabled: root.signedIn && !root.busy && !root.programActive
            onClicked: root.hostWidget.request({op: "enable-mutations", enabled: !root.accountState.mutationsEnabled})
          }
          Ui.TextField {
            id: commandField
            width: parent.width
            placeholderText: '{"method":"set_properties","params":[...]}'
            enabled: root.accountState && root.accountState.mutationsEnabled && !root.busy && !root.programActive
            onTextChanged: {
              if (root.hostWidget) root.hostWidget.confirmation = null
            }
          }
          Ui.Button {
            text: "Prepare command preview"
            enabled: root.accountState && root.accountState.mutationsEnabled && root.accountState.selectedDid !== ""
              && !root.busy && !root.programActive
            onClicked: root.hostWidget.previewCommand(commandField.text)
          }
          Text {
            visible: root.confirmation !== null
            textFormat: Text.PlainText
            width: parent.width
            text: root.confirmation ? "Confirm for " + root.confirmation.deviceName
              + " [" + root.confirmation.deviceId + "]\n" + root.confirmation.method
              + "\n" + JSON.stringify(root.confirmation.params, null, 2)
              + "\nExpires: " + new Date(root.confirmation.expiresAt * 1000).toLocaleTimeString() : ""
            color: Color.urgent
            wrapMode: Text.WrapAnywhere
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Row {
            visible: root.confirmation !== null
            spacing: Style.space(8)
            Ui.Button {
              text: "Confirm and send once"
              enabled: !root.busy && !root.programActive
              onClicked: root.hostWidget.confirmCommand()
            }
            Ui.Button {
              text: "Cancel"
              enabled: !root.busy
              onClicked: {
                root.hostWidget.confirmation = null
                root.hostWidget.request({op: "cancel"})
              }
            }
          }
          Text {
            visible: root.hostWidget && root.hostWidget.rawResult !== ""
            textFormat: Text.PlainText
            width: parent.width
            text: root.hostWidget ? root.hostWidget.rawResult : ""
            color: Color.muted
            wrapMode: Text.WrapAnywhere
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          }
          Ui.Button {
            visible: root.signedIn
            text: root.logoutConfirmation ? "Confirm: remove local session" : "Sign out locally"
            enabled: !root.busy
            onClicked: {
              if (root.logoutConfirmation) {
                root.logoutConfirmation = false
                root.hostWidget.request({op: "logout"})
              } else {
                root.logoutConfirmation = true
              }
            }
          }
          }
        }
      }
    }
  }
}
