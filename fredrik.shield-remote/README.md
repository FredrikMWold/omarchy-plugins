# Shield Remote

An Omarchy shell plugin that keeps a triangular NVIDIA Shield remote paired to
this Linux computer and routes its buttons to either this computer or a Shield TV
over Android TV Remote Service v2. No remote firmware changes, Bluetooth
impersonation, ADB, or Home Assistant installation are required.

## Controls

Double-press Menu/settings within 500 ms to change targets. A single Menu press
is reserved and ignored. The bar icon and a desktop notification show the target.

| Button | Computer | Shield |
| --- | --- | --- |
| Direction pad | Focused application's arrow keys | Native direction-pad events |
| Select / Back | Enter / Escape | Native Select / Back |
| Home | Focus existing Stremio window, otherwise launch | Native Shield home screen |
| Play/pause | Unique active MPRIS player | Native media key |
| Rewind / fast-forward | Supported MPRIS seek, -/+10 seconds | Native keys; the Shield app decides the interval |
| Volume | Default computer output, 5% steps | Native Shield volume keys |
| Power / microphone / Netflix | Ignored | Ignored |

Shield presses/releases use the protocol's `START_LONG`/`END_LONG` events.
Changing targets releases any held Shield keys. Offline Shield input is dropped
with an explicit error, never redirected to the computer.

Computer Home always uses Stremio, either its native executable or the installed
`com.stremio.Stremio` Flatpak. Install Stremio separately on the hub. Existing
configuration is migrated without changing the remote, Shield host or credentials.
Actual Stremio window identification and its MPRIS capabilities still need
verification on the hub; Stremio is not installed on the development computer.
Seek failures and multiple-player ambiguity are reported rather than guessed.

## Installation

This is a personal plugin for current Omarchy, including Hyprland's Lua
dispatchers, BlueZ and the graphical-session user manager. The computer must
stay on and within Bluetooth range of the remote to control either target.

Keep this plugin in `~/omarchy-plugins/fredrik.shield-remote` and create/manage
the symlink into `~/.config/omarchy/plugins/fredrik.shield-remote` yourself.
The setup helper does not clone repositories, register plugins, create symlinks
or change the bar layout. Validate and enable the widget yourself with:

```sh
omarchy plugin validate ~/omarchy-plugins/fredrik.shield-remote
omarchy plugin enable fredrik.shield-remote right
```

Use **Setup input / service** in the panel, or run:

```sh
bash ~/omarchy-plugins/fredrik.shield-remote/setup.sh
```

Run this helper in your Omarchy desktop session. It resolves the locked
dependencies using Python 3.12 into private application storage at
`$XDG_DATA_HOME/fredrik.shield-remote/venv` (normally under `~/.local/share`).
It checks for a usable uv, including a project-scoped mise fallback, and asks
before installing missing uv/build tools through Omarchy.

On a new hub, the helper opens Omarchy's Bluetooth panel if the remote is not
known. Hold Home and Back until the remote blinks, wait for
**NVIDIA SHIELD Remote** to appear, then continue setup **without pairing yet**.
The helper identifies the single discovered remote and asks before applying
its input rule and enabling the user service. Only then open Shield Remote,
hold Home + Back until the indicator blinks, and select **Pair remote**.
The regular Bluetooth panel is used for initial discovery, not pairing.
This order prevents its Power button from
being handled as an unfiltered desktop power button during first setup.
The helper does not pair Bluetooth devices or forget existing bonds automatically.

The rule at `/etc/udev/rules.d/71-fredrik-shield-remote.rules` matches only the
selected remote's name/address. It grants the active seat user access with
`uaccess`, sets `LIBINPUT_IGNORE_DEVICE=1`, and removes `power-switch`. The
compositor/logind therefore ignore that physical device even if the backend
stops; only the backend's explicitly forwarded actions reach the desktop.
No input-group membership or access to other keyboards is granted.
The backend requires a matching setup receipt written only after the rule reload
and service activation succeed; a partially completed installation cannot
automatically unblock input. **Setup input / service** disappears only when the
rule, receipt and matching enabled user unit are all present. Missing or outdated
setup remains repairable through the same button or script.

The user service is `omarchy-shield-remote.service`, scoped to the graphical
session. It starts in Computer mode. The backend exclusively captures the
selected remote and rediscovers its event node after reconnects. Re-running
setup restarts the service so updated source and session environment take effect.

**Pair remote** uses BlueZ directly, cancels any stale pairing attempt for only
the selected remote, explicitly unblocks it, pairs/trusts it, and verifies the
saved bond and safe button capture. Progress appears in the panel; pairing has
a 60-second deadline, followed by bounded safety cleanup. A timeout is cancelled
and reported, not left spinning. Stalled pairing/connection attempts for the
selected remote are cancelled before a fresh attempt.
Failed attempts restore the remote's blocked safety state. Use this plugin's
pairing button to retry: the regular Bluetooth panel does not clear that block.

For a stale bond, select **Repair pairing**, read the warning, then explicitly
confirm **Forget bond and pair** with the remote blinking. This removes only
that physical remote's Linux bond, never another device or the Shield TV's LAN
credentials. Ordinary pairing and reconnecting never forget a bond.

After Bluetooth pairing and setup, use **Reconnect remote**. This reconnects
the physical Bluetooth remote to this computer, not the Shield TV. It unblocks/connects the selected device
only if the scoped rule is installed; reconnecting creates an input node with
the new safety properties. The connection action first disconnects any existing
link, so an old input node cannot retain pre-setup permissions.
Wake the remote with an arrow button while connecting. A short-lived discovery
client assists BLE reconnection and stops afterward without stopping other
applications' discovery sessions. Failed connections restore the blocked state.
Bluetooth remains paired to the computer, not Shield.
Use double-Menu to change targets, not Bluetooth pairing mode. If the physical
remote is put back into pairing mode and its stored Linux bond no longer works,
use **Repair pairing** and its confirmation before **Reconnect remote** again. This is
separate from the TV's LAN pairing and does not replace its client credentials.

Capture waits for udev and checks `CURRENT_TAGS`, matching logind's active
power-key policy rather than rejecting historical tags in `TAGS`. Missing or
unsafe properties block the remote and report the observed safety state.

## Shield pairing

Enter the Shield's IP address/hostname in the panel, select **Show pairing code
on TV**, and enter the six-character code displayed by Android TV Remote Service.
The service must be installed/running and reachable over LAN (6466/6467).
Pairing times out after two minutes. Failed codes do not mark a host paired.

Client certificate/key and settings are stored outside the repository in
`$XDG_CONFIG_HOME/fredrik.shield-remote` (normally `~/.config/fredrik.shield-remote`),
with a private directory and 0600 credential/config files. Codes travel through
process stdin and the private user-session Unix socket, not command-line arguments.
No LAN listener is exposed by this plugin.

Wake behavior, Shield volume versus existing IR/CEC settings, and native media
timing need checking on the actual Shield. Power is intentionally not mapped.
The remote's existing IR transmissions are unaffected by the software router.

## Development

```sh
export UV_PROJECT_ENVIRONMENT="${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.shield-remote/venv"
uv sync --project . --locked --no-dev
"$UV_PROJECT_ENVIRONMENT/bin/python" -m unittest -v test_router
bash run.sh status
journalctl --user -u omarchy-shield-remote.service --no-pager
```

The backend accepts a JSON request on stdin:

```sh
printf '%s\n' '{"command":"target","target":"computer"}' | bash run.sh request
```

The native panel wraps this same local API. Private config, sockets and credentials
are never placed in Git. Do not copy the configuration directory to a new hub:
repeat its machine-local input setup and TV pairing there.

To stop routing, stop the user service. The input rule continues to suppress
native remote events. To undo the setup, block/disconnect the physical remote
first, disable/remove the user unit, and remove only this plugin's udev rule
before reloading rules; reconnecting without the rule restores normal HID behavior.
