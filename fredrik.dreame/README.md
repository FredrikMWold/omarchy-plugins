# Dreame

Machine-specific Omarchy bar plugin for **Android-free Dreamehome cloud access**.
It uses no emulator, Appium, Home Assistant, HTTP listener or permanent service.
The bar owns short-lived Python workers over JSON stdin/stdout; a submitted
cleaning program uses one transient systemd **user** worker.

The protocol was investigated with REA against Dreamehome 2.6.6.3. This
implementation has fixture-backed protocol/storage/IPC coverage. **Password
login, session renewal, device discovery and all fourteen status property reads
have now succeeded against the live cloud on this machine**.
The active saved map resolves all five agreed room labels, and real Luna
responses pass the original contract validator. Native settings, room targeting
and ordered execution have fixture-backed coverage, but **physical cleaning
has not been tested**. A matching APK or accepted API response does not prove
physical cleaning behavior.

## Setup and login

```sh
bash ~/omarchy-plugins/fredrik.dreame/setup.sh
bash ~/omarchy-plugins/fredrik.dreame/login.sh
```

Both are available from the plugin panel. Setup installs the locked Python
dependencies into private storage, and imports app constants from a
checksum-pinned public reference. It uses project-scoped `uv` without changing
global mise configuration; if no usable uv is present, it explicitly asks you
to install it with `omarchy pkg add uv`. Setup does not authenticate or touch
the vacuum.

Login asks for the existing Dreamehome account **region, username/email and
password** in your terminal. Username/password entry is hidden. The password
travels to the worker over stdin, is sent only as the vendor's salted digest
over certificate-verified HTTPS, and is not persisted or put in command
arguments. The session stores the username (needed for app metadata), access
token, refresh token, expiry and a stable random fingerprint.

Use the same region as your existing Dreamehome account: `eu`, `us`, `sg`,
`cn`, `ru` or `kr`. Wrong-region, social-only accounts or interactive
verification challenges may need a different login flow. These errors are
reported; the plugin never scrapes Android credentials, disables certificate
verification, guesses another region or falls back to a hand-written TLS
implementation.

If discovery fails after authentication, the already-saved session remains
available; the wizard reports failure rather than claiming setup completed.

## Use

Select your vacuum under **Settings**. The main dashboard shows its name,
state, battery, mop wetness, last/current area and duration, and robot faults.
Requests and ordered jobs are presented as readable cleaning plans, not JSON.
Account management, device selection, complete status details and guarded
protocol tools are under **Settings**. Unsupported properties retain their
error codes there. The last successful read has a visible age and becomes stale after 90 seconds;
a failed refresh displays an error rather than fabricating data. Status
refreshes every 30 seconds while signed in, or every 10 seconds during a
program. Local program/voice state is polled every two seconds while the panel
is open or a request is active; this does not add cloud polling.

### Cleaning requests

Use the Shield remote's **Speak** gesture described below to submit a request.
The temporary text composer has been removed from both the dashboard and settings.
**Submission authorizes automatic execution after validation, without another confirmation.**
The manual API opt-in below is separate and is not required for this feature.
Requests are limited to 4000 characters. For example:

> Vacuum the kitchen twice, quietly, without mopping.

Short requests such as **“In the kitchen.”** mean routine cleaning of that room:
vacuum then mop with the configured quality-first planning defaults and one pass.
Luna must not interview you for optional settings; explicit method/settings
requests still take precedence. A missing or unknown target, conflicting
instructions or an unsupported operation can still require clarification.

The planner calls the signed-in Copilot CLI with **gpt-6-luna**, no tools,
no MCP servers and no custom instructions, in a temporary private directory.
Only the user request and agreed capability/room labels go to Luna, not account
credentials, device IDs, map geometry or executable API payloads. This uses
your Copilot access and model quota.

The original `~/Work/dreame-local/android/cleaning-program.mjs` remains the
single schema/prompt/validator source; Node and its existing pinned Ajv
dependency are required, but Android and Appium are never started. The complete
version-1 / capabilities-version-2 envelope and caller request ID are validated
without coercion, repair, dropped fields or extra keys. Clarifications are
displayed as questions and send no device-changing commands. Use Speak to send
a complete request containing the requested detail; previous utterances are not
combined into a conversation.

Automatic programs are currently mapped only for `dreame.vacuum.r2532h` (X50).
The supported contract includes whole-home or named-room targets, vacuum/mop/
mop-after-vac, suction and one-shot boost, routes, wetness and washing rules,
room passes, and ordered jobs. Zones have no verified mapping and must produce
clarification. One-shot boost requires 100% battery. Limits are 32 jobs, 64
expanded tasks and eight hours.

The whole plan is checked against the active saved map and required native
settings before the first write. Room arrays become separate ordered native
tasks to preserve their order. Each property write requires readback; final
settings and map identity are checked again before start. Initial active-task
readback also checks settings, stopping if, for example, room-start firmware
rounds the requested wetness. For vacuum-then-mop, Intensive/Deep refer to the
mopping route: Standard is a valid active readback during the vacuum-first
phase only. Settings are checked again when entering a cleaning phase, so the
mopping phase must still report the requested route; a real downgrade still
stops the robot. A following task requires an observed active
task followed by two completed-and-docked observations. Faults, pause,
cancellation, unexpected tasks or map/account changes stop the remaining queue.
Readback errors identify each mismatching setting and its requested/observed
values. The latest check's bounded native settings and startup state are retained
privately as `settingsCheck` in `program.json`, including on success; they are
not included in the panel's public state and contain no account credentials.

**End cleaning and return to dock** cancels remaining tasks, terminates that
worker, attempts native Stop once, then requests return to charge once after
Stop is acknowledged. Before dispatch, **Cancel request** only cancels planning
without moving the robot. An already-stopped dispatched program offers
**Return to dock**, without replaying Stop. Automatic safety stops do not dock.
An acknowledgement is not physical stop/docking verification. Ambiguous starts/stops/docking
are never replayed. Failures do not roll back settings already applied, and an
already-running task may continue after an error or sign-out; inspect the robot.
Other controllers are not atomically excluded: avoid using the phone or another
controller during a program.

The transient worker survives bar reloads but never resumes a previously
claimed program after interruption. Starting another program requires the
robot to be idle and fault-free; no restart or login silently replays a queue.

### Voice requests

With the Shield Remote audio receiver configured, double-press **Speak** in
either Computer or Shield mode. Wait for the listening notification, say your
request, then press Speak **once** to finish and send. Recording cancels at
30 seconds instead of automatically submitting a truncated request.

Dreame accepts a private WAV through:

```sh
bash ~/omarchy-plugins/fredrik.dreame/run.sh audio --audio-file /absolute/path/request.wav
```

**This submits a command, not a transcription preview.** Cleaning requests
follow the validated, automatic-start path. Explicit **"stop"**, **"stop cleaning"**,
**"stop the vacuum"**, **"end the task"** and **"cancel cleaning"** requests use the
same guarded end-and-return-to-dock action as the panel. Polite "please" and
punctuation are accepted, as is an explicit "and return to base/dock" suffix.
This routing happens locally after transcription, without waiting for Luna or
requiring the robot to be idle. Before dispatch it only cancels the queued request,
without movement. Repeating a successfully acknowledged Stop reports the prior
docking request without resending it. With no current program, a finished program, an account/vacuum
mismatch or an ambiguous prior control attempt, it reports an explicit error
instead of guessing or replaying robot commands. It does not stop cleaning started
outside this plugin. Negated, conditional or compound replacement requests such
as "don't stop", "stop after cleaning the kitchen" or "stop and clean the bedroom"
are not treated as immediate Stop commands.

Audio must be a user-owned, mode-0600, single-link regular WAV file with
16 kHz mono signed-16-bit PCM and 0.5–30 seconds of usable signal.

File transcription uses the existing Voxtype configuration and Whisper model,
explicitly forces **local** Whisper, and honors the configured voice-activity
detection setting without enabling it independently of regular dictation. It
never types into an application or uses the clipboard. Other configured engines
are rejected rather than risking cloud speech processing. Only recognized text,
not audio, goes to Luna for cleaning plans; recognized stop requests do not go to
Luna. No speech, transcription errors, account/device changes
and duplicate concurrent intake are explicit failures, with no automatic retry.
The receiver privately copies the audio and removes its copy after transcription;
the sender owns cleanup of the original.

The property-reader accepts an array such as:

```json
[{"siid":2,"piid":1},{"siid":3,"piid":1}]
```

Refresh tokens rotate on renewal, which happens before expiry and once after
an unauthorized **read**. A rejected refresh clears the saved tokens and
requires login again; transient errors do not delete the session. There is no
password fallback or automatic repeated login.

### Guarded commands

Advanced commands are **off by default**, including after each new login.
Manual commands are unavailable while an automatic program is active.
The advanced field takes either of these shapes, with model-verified values:

```text
{"method":"set_properties","params":[{"siid":<service>,"piid":<property>,"value":<scalar>}]}
{"method":"action","params":{"siid":<service>,"aiid":<action>,"in":[<inputs>]}}
```

Enable advanced commands, prepare a preview, inspect its exact target and
payload, then click **Confirm and send once**. Preparing sends no mutation.
Confirmation expires after 90 seconds, is bound to that exact stored payload,
and is consumed **before** dispatch. Changing selection, disabling commands,
cancelling or signing out removes it.

Immediately before dispatch, the worker requires a fresh state read of
standby, charging or charging-completed (`2/1` values `2`, `6`, `13`). This
intentionally excludes pause/resume/dock operations while actively cleaning.
The check is not atomic with the robot: another controller can change state
between read and write. Do not operate competing controllers during a test.

Mutations are never automatically replayed, even after HTTP 401, timeouts or
ambiguous replies. An explicit action acknowledgement is not physical-outcome
verification. Property writes additionally require matching live readback;
failure causes an error, not retry or rollback. Some commands may have taken
effect before an error: inspect live state before preparing another one.

## Storage and scope

All settings and credentials are machine-local:

```text
${XDG_DATA_HOME:-~/.local/share}/fredrik.dreame/
  venv/                 Locked Python environment
  profile.json          Imported app-level protocol constants
  session.json          Renewable account session; never the password
  settings.json         Selected device and advanced-command opt-in
  confirmation.json     At most one expiring command preview
  program.json          Current/last program, validated packet and progress
  program-cancel.json   Cancellation marker bound to a program ID
  program.lock          One worker for the whole program lifetime
  operation.lock        Kernel flock; released even if a worker crashes
  voice.json            Sanitized current/last voice-input outcome
  voice.lock            One audio intake; duplicate input is never queued
```

The directory is mode `0700`; state files are mode `0600`. State writes are
atomic, symlinked/hard-linked/insecure state files are rejected, and an
exclusive operation lock prevents concurrent refresh-token rotations. These
files are **not additionally encrypted**: protect your user account and disk,
and never commit or share them. No credentials belong in `shell.json` or
dotfiles. Sign out removes this plugin's local session; it does not revoke
cloud sessions or sign out your phone.

Local session-state inspection never waits for a network operation. Cloud
reads on separate monitors/workers wait up to 30 seconds for the shared
operation lock instead of racing refresh-token rotation; mutation attempts
fail explicitly if another operation already owns the lock.
The current/last original cleaning request is retained privately
as `inputText` alongside the validated packet so the panel can show what was
requested. It is replaced by the next program, not cleared after planning.
Audio recordings are not retained after normal processing. Signing out removes credentials even
if stopping the worker fails, and invalidates the remaining queue.

Canonical source: `~/omarchy-plugins/fredrik.dreame`.
Installed link: `~/.config/omarchy/plugins/fredrik.dreame`.
The bar entry is in this machine's Git-ignored `shell.json`.

## CLI / JSON IPC

```sh
plugin="$HOME/omarchy-plugins/fredrik.dreame"
bash "$plugin/run.sh" state
printf '%s\n' '{"op":"devices"}' | bash "$plugin/run.sh" request
printf '%s\n' '{"op":"select","deviceId":"YOUR_BOUND_DEVICE_ID"}' | bash "$plugin/run.sh" request
printf '%s\n' '{"op":"status"}' | bash "$plugin/run.sh" request
printf '%s\n' '{"op":"refresh-session"}' | bash "$plugin/run.sh" request
```

Each request is one newline-terminated JSON object, at most 32 KiB. Replies
contain `ok`, `data`/`error`, and sanitized local `state`; failures exit
nonzero. No account tokens or username are returned. IPC is process-local,
not an authenticated LAN API. Device discovery is restricted to this account's
bound vacuums; arbitrary URLs, methods and caller-supplied shards are not
accepted.

```sh
cd ~/omarchy-plugins/fredrik.dreame
"${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.dreame/venv/bin/python" \
  -m unittest discover -p 'test_*.py' -q
omarchy plugin validate "$PWD"
```

## Protocol source and limits

Authentication, header/signing and command-envelope prior art:
[`Tasshack/dreame-vacuum`, commit `f5b8472b8e2e4bb3dff7bc2a81a5fd594eca9585`](https://github.com/Tasshack/dreame-vacuum/blob/f5b8472b8e2e4bb3dff7bc2a81a5fd594eca9585/custom_components/dreame_vacuum/dreame/protocol.py).
The reference's shared password salt, signing/client key and Basic
authorization value were found in the investigated APK. Constants are
generated into private storage rather than embedded in this repository.
Its MIT notice is preserved in `licenses/dreame-vacuum.txt`.

This client uses Python's standard verified TLS, not the reference's custom
TLS/fingerprint impersonation. Server-side compatibility restrictions may
therefore reject login even when the application-level protocol matches.
Such rejection needs investigation, not a certificate-check bypass.

MQTT push notifications, scheduled starts, zone geometry, other device models,
and social/SMS/challenge login are not implemented. Room decoding and native
control bindings are derived from the same pinned reference's `map.py`,
`device.py` and `types.py`, with its MIT attribution preserved. Exact physical
room/settings behavior remains to be verified on the vacuum.
