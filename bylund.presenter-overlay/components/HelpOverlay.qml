import QtQuick
import qs.Commons
import qs.Ui

// Keyboard cheat sheet. Shown automatically the first time the overlay is
// summoned in a shell session, and on demand with ? / F1 / H afterwards.
Item {
  id: help

  signal dismissed()

  readonly property var sections: [
    {
      title: "Draw",
      rows: [
        { keys: "Drag", action: "Draw with the current tool" },
        { keys: "D / A / R", action: "Pen · arrow · rectangle" },
        { keys: "1 – 4", action: "Pen color: accent, urgent, foreground, muted" },
        { keys: "+ / −", action: "Thicker · thinner pen" },
        { keys: "Wheel", action: "Pen thickness — spotlight size while it is on" },
        { keys: "Shift+Wheel", action: "Pen thickness, always" }
      ]
    },
    {
      title: "Edit",
      rows: [
        { keys: "U / Right-click", action: "Undo the last stroke" },
        { keys: "Ctrl+Y", action: "Redo" },
        { keys: "C", action: "Clear all annotations" }
      ]
    },
    {
      title: "Presenting",
      rows: [
        { keys: "S", action: "Spotlight the cursor" },
        { keys: "P", action: "Click ripples on/off" },
        { keys: "T", action: "Hide the toolbar for a clean recording" },
        { keys: "? / F1 / H", action: "This cheat sheet" },
        { keys: "Esc", action: "Clear everything and close" }
      ]
    }
  ]

  Rectangle {
    anchors.fill: parent
    color: Color.menu.scrim
  }

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    onClicked: help.dismissed()
    onWheel: function(wheel) { wheel.accepted = true }
  }

  BorderSurface {
    id: card

    anchors.centerIn: parent
    width: layout.implicitWidth + contentLeftInset + contentRightInset
    height: layout.implicitHeight + contentTopInset + contentBottomInset
    radius: Style.cornerRadius
    color: Color.menu.background
    borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
    padding: Style.spacing.panelPadding

    MouseArea {
      anchors.fill: parent
      onClicked: {}
    }

    Column {
      id: layout
      anchors.centerIn: parent
      spacing: Style.spacing.lg

      Text {
        text: "Presenter Overlay"
        color: Color.menu.selectedText
        font.family: Style.font.menuFamily
        font.pixelSize: Style.font.heading
      }

      Repeater {
        model: help.sections

        Column {
          required property var modelData

          spacing: Style.spacing.sm

          Text {
            text: modelData.title
            color: Color.menu.text
            opacity: 0.6
            font.family: Style.font.menuFamily
            font.pixelSize: Style.font.caption
          }

          Repeater {
            model: modelData.rows

            Row {
              required property var modelData

              spacing: Style.spacing.lg

              Text {
                width: Style.space(150)
                text: modelData.keys
                color: Color.menu.selectedText
                font.family: Style.font.menuFamily
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                text: modelData.action
                color: Color.menu.text
                font.family: Style.font.menuFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }
        }
      }

      Text {
        text: "Hiding with the hotkey keeps your drawing — Esc throws it away."
        color: Color.menu.text
        opacity: 0.6
        font.family: Style.font.menuFamily
        font.pixelSize: Style.font.caption
      }
    }
  }
}
