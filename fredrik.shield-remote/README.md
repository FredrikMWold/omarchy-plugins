# Shield Remote

An Omarchy shell plugin that keeps a triangular NVIDIA Shield remote paired to
this Linux computer and routes its buttons to either this computer or a Shield TV
over Android TV Remote Service v2. No remote firmware changes, Bluetooth
impersonation, ADB, or Home Assistant installation are required.

## Controls

Double-press Menu/settings within 500 ms to change targets. A single Menu press
is reserved and ignored.
The bar icon and a desktop notification show the target.

With a Sony soundbar configured, every target selection also selects **HDMI1
for Shield** or **HDMI2 for Computer**. This includes double-Menu, panel buttons,
the bar's right-click shortcut, and **Open Home Theater**.
Volume Up/Down adjusts only the soundbar in either target, one step per press.
Holding a volume button repeats at a bounded rate without replaying stale
queued repeats after a slow network request. No volume keys are forwarded
to the computer or Shield while a soundbar is configured.

| Button | Computer | Shield |
| --- | --- | --- |
| Direction pad | Active Theater directions; Left/Right tap seeks 10 seconds with controls hidden or timeline focused, hold accelerates a preview and release commits; prior Stremio / other-app arrows otherwise | Native direction-pad events |
| Select / Back | Active Theater confirm/back; prior Stremio selection/history / other-app Enter/Escape otherwise | Native Select / Back |
| Home | Raise existing Home Theater session, or launch `home-theater --fullscreen` once; with a TV output configured, bring its owned window fullscreen onto that monitor's current workspace using `hyprctl` | Native Shield home screen |
| Play/pause | Active Theater `play-pause`; prior unique MPRIS player otherwise | Native media key |
| Rewind / fast-forward | Active Theater seek, -/+10 seconds; prior supported MPRIS seek otherwise | Native keys; the Shield app decides the interval |
| Volume | Soundbar, one-step adjustments; default computer output in 5% steps if no soundbar is configured | Soundbar, one-step adjustments; native Shield volume keys if no soundbar is configured |
| Power | Ignored (never sleeps or shuts down this computer) | Toggle Shield sleep / wake |
| Microphone/Search | Toggle Omarchy's built-in dictation using the Shield microphone | Button ignored; use the app's on-screen microphone as described below |
| Netflix | Ignored | Ignored |

In Computer mode, press Microphone once to start dictation and again to stop
and transcribe, just like Omarchy's Super+Ctrl+X shortcut. It temporarily selects
the Shield microphone as the system default and uses the existing Voxtype
transcription and output settings, including when Home Theater has focus;
it no longer dispatches Theater Search. Holding the button, duplicate presses
without a release, and releasing it do not toggle again. Voxtype must be
installed and running; command failures use the router's existing error
notification and panel reporting, never a Search or keyboard fallback.

Shield presses/releases use the protocol's `START_LONG`/`END_LONG` events.
Power instead sends one `POWER`/`SHORT` per press, including when held, so it
toggles sleep/wake without opening the long-press power menu.
Changing targets releases any held Shield keys. Offline Shield input is dropped
with an explicit error, never redirected to the computer.

Outside an active Theater session, Stremio navigation uses its real Linux accessibility controls (AT-SPI), not
cursor increments, screenshot recognition, or guessed click coordinates.
Left/right prefers controls in the same row; up/down prefers the same column.
Stremio draws its native focus highlight and scrolls an off-screen selection
into view. Select sends Enter to the focused control. With no selection, the
first Select only establishes focus rather than activating an arbitrary item.
Held directions repeat at a bounded rate without accumulating slow focus calls.
Accessible dialogs and popup menus constrain navigation to their own controls.
Back dismisses those overlays with Escape, otherwise uses Stremio's Backspace
history shortcut. Other applications keep their original
keyboard controls. Missing/ambiguous accessibility information produces an
explicit error, never a silent mouse fallback.

The geometry approach also appears in [Stremio's native gamepad
navigation](https://github.com/Stremio/stremio-web/blob/development/src/services/GamepadNavigation/useContentGamepadNavigation.tsx).
AT-SPI lets this remote focus those controls without setting up a virtual
gamepad, a privileged input daemon, or uinput permissions.

Computer routing queries the app-owned user-session endpoint:
`org.home_theater.Remote`, object `/org/home_theater/Remote`, same interface.
`Active` is a typed, read-only boolean, not a truthy string. While true, buttons
call `Dispatch(s)` with exact lower-case up/down/confirm/back/
play-pause/seek-backward/seek-forward. Left/Right instead call the additive
`Direction(s action, s phase)` method with the actual input phase:
`press`, `repeat`, `release`, or `cancel`. This lets the app distinguish a tap
from accelerated hold scrubbing without issuing repeated video seeks. Other
visible player controls retain horizontal selection. The app commits a
scrub only on a real release, not on a repeat timeout.
The selected Shield remote does not advertise hardware auto-repeat. While a
horizontal button remains physically held, the capture worker checks the
kernel's held-key state every 500 ms and refreshes Direction liveness when no
phase was sent recently. This does not seek or accelerate the preview itself;
the app's monotonic hold duration does that. A missing held key sends no
heartbeat and never invents a release; only a real key-up commits. An unreadable
or disconnected device cancels the capture and hold explicitly.
Query failures and active dispatch errors
remain explicit: **no injected keys, AT-SPI, global MPRIS or other-player fallback**.
Service availability/focus does not establish physical Bluetooth acceptance.
Inactive Theater preserves the prior other-app navigation/media behavior.
Microphone uses dictation independently of Theater ownership or focus.
A held direction cannot cross active/inactive routing boundaries and is blocked
until a fresh press after a dispatch failure or focus-route change. Computer
direction repeats with no currently pressed key are dropped after input cleanup.
Horizontal holds are pinned to the application's unique DBus owner so they
cannot cross an app restart. A target change, input disconnect, shutdown,
focus-route change or rejected repeat cancels the held direction without
seeking. A later physical release cannot revive that cancelled hold.
The Direction signature is negotiated once per unique app owner. A still-running
older Home Theater keeps its existing one-shot routing until reopened, with a
router journal notice; malformed introspection or a rejected Direction call
remains an error and never falls back to another player or injected keys.

Home first checks `NameHasOwner` and calls `Raise()` on the existing session.
If absent, it launches `[executable, "--fullscreen"]` from the service's PATH, with
`~/.local/bin/home-theater` as a machine-local fallback, then waits up to 12 seconds
for ownership and raises it. Launches are serialized; a live timed-out launch
stays tracked rather than duplicating on another Home. Early exit includes
application stderr in the error. No Stremio/Flatpak launch or compositor window
guess remains in Home. New sessions request fullscreen so the app's existing
first-run output setup is not bypassed. Existing sessions first receive `Raise()`,
preserving browsing/playback context and pause.
When the machine-local **TV output** is configured (for example `HDMI-A-1`),
Home additionally uses `hyprctl` to move the existing integrated Home Theater
window to that monitor's currently active workspace, center it when floating,
explicitly set fullscreen (never toggle it), and focus it. This avoids Qt/Wayland
activation returning success while the window stays out of view, including stale
off-screen floating positions.
The window is identified by the DBus owner's process ID and exact app class,
not by title or whichever window happens to be focused. A new window gets up to
3 seconds to map. Missing/ambiguous windows, disconnected TV outputs, rejected
commands, or failure to actually focus a fullscreen, mapped, unhidden window with
its center inside the TV's scaled bounds report an explicit error. No unrelated
window or workspace is moved. With no TV output configured, the existing
app-owned `Raise()` behavior remains. The panel's **Open Home Theater** also selects
Computer using the existing Sony input policy; `open_stremio` remains a
compatibility request alias, now with Home Theater semantics.

This machine-local plugin source is reached by its existing installed symlink;
no dotfiles move or new symlink is required. QML hot reload does **not** reload
the long-running Python router. An authorized
restart loads new Python code. Successful target selection now checkpoints only
the selected target in the existing private runtime directory (0600); startup
restores it before any worker/input capture, without a Sony input reassertion.
With no prior checkpoint, Computer remains the default. Invalid saved state
fails closed rather than falling back to Computer. Receiver selection errors
cannot overwrite the prior target; a checkpoint write failure retains the prior
mode and attempts to restore its Sony input, reporting rollback failure explicitly.

For the first upgrade from the currently running old router, use its existing
read-only socket status to checkpoint **current Shield** before restarting:

```sh
cd ~/omarchy-plugins/fredrik.shield-remote
set -o pipefail
~/.local/share/fredrik.shield-remote/venv/bin/python router.py checkpoint-target |
  ~/.local/share/fredrik.shield-remote/venv/bin/python -c \
  'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s == {"ok": True, "target": "shield", "checkpointed": True} else 1)' &&
systemctl --user restart omarchy-shield-remote.service &&
~/.local/share/fredrik.shield-remote/venv/bin/python router.py status
```

Do this only after the reviewed app/plugin changes are installed, with no
concurrent target selection between checkpoint and restart. A failed checkpoint
or unexpected mode aborts the command before restart. No target request, Sony
input change, Bluetooth pairing, service replacement, drop-in or dotfiles change
is involved in the upgrade.

Verify status remains `target: shield`, `running: true`, input safely captured,
`input.waiting_for_release: false`, and Shield connected (allow bounded startup
reconnect time). On each capture/regrab, buttons already held—and presses while
waiting—are suppressed until all are released. The panel reports “release held
buttons”; only fresh input resumes. This prevents held Menu/Home/media/Power
from being replayed after restart. Software fixtures do not prove physical
reconnect or native focus, and no actual restart was executed in these tests.

Private software validation (no live remote endpoint, Bluetooth or receiver):

```sh
cd ~/omarchy-plugins/fredrik.shield-remote
TMPDIR="$PWD" ~/.local/share/fredrik.shield-remote/venv/bin/python \
  -m unittest test_router test_navigation test_home_theater test_restart test_microphone test_tv_voice
omarchy plugin validate ~/omarchy-plugins/fredrik.shield-remote
```

The test-owned DBus daemon uses an abstract socket and a project-local private
configuration without service activation. A GI fixture exports the public
contract; tests exercise actual `Router.key` and real `busctl`, covering errors,
typed booleans, duplicate suppression, focus-route repeat blocking, early stderr
and bounded launch readiness. This is not physical Home/search/media/reconnect,
native activation, TV/audio/readability or idle continuity acceptance.

## Remote microphone audio

The plugin includes its own [Rust microphone daemon](microphone/README.md),
exposing **NVIDIA Shield Remote Microphone** as a selectable PipeWire
audio input. It uses the remote's actual microphone over ATVV v0.4, with 8 kHz
mono ADPCM audio. Capture opens when an application connects to this input and
closes when its capture clients disconnect. The source disappears when the remote
disconnects and is recreated after reconnection.

The plugin's Microphone button saves the current default input, temporarily
selects `atvvoice-shield-remote`, and starts the existing Voxtype daemon. Voxtype
must use its normal `audio.device = "default"` setting. The prior input is
restored when recording stops, including keyboard stop/cancel and the recording
time limit, without waiting for transcription. A manual input change during
recording is preserved instead of overwritten. Keyboard-started dictation keeps
using the user's normal default.

Other applications following the system default may also use the remote during
this temporary switch. The router stores the prior input privately in its runtime
directory, cancels its recording and restores the input on shutdown, and recovers
the saved input after a router crash/restart. Failed starts restore the input;
an unavailable Shield source produces an explicit error rather than recording
the headset. Select the Shield input directly in another application's settings
to use it independently of this button.

The plugin builds the locked source in `microphone/` and installs its executable
to `$XDG_DATA_HOME/fredrik.shield-remote/bin/atvvoice` (normally under
`~/.local/share`). Cargo build output stays in the same private application
storage under `cargo-target/`, outside the shell's hot-reloaded plugin tree.
No separate ATVVoice repository, submodule, download or global binary is needed.
Upstream attribution and the MIT license are preserved alongside the source,
including the local SHIELD fragment-reassembly fixes.

The plugin owns `atvvoice-shield-remote.service`. It starts with the graphical
session, is restricted to the remote selected in the plugin's private
configuration, and uses SIGINT for a clean microphone close on shutdown.
The source name stays `atvvoice-shield-remote`, so app selections and dictation
input restoration keep working. Button routing remains a separate process;
stopping it does not interrupt an application's independent microphone capture.
No Bluetooth pairing, button mappings, soundbar routing or dotfiles management
is changed by a microphone-only rebuild.

To rebuild and reinstall only the microphone, without restarting the button
router or changing its target:

```sh
cd ~/omarchy-plugins/fredrik.shield-remote
bash run.sh install-microphone
```

The full setup helper performs this step automatically. Reinstallation is
supported; a failed build leaves the installed binary and user units untouched.
The original machine-local standalone SHIELD service is migrated in place, while
unrelated or symlinked services are refused. The old `~/.local/bin/atvvoice` is
not removed because another application could use it.

### Shield TV app voice search

In **Shield mode**, select the microphone icon inside YouTube's search screen
(or another app that requests Android TV Remote Service voice input). The app's
existing listening prompt requests an audio session, and the plugin feeds the
physical remote microphone into that exact session. It does **not** send
`SEARCH`, `VOICE_ASSIST`, or other keys to open Google Assistant, nor transcribe
on the computer or inject text into a different search screen.

The TV receives 8 kHz, signed 16-bit mono PCM from the named
`atvvoice-shield-remote` PipeWire input in full 20 KiB packets. This Shield accepts
smaller 8 KiB streaming packets without an explicit error but does not recognize
the speech; retain the full-packet buffering even though the library permits a
smaller minimum. Capture never changes the system default
input and explicitly disables source fallback/reconnection, so a missing remote
cannot silently record the headset. No recorded audio or recognized query is
saved or logged by the plugin.

Only a TV voice request while the physical remote is safely captured in Shield
mode starts capture. It ends when the TV closes its session, the target changes,
either connection is lost, or the router stops. Each request has a 30-second
safety limit; after the limit, select the app's microphone again if needed.
The panel shows **TV microphone: streaming remote audio** while active. Capture
errors use the existing panel, notification and journal reporting.

`tv_voice.py` is the narrow connection/protocol adapter for app-initiated voice
requests that `androidtvremote2` otherwise ignores. It preserves the library's
pairing, key routing and reconnect behavior, and uses its existing PCM voice
encoding. The dependency is pinned to `0.3.2` because the adapter extends its
internal protocol factory; keep the local TLS protocol tests when upgrading it.
The adapted connection code's Apache-2.0 license is preserved in
`licenses/androidtvremote2-LICENSE`.

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

Use **Setup remote** in the panel, or run:

```sh
bash ~/omarchy-plugins/fredrik.shield-remote/setup.sh
```

Run this helper in your Omarchy desktop session. It resolves the locked
dependencies using Python 3.12 into private application storage at
`$XDG_DATA_HOME/fredrik.shield-remote/venv` (normally under `~/.local/share`).
It checks for a usable uv, including a project-scoped mise fallback, and asks
before installing missing uv/build tools through Omarchy. The embedded microphone
also needs Rust/Cargo, Clang, pkg-config and the PipeWire/D-Bus development
libraries; setup offers `omarchy pkg add rust clang pkgconf pipewire dbus`
only when these prerequisites are missing.
It also checks system Python's `python-gobject` and `at-spi2-core` accessibility
bindings and asks before installing them through Omarchy. The small navigation
helper uses system Python; the Bluetooth/Shield backend retains its private
Python 3.12 environment. No additional kernel-device permissions are required.

On a new hub, the helper opens Omarchy's Bluetooth panel if the remote is not
known. Hold Home and Back until the remote blinks, wait for
**NVIDIA SHIELD Remote** to appear, then continue setup **without pairing yet**.
The helper identifies the single discovered remote and asks before applying
its input rule and enabling both user services. Only then open Shield Remote,
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
automatically unblock input. **Setup remote** disappears only when the rule,
receipt, executable microphone binary and both matching enabled user units are
present. Missing or outdated setup remains repairable through the same button
or script.

The button-router user service is `omarchy-shield-remote.service`, scoped to the
graphical session. It restores its checkpointed target, defaulting to Computer
only when no checkpoint exists. The backend exclusively captures the
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
timing need checking on the actual Shield. Sleep/wake requires the Shield's
Android TV Remote Service to remain reachable while asleep.
The remote's existing IR transmissions are unaffected by the software router.

## Soundbar input and volume

Enter the Sony soundbar's IP address or hostname under **Soundbar input and volume**
in the panel, then select **Save soundbar**. Leave it empty to disable automatic
input switching and restore the original target-specific volume controls.
The host is private machine-local configuration, not plugin
source or shared dotfiles. Existing configurations migrate with this feature
disabled and preserve the remote, Shield pairing, and TV output.

The HT-ZF9 is supported through Sony's local Audio Control API on port 10000.
The plugin uses `curl` to call `setPlayContent` (version 1.2), with
`extInput:hdmi?port=1` for Shield and `extInput:hdmi?port=2` for Computer.
No cloud account, Home Assistant, or additional Python package is required.
The request bypasses HTTP proxies, has a five-second network deadline, and
must receive an explicit API success before changing the control target.
Connection errors, HTTP failures, rejected commands, and invalid responses
are reported in the panel, desktop notification, and service log; routing stays
on the previous target. Selecting the current target again reasserts its input.
Service startup does not select an input until a target is explicitly selected.
Volume uses `setAudioVolume` (version 1.1), with relative `+1`/`-1` adjustments.
The soundbar handles its own volume limits, and there is no cached volume to
overwrite changes made with another remote or app. Key releases do not change
volume. Held-button repeats wait at least 120 ms after the previous successful
adjustment; queued repeats older than 250 ms are discarded. A new button press
is not throttled. Soundbar volume works even when the Shield's LAN connection is
offline. Failed volume commands are explicitly reported, never redirected to
computer or Shield volume controls.
Sound settings, Shield pairing, and TV display configuration are unchanged.

## Development

```sh
export UV_PROJECT_ENVIRONMENT="${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.shield-remote/venv"
uv sync --project . --locked --no-dev
"$UV_PROJECT_ENVIRONMENT/bin/python" -m unittest -v test_router
"$UV_PROJECT_ENVIRONMENT/bin/python" -m unittest -v test_microphone
"$UV_PROJECT_ENVIRONMENT/bin/python" -m unittest -v test_tv_voice
/usr/bin/python -m unittest -v test_navigation
cargo test --locked --release --manifest-path microphone/Cargo.toml \
  --target-dir "${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.shield-remote/cargo-target"
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

The microphone installer tests use temporary application/configuration storage
and fake external build/service commands; they never modify the live user
services, Bluetooth state or microphone selection. The Rust suite covers the
preserved audio and protocol behavior.
TV voice tests use an actual loopback TLS connection with the Android TV protobuf
handshake and a fixture microphone process. They cover app-requested PCM payloads
in exact 20 KiB packets without Search/Assistant keys, Computer-mode rejection, remote-only capture,
TV-end/target-change/disconnect cleanup, capture failures and the safety limit.
They do not establish physical TV speech recognition.

To stop routing, stop the router user service. The input rule continues to suppress
native remote events. To undo the setup, block/disconnect the physical remote
first, disable/remove both plugin user units, and remove only this plugin's udev rule
before reloading rules; reconnecting without the rule restores normal HID behavior.
