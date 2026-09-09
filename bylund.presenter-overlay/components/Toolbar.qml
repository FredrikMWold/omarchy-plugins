import QtQuick
import qs.Commons
import qs.Ui

// Bottom-center status pill. Everything the overlay can be in the middle of
// doing is readable here at a glance, and clickable for anyone who has not
// memorised the keymap yet.
BorderSurface {
  id: toolbar

  property string tool: "pen"
  property var pens: []
  property int colorIndex: 0
  property color penColor: Color.foreground
  property int penWidth: 4
  property bool spotlight: false
  property bool ripples: false
  property bool canUndo: false
  property bool canRedo: false

  signal toolPicked(string name)
  signal colorPicked(int index)
  signal spotlightToggled()
  signal ripplesToggled()
  signal undoRequested()
  signal redoRequested()
  signal clearRequested()
  signal helpRequested()
  signal hideRequested()
  signal exitRequested()

  width: row.implicitWidth + contentLeftInset + contentRightInset
  height: row.implicitHeight + contentTopInset + contentBottomInset
  radius: Style.cornerRadius
  color: Color.menu.background
  borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
  padding: Style.spacing.popupPadding

  // Clicks on the pill must not fall through to the drawing canvas underneath.
  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    onClicked: {}
    onWheel: function(wheel) { wheel.accepted = true }
  }

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.spacing.controlGap

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Pen"
      hint: "D"
      active: toolbar.tool === "pen"
      onActivated: toolbar.toolPicked("pen")
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Arrow"
      hint: "A"
      active: toolbar.tool === "arrow"
      onActivated: toolbar.toolPicked("arrow")
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Rect"
      hint: "R"
      active: toolbar.tool === "rect"
      onActivated: toolbar.toolPicked("rect")
    }

    Rectangle {
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(1, Style.space(1))
      height: Style.spacing.controlHeight
      color: Util.alpha(Color.menu.text, 0.25)
    }

    // Pen colors. These are the theme's own roles, so the swatches change with
    // the active Omarchy theme instead of fighting it.
    Row {
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.spacing.sm

      Repeater {
        model: toolbar.pens

        Rectangle {
          required property int index
          required property var modelData

          readonly property bool active: index === toolbar.colorIndex

          width: Style.spacing.controlHeight - Style.spacing.xs * 2
          height: width
          radius: width / 2
          color: modelData
          border.width: active ? Math.max(2, Style.space(2)) : Math.max(1, Style.space(1))
          border.color: active ? Color.menu.selectedText : Util.alpha(Color.menu.text, 0.35)
          scale: active ? 1.15 : 1

          Behavior on scale {
            NumberAnimation { duration: 90; easing.type: Easing.OutQuad }
          }

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: toolbar.colorPicked(index)
          }
        }
      }
    }

    Rectangle {
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(1, Style.space(1))
      height: Style.spacing.controlHeight
      color: Util.alpha(Color.menu.text, 0.25)
    }

    // Live preview of the stroke the pen would lay down right now.
    Item {
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(38)
      height: Style.spacing.controlHeight

      Rectangle {
        anchors.centerIn: parent
        width: parent.width
        height: Math.min(parent.height, Math.max(2, toolbar.penWidth))
        radius: height / 2
        color: toolbar.penColor
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: toolbar.penWidth + " px"
      color: Color.menu.text
      opacity: 0.75
      font.family: Style.font.menuFamily
      font.pixelSize: Style.font.caption
    }

    Rectangle {
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(1, Style.space(1))
      height: Style.spacing.controlHeight
      color: Util.alpha(Color.menu.text, 0.25)
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Spotlight"
      hint: "S"
      active: toolbar.spotlight
      onActivated: toolbar.spotlightToggled()
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Ripples"
      hint: "P"
      active: toolbar.ripples
      onActivated: toolbar.ripplesToggled()
    }

    Rectangle {
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(1, Style.space(1))
      height: Style.spacing.controlHeight
      color: Util.alpha(Color.menu.text, 0.25)
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Undo"
      hint: "U"
      enabled: toolbar.canUndo
      onActivated: toolbar.undoRequested()
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Redo"
      hint: "Ctrl+Y"
      enabled: toolbar.canRedo
      onActivated: toolbar.redoRequested()
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Clear"
      hint: "C"
      enabled: toolbar.canUndo
      onActivated: toolbar.clearRequested()
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Help"
      hint: "?"
      onActivated: toolbar.helpRequested()
    }

    Rectangle {
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(1, Style.space(1))
      height: Style.spacing.controlHeight
      color: Util.alpha(Color.menu.text, 0.25)
    }

    // The overlay covers the bar, so the widget that opened it cannot be
    // clicked again to put it away. These two chips are the mouse-only
    // equivalents of the two ways out: park the drawing, or throw it away.
    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Hide"
      onActivated: toolbar.hideRequested()
    }

    ToolbarChip {
      anchors.verticalCenter: parent.verticalCenter
      label: "Exit"
      hint: "Esc"
      onActivated: toolbar.exitRequested()
    }
  }
}
