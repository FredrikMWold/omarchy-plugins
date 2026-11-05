import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SHELL = Path("/usr/share/omarchy/shell")
PLUGINS = ("fredrik.shield-remote", "fredrik.dreame", "fredrik.ryde-nearby")


class BarIconTests(unittest.TestCase):
    def test_custom_icon_geometry(self):
        components = []
        contents = []
        for index, plugin in enumerate(PLUGINS):
            source = (ROOT / plugin / "BarWidget.qml").read_text()
            name = re.search(r"iconComponent: (\w+)", source).group(1)
            component = re.search(
                rf"^  Component \{{\n    id: {name}\n.*?^  \}}",
                source,
                re.MULTILINE | re.DOTALL,
            ).group()
            components.append(component.replace(f"id: {name}", f"id: icon{index}", 1))
            contents.append(
                f'Ui.BarIconContent {{ objectName: "{plugin}"; '
                f"x: {index * 32}; width: 24; height: 24; sourceComponent: icon{index} }}"
            )

        # Exercise the installed shell's Loader and optical sizing without starting
        # the plugins' network/device backends.
        qml = """
import QtQuick
import QtQuick.Window
import Quickshell
import qs.Commons
import qs.Ui as Ui
import "Scooter" as Scooter

Window {
  visible: true
  width: 96
  height: 32
  Item {
    id: root
    anchors.fill: parent
    property var bar: QtObject {
      property color foreground: "white"
      property color background: "black"
      property string fontFamily: "monospace"
    }
    property var backendState: null
    property string target: "computer"
    property string voicePhase: "idle"
    property string errorMessage: ""
    property var program: null
    property var deviceStatus: null
    property bool programActive: false
    property bool voiceBusy: false
    property color indicatorColor: "#f97316"
    property bool indicatorHollow: false
    COMPONENTS
    CONTENTS
    Timer {
      interval: 500
      running: true
      onTriggered: {
        for (var child of root.children) {
          if (!child.objectName) continue
          var icon = child.children[0].item
          console.log("BAR_ICON_RESULT " + JSON.stringify({
            plugin: child.objectName,
            width: icon.width,
            height: icon.height,
            expected: child.sourceSize,
            bodyWidth: icon.children[0].width,
            bodyHeight: icon.children[0].height,
            indicatorSize: icon.children[1].width * child.contentScale
          }))
        }
        Qt.quit()
      }
    }
  }
}
"""
        qml = qml.replace("COMPONENTS", "\n".join(components))
        qml = qml.replace("CONTENTS", "\n".join(contents))
        qml = qml.replace("ScooterIcon {", "Scooter.ScooterIcon {")
        with tempfile.TemporaryDirectory(prefix="omarchy-icon-test-") as directory:
            path = Path(directory)
            for name in ("Commons", "Ui"):
                (path / name).symlink_to(SHELL / name, target_is_directory=True)
            (path / "Scooter").symlink_to(ROOT / "fredrik.ryde-nearby", target_is_directory=True)
            (path / "shell.qml").write_text(qml)
            result = subprocess.run(
                ["quickshell", "--no-color", "--path", str(path)],
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        measurements = [
            json.loads(line.split("BAR_ICON_RESULT ", 1)[1])
            for line in result.stdout.splitlines()
            if "BAR_ICON_RESULT " in line
        ]
        self.assertEqual(len(measurements), len(PLUGINS), result.stdout)
        indicator_size = measurements[0]["indicatorSize"]
        for measurement in measurements:
            with self.subTest(plugin=measurement["plugin"]):
                self.assertEqual(measurement["width"], measurement["expected"], measurement)
                self.assertEqual(measurement["height"], measurement["expected"], measurement)
                self.assertGreater(measurement["bodyWidth"], 0, measurement)
                self.assertGreater(measurement["bodyHeight"], 0, measurement)
                self.assertAlmostEqual(
                    measurement["indicatorSize"],
                    indicator_size,
                    delta=indicator_size * 0.1,
                    msg=measurement,
                )


if __name__ == "__main__":
    unittest.main()
