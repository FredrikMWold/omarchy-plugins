import QtQuick
import qs.Commons

Item {
  id: root

  property color strokeColor: Color.foreground
  property real strokeWidth: Math.max(1.4, width * 0.095)

  Canvas {
    id: canvas
    anchors.fill: parent

    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    onPaint: {
      var context = getContext("2d")
      context.reset()
      context.strokeStyle = root.strokeColor.toString()
      context.lineWidth = root.strokeWidth
      context.lineCap = "round"
      context.lineJoin = "round"
      context.beginPath()
      context.arc(width * 0.22, height * 0.74, width * 0.105, 0, Math.PI * 2)
      context.moveTo(width * 0.325, height * 0.74)
      context.lineTo(width * 0.65, height * 0.74)
      context.lineTo(width * 0.77, height * 0.25)
      context.moveTo(width * 0.68, height * 0.19)
      context.lineTo(width * 0.89, height * 0.19)
      context.moveTo(width * 0.65, height * 0.74)
      context.arc(width * 0.77, height * 0.74, width * 0.105, Math.PI, Math.PI * 3)
      context.stroke()
    }
  }

  onStrokeColorChanged: canvas.requestPaint()
}
