import QtQuick
import qs.Commons

// One control in the toolbar pill: a label, the key that triggers it, and the
// active/hover states from the shared Omarchy control tokens.
Rectangle {
  id: chip

  property string label: ""
  property string hint: ""
  property bool active: false

  signal activated()

  implicitWidth: content.implicitWidth + Style.spacing.controlPaddingX * 2
  implicitHeight: Style.spacing.controlHeight
  radius: Style.cornerRadius
  opacity: chip.enabled ? 1 : 0.4

  color: chip.active ? Style.selectedFill : (mouse.containsMouse && chip.enabled ? Style.hoverFill : Style.normalFill)
  border.width: chip.active ? Math.max(1, Style.selectedBorderWidth) : Style.normalBorderWidth
  border.color: chip.active ? Style.selectedBorderColor : Style.normalBorderColor

  Row {
    id: content
    anchors.centerIn: parent
    spacing: Style.spacing.labelGap

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: chip.label
      color: chip.active ? Color.menu.selectedText : Color.menu.text
      font.family: Style.font.menuFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: chip.hint.length > 0
      text: chip.hint
      color: chip.active ? Color.menu.selectedText : Color.menu.text
      opacity: 0.55
      font.family: Style.font.menuFamily
      font.pixelSize: Style.font.caption
    }
  }

  // Item.enabled already gates this MouseArea, so a disabled chip only needs
  // the dimmed look above.
  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: chip.activated()
  }
}
