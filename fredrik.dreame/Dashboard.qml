pragma ComponentBehavior: Bound
import QtQuick
import qs.Commons
import qs.Ui as Ui

Column {
  id: root
  property var hostWidget: null
  property var bar: null
  signal settingsRequested()
  readonly property var status: hostWidget ? hostWidget.deviceStatus : null
  readonly property var program: hostWidget ? hostWidget.program : null
  readonly property var voice: hostWidget && hostWidget.backendState ? hostWidget.backendState.voice : null
  readonly property bool signedIn: hostWidget && hostWidget.signedIn
  readonly property bool active: hostWidget && hostWidget.programActive
  readonly property bool voiceBusy: hostWidget && hostWidget.voiceBusy
  readonly property bool stale: status && hostWidget.clockNow - status.observedAt * 1000 > 90000
  readonly property string vacuumName: status ? status.device.name : "Your vacuum"
  spacing: Style.space(14)

  function metric(key) {
    var fields = status ? status.display || [] : []
    for (var i = 0; i < fields.length; i++) {
      if (fields[i].key === key) return fields[i]
    }
    return {text: "-", available: false}
  }

  function phaseLabel() {
    if (!program) return ""
    return ({
      queued: "Getting ready", planning: "Luna is planning", validating: "Checking your plan",
      applying: "Preparing the vacuum", starting: "Starting cleaning", running: "Cleaning",
      waiting: "Finishing this task", clarification: "A little more detail",
      completed: "Cleaning complete", cancelled: "Cleaning cancelled", failed: "Needs attention"
    })[program.phase] || "Updating"
  }

  function updatedLabel() {
    if (!root.status) return "Waiting for a fresh reading"
    var age = Math.max(0, Math.floor((root.hostWidget.clockNow - root.status.observedAt * 1000) / 1000))
    return (age < 10 ? "Updated just now" : "Updated " + (age < 60 ? age + "s" : Math.floor(age / 60) + "m") + " ago")
      + " · Last / current clean"
  }

  function target(job) {
    return job.target.scope === "all" ? "Whole home" : job.target.rooms.join(" → ")
  }

  function method(job) {
    return ({vacuum: "Vacuum", mop: "Mop", "mop-after-vac": "Vacuum, then mop"})[job.method] || job.method
  }

  function details(job) {
    var parts = []
    if (job.suction) parts.push(job.suction.charAt(0).toUpperCase() + job.suction.slice(1) + " suction")
    if (job.wetness) parts.push("Wetness " + job.wetness + "/32")
    if (job.passes) parts.push(job.passes + (job.passes === 1 ? " pass" : " passes"))
    parts.push(job.route.charAt(0).toUpperCase() + job.route.slice(1) + " route")
    if (job.washing) parts.push(job.washing.by === "room" ? "Wash after each room"
      : "Wash every " + job.washing.value + (job.washing.by === "area" ? " m²" : " min"))
    if (job.maxSuctionPower) parts.push("One-shot boost")
    return parts.join(" · ")
  }

  component Label: Text {
    textFormat: Text.PlainText
    color: Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    wrapMode: Text.WordWrap
  }

  component Surface: Rectangle {
    width: parent.width
    radius: Style.cornerRadius
    color: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.035)
    border.width: 1
    border.color: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.10)
  }

  Surface {
    visible: root.signedIn && root.hostWidget.selectedDid !== ""
    implicitHeight: hero.implicitHeight + Style.space(32)
    Column {
      id: hero
      x: Style.space(16)
      y: Style.space(16)
      width: parent.width - Style.space(32)
      spacing: Style.space(14)
      Row {
        width: parent.width
        spacing: Style.space(12)
        Rectangle {
          width: Style.space(52)
          height: width
          radius: width / 2
          color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.10)
          Label {
            anchors.centerIn: parent
            text: "󰚩"
            font.pixelSize: Style.space(30)
            color: Color.accent
          }
        }
        Column {
          width: parent.width - Style.space(136)
          spacing: Style.space(5)
          Label {
            width: parent.width
            text: root.vacuumName
            font.pixelSize: Style.font.title
            font.bold: true
            maximumLineCount: 1
            elide: Text.ElideRight
          }
          Label {
            width: parent.width
            text: !root.status ? "Updating status…" : root.status.stateLabel === "Charging completed"
              ? "Ready · Docked" : root.status.stateLabel
            color: root.stale ? Color.urgent : Color.accent
          }
        }
        Column {
          width: Style.space(60)
          spacing: Style.space(5)
          Label {
            width: parent.width
            horizontalAlignment: Text.AlignRight
            text: root.metric("battery").text
            font.pixelSize: Style.font.heading
            font.bold: true
          }
          Rectangle {
            width: parent.width
            height: Style.space(4)
            radius: height / 2
            color: Color.muted
            Rectangle {
              width: parent.width * (root.metric("battery").available ? parseInt(root.metric("battery").text) / 100 : 0)
              height: parent.height
              radius: height / 2
              color: Color.accent
            }
          }
        }
      }
      Ui.PanelSeparator { width: parent.width; foreground: Color.foreground }
      Row {
        width: parent.width
        Repeater {
          model: [{key: "area", label: "Area"}, {key: "minutes", label: "Duration"}, {key: "wetness", label: "Wetness"}]
          delegate: Column {
            required property var modelData
            width: hero.width / 3
            spacing: Style.space(4)
            Label { text: modelData.label; color: Color.muted; font.pixelSize: Style.font.caption }
            Label { text: root.metric(modelData.key).text.replace(" m2", " m²"); font.bold: true }
          }
        }
      }
      Label {
        width: parent.width
        text: root.stale ? "Status is out of date. Refresh before relying on it."
          : root.updatedLabel()
        color: root.stale ? Color.urgent : Color.muted
        font.pixelSize: Style.font.caption
      }
      Label {
        visible: root.status && (!root.metric("error").available || root.metric("error").text !== "None")
        width: parent.width
        text: root.metric("error").available ? root.metric("error").text : "Robot fault status unavailable"
        color: Color.urgent
      }
    }
  }

  Column {
    visible: !root.signedIn || root.hostWidget.selectedDid === ""
    width: parent.width
    spacing: Style.space(12)
    Label {
      width: parent.width
      text: root.signedIn ? "Choose your vacuum" : "A cleaner home, in your words."
      font.pixelSize: Style.font.heading
      font.bold: true
    }
    Label {
      width: parent.width
      text: root.signedIn ? "Select a vacuum in settings to get started." : "Connect your Dreame account to see your vacuum and ask Luna to clean."
      color: Color.muted
    }
    Ui.Button { text: root.signedIn ? "Choose vacuum" : "Connect Dreame"; selected: true; onClicked: root.settingsRequested() }
  }

  Label {
    visible: root.hostWidget && root.hostWidget.errorMessage !== ""
    width: parent.width
    text: root.hostWidget ? root.hostWidget.errorMessage : ""
    color: Color.urgent
  }

  Column {
    visible: root.signedIn && root.hostWidget.selectedDid !== ""
    width: parent.width
    spacing: Style.space(9)
    Label { text: "Voice commands"; font.bold: true; font.pixelSize: Style.font.title }
    Label {
      width: parent.width
      text: "󰍬  Double-press Speak on your remote. Wait for the listening notification, then speak. Press once more to send."
      color: Color.muted
      font.pixelSize: Style.font.caption
    }
    Label {
      width: parent.width
      text: "Ask for cleaning, or say \"stop\" to end the current program and return to base."
      color: Color.muted
      font.pixelSize: Style.font.caption
    }
    Label {
      visible: root.voiceBusy
      width: parent.width
      text: root.hostWidget.voiceStopping ? "Ending cleaning and requesting return to base…" : "Processing your voice command…"
      color: Color.accent
    }
    Label {
      visible: root.voice && root.voice.phase === "failed"
        && (!root.program || root.voice.updatedAt >= root.program.createdAt)
      width: parent.width
      text: root.voice && root.voice.error ? root.voice.error.message : ""
      color: Color.urgent
    }
  }

  Surface {
    visible: root.signedIn && root.program !== null
    implicitHeight: plan.implicitHeight + Style.space(32)
    Column {
      id: plan
      x: Style.space(16)
      y: Style.space(16)
      width: parent.width - Style.space(32)
      spacing: Style.space(10)
      Label {
        width: parent.width
        text: root.phaseLabel()
        font.pixelSize: Style.font.title
        font.bold: true
        color: root.program && (root.program.phase === "failed" || root.program.error) ? Color.urgent : Color.accent
      }
      Label {
        visible: root.program && !!root.program.inputText
        width: parent.width
        text: root.program ? root.program.inputText || "" : ""
        maximumLineCount: 3
        elide: Text.ElideRight
        color: Color.muted
      }
      Label {
        visible: root.active && root.program.stepCount > 0
        width: parent.width
        text: root.program ? (root.program.currentTarget || "Preparing") + " · Task "
          + Math.min(root.program.stepIndex + 1, root.program.stepCount) + " of " + root.program.stepCount : ""
      }
      Ui.Button {
        visible: root.hostWidget && root.hostWidget.canEndProgram
        width: parent.width
        text: root.hostWidget ? root.hostWidget.endProgramLabel : ""
        foreground: Color.urgent
        bordered: true
        enabled: root.hostWidget && !root.hostWidget.busy
        onClicked: root.hostWidget.stopProgram()
      }
      Repeater {
        model: root.program && root.program.packet && root.program.packet.type === "program" ? root.program.packet.jobs : []
        delegate: Column {
          required property var modelData
          required property int index
          width: plan.width
          spacing: Style.space(4)
          Label { width: parent.width; text: (index + 1) + ". " + root.target(modelData); font.bold: true }
          Label { width: parent.width; text: root.method(modelData) }
          Label { width: parent.width; text: root.details(modelData); color: Color.muted; font.pixelSize: Style.font.caption }
        }
      }
      Repeater {
        model: root.program && root.program.packet && root.program.packet.type === "clarification" ? root.program.packet.issues : []
        delegate: Label {
          required property var modelData
          width: plan.width
          text: modelData.question + "\nUse Speak to send a complete request with this detail."
        }
      }
      Label {
        visible: root.program && !!root.program.error
        width: parent.width
        text: root.program && root.program.error ? root.program.error.message : ""
        color: Color.urgent
      }
      Label {
        visible: root.hostWidget && root.hostWidget.programControlMessage !== ""
        width: parent.width
        text: root.hostWidget ? root.hostWidget.programControlMessage : ""
        color: Color.muted
      }
    }
  }
}
