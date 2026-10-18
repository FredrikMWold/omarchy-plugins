# Shield microphone daemon

This is the plugin's embedded ATVVoice daemon, maintained here rather than in a
separate checkout. The button router and Rust audio daemon remain separate
processes so applications can use the microphone independently of button routing.

Based on [b0o/ATVVoice v0.2.0](https://github.com/b0o/ATVVoice/tree/v0.2.0),
commit `319c379b34535897e118cd66d41f0310bdfd691a`, with the local SHIELD
compatibility changes preserved: ATVV v0.4 fragment reassembly, truncated-frame
rejection, and partial-frame reset at microphone start/stop. SHIELD sends each
134-byte audio frame as six 20-byte fragments and a 14-byte tail.

Each PipeWire capture starts with empty pending PCM and flushed PipeWire buffers.
BLE receive timestamps travel through both audio queues, so delayed decoder
output from an earlier capture is rejected instead of entering a new recording.
The cutoff uses each capture's start time, not fixed-duration speech trimming.
This applies to ordinary dictation and generic voice-command capture.

The upstream MIT license and copyright are preserved in [LICENSE](LICENSE).
Distribution packaging metadata was omitted; this daemon is installed only by
the plugin. Dependencies remain pinned by `Cargo.lock`.

From the plugin directory, run `bash run.sh install-microphone` to rebuild,
install and restart only the microphone. Normal `setup.sh` installs both the
microphone and button router. See the [plugin README](../README.md) for paths,
dependencies, lifecycle and test commands.

The opt-in PipeWire regression uses a separate virtual source and synthetic
samples, never the physical microphone or a configured command receiver. It
checks partial-buffer reuse, queued audio, delayed decoder delivery and a silent
second recording, then removes its source and temporary WAVs:

```sh
cargo build --locked --release --example capture-isolation --target-dir target
python tests/capture_isolation.py target/release/examples/capture-isolation
```
