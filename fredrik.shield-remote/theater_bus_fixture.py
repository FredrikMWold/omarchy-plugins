"""Test-owned endpoint; never connects to the user's session bus."""

import json
import os
import sys

from gi.repository import Gio, GLib


SERVICE = "org.home_theater.Remote"
OBJECT = "/org/home_theater/Remote"
INTERFACE = SERVICE
XML = f"""
<node><interface name="{INTERFACE}">
  <property name="Active" type="b" access="read"/>
  <method name="Dispatch"><arg name="action" type="s" direction="in"/></method>
  <method name="Direction">
    <arg name="action" type="s" direction="in"/>
    <arg name="phase" type="s" direction="in"/>
  </method>
  <method name="Raise"/>
</interface></node>
"""


def main():
    address = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    assert address.startswith("unix:abstract=shield-theater-test-"), address
    active = json.loads(sys.argv[1])
    error = sys.argv[2] if len(sys.argv) > 2 else ""
    connection = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    result = connection.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "RequestName", GLib.Variant("(su)", (SERVICE, 4)),
        GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
    assert result.unpack() == (1,), result

    def method(bus, sender, path, interface, name, arguments, invocation):
        print(json.dumps({"method": name, "arguments": arguments.unpack()}), flush=True)
        if error:
            invocation.return_dbus_error(f"{SERVICE}.Error.{error}", "fixture rejected action")
        else:
            invocation.return_value(GLib.Variant("()", ()))

    def property_value(bus, sender, path, interface, name):
        return GLib.Variant("b", active)

    xml = XML
    if len(sys.argv) > 3 and sys.argv[3] == "legacy":
        start = xml.index('  <method name="Direction">')
        end = xml.index("  </method>", start) + len("  </method>")
        xml = xml[:start] + xml[end:]
    connection.register_object(
        OBJECT, Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], method, property_value, None)
    print("ready", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
