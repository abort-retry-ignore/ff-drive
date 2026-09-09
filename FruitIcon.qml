import QtQuick
import QtQuick.Shapes
import qs.Commons

Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground

  width: iconSize
  height: iconSize
  implicitWidth: iconSize
  implicitHeight: iconSize

  Shape {
    anchors.fill: parent
    antialiasing: true
    layer.enabled: true
    layer.samples: 4

    // Fruit body
    ShapePath {
      fillColor: root.color
      strokeWidth: 0
      startX: root.width * 0.50
      startY: root.height * 0.22
      PathCubic {
        x: root.width * 0.90; y: root.height * 0.58
        control1X: root.width * 0.86; control1Y: root.height * 0.22
        control2X: root.width * 0.96; control2Y: root.height * 0.42
      }
      PathCubic {
        x: root.width * 0.50; y: root.height * 0.96
        control1X: root.width * 0.86; control1Y: root.height * 0.78
        control2X: root.width * 0.68; control2Y: root.height * 0.96
      }
      PathCubic {
        x: root.width * 0.10; y: root.height * 0.58
        control1X: root.width * 0.32; control1Y: root.height * 0.96
        control2X: root.width * 0.14; control2Y: root.height * 0.78
      }
      PathCubic {
        x: root.width * 0.50; y: root.height * 0.22
        control1X: root.width * 0.04; control1Y: root.height * 0.42
        control2X: root.width * 0.14; control2Y: root.height * 0.22
      }
    }

    // Leaf
    ShapePath {
      fillColor: root.color
      strokeWidth: 0
      startX: root.width * 0.52
      startY: root.height * 0.18
      PathCubic {
        x: root.width * 0.82; y: root.height * 0.04
        control1X: root.width * 0.58; control1Y: root.height * 0.02
        control2X: root.width * 0.72; control2Y: root.height * -0.02
      }
      PathCubic {
        x: root.width * 0.56; y: root.height * 0.26
        control1X: root.width * 0.78; control1Y: root.height * 0.14
        control2X: root.width * 0.64; control2Y: root.height * 0.24
      }
    }
  }

  // Stem
  Rectangle {
    width: Math.max(1.5, root.width * 0.07)
    height: root.height * 0.16
    radius: width / 2
    color: root.color
    x: root.width * 0.50 - width / 2
    y: root.height * 0.06
  }
}
