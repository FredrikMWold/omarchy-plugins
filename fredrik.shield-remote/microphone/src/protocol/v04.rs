//! ATVV v0.4 protocol implementation.

use anyhow::Result;

use super::types::*;
use super::Protocol;
use crate::adpcm::AdpcmDecoder;

// v0.4 CTL payload minimum lengths (including opcode byte).
const AUDIO_SYNC_MIN_LEN: usize = 3; // opcode + frame_num(2)
const CAPS_RESP_MIN_LEN: usize = 9; // opcode + version(2) + codecs(2) + frame_size(2) + bytes_per_char(2)
const MIC_OPEN_ERROR_MIN_LEN: usize = 3; // opcode + error_code(2)

/// v0.4 audio frame header: seq(2) + padding(1) + predictor(2) + step_index(1).
const FRAME_HEADER_SIZE: usize = 6;

/// ATVV v0.4 protocol implementation.
pub struct ProtocolV04 {
    pub(crate) selected_codec: Option<Codec>,
    pub(crate) audio_frame_size: AudioFrameSize,
    decoder: AdpcmDecoder,
    partial_frame: Vec<u8>,
    fragment_size: usize,
}

impl Default for ProtocolV04 {
    fn default() -> Self {
        Self {
            selected_codec: None,
            audio_frame_size: AudioFrameSize::DEFAULT_V04,
            decoder: AdpcmDecoder::new(),
            partial_frame: Vec::new(),
            fragment_size: 0,
        }
    }
}

/// Parse a v0.4 CAPS_RESP payload. Returns None if data is too short.
///
/// v0.4 layout: [opcode(1), version(2), codecs(2), frame_size(2), bytes_per_char(2)]
pub(crate) fn parse_caps_resp_payload(data: &[u8]) -> Option<Capabilities> {
    if data.len() < CAPS_RESP_MIN_LEN {
        return None;
    }
    let version_wire = u16::from_be_bytes([data[1], data[2]]);
    let version = ProtocolVersion::from_wire(version_wire).unwrap_or(ProtocolVersion::V0_4);
    // codecs_supported is 2 bytes in v0.4; only low byte has values
    if data[3] != 0x00 {
        tracing::trace!(
            "v0.4 CAPS_RESP: ignoring non-zero codecs high byte: 0x{:02x}",
            data[3]
        );
    }
    let codecs = Codecs::from_bits_truncate(data[4]);
    let frame_size = u16::from_be_bytes([data[5], data[6]]);
    if (frame_size as usize) < FRAME_HEADER_SIZE {
        tracing::warn!(
            frame_size,
            min = FRAME_HEADER_SIZE,
            "v0.4 CAPS_RESP: frame_size smaller than header size"
        );
    }
    Some(Capabilities {
        version,
        codecs,
        interaction_model: InteractionModel::OnRequest,
        audio_frame_size: AudioFrameSize(frame_size),
    })
}

impl ProtocolV04 {
    pub fn new() -> Self {
        Self::default()
    }
}

impl Protocol for ProtocolV04 {
    fn version(&self) -> ProtocolVersion {
        ProtocolVersion::V0_4
    }

    fn mic_open_cmd(&self) -> Vec<u8> {
        let codec_val = u8::from(
            self.selected_codec
                .expect("codec must be negotiated before mic_open"),
        );
        vec![u8::from(TxOpcode::MicOpen), 0x00, codec_val]
    }

    fn mic_close_cmd(&self, _stream_id: StreamId) -> Vec<u8> {
        // v0.4: no stream_id
        vec![u8::from(TxOpcode::MicClose)]
    }

    fn keepalive_cmd(&self, _stream_id: StreamId) -> Vec<u8> {
        // v0.4: no MIC_EXTEND, fall back to MIC_OPEN
        tracing::debug!("v0.4 keepalive: re-sending MIC_OPEN (no MIC_EXTEND support)");
        self.mic_open_cmd()
    }

    fn parse_ctl(&self, data: &[u8]) -> CtlEvent {
        if data.is_empty() {
            return CtlEvent::Unknown(data.to_vec());
        }
        let opcode = match CtlOpcode::try_from(data[0]) {
            Ok(op) => op,
            Err(_) => return CtlEvent::Unknown(data.to_vec()),
        };
        match opcode {
            CtlOpcode::AudioStop => {
                // v0.4: no payload, synthesize default reason
                CtlEvent::AudioStop {
                    reason: AudioStopReason::MicClose,
                }
            }
            CtlOpcode::AudioStart => {
                // v0.4: no payload, synthesize defaults
                CtlEvent::AudioStart {
                    reason: AudioStartReason::MicOpen,
                    codec: self.selected_codec.unwrap_or(Codec::Adpcm8kHz),
                    stream_id: StreamId::MIC_OPEN,
                }
            }
            CtlOpcode::StartSearch => CtlEvent::StartSearch,
            CtlOpcode::AudioSync => {
                // v0.4: [opcode, frame_num_hi, frame_num_lo]
                if data.len() >= AUDIO_SYNC_MIN_LEN {
                    let seq = u16::from_be_bytes([data[1], data[2]]);
                    CtlEvent::AudioSync(AudioSyncData::FrameNum { seq })
                } else {
                    CtlEvent::Unknown(data.to_vec())
                }
            }
            CtlOpcode::CapsResp => match parse_caps_resp_payload(data) {
                Some(caps) => CtlEvent::CapsResp(caps),
                None => CtlEvent::Unknown(data.to_vec()),
            },
            CtlOpcode::MicOpenError => {
                if data.len() >= MIC_OPEN_ERROR_MIN_LEN {
                    let code = u16::from_be_bytes([data[1], data[2]]);
                    CtlEvent::MicOpenError(MicOpenErrorCode::from(code))
                } else {
                    CtlEvent::Unknown(data.to_vec())
                }
            }
        }
    }

    fn on_caps_resp(&mut self, caps: &Capabilities) -> Result<Codec> {
        let codec = super::negotiate_codec(caps.codecs)?;
        self.selected_codec = Some(codec);
        self.audio_frame_size = caps.audio_frame_size;
        self.reset_audio();
        Ok(codec)
    }

    fn decode_audio(&mut self, data: &[u8]) -> Option<AudioFrame> {
        let expected = self.audio_frame_size.0 as usize;
        if expected < FRAME_HEADER_SIZE || data.is_empty() {
            return None;
        }
        let assembled;
        let data = if data.len() == expected {
            if !self.partial_frame.is_empty() {
                tracing::warn!("Discarding incomplete v0.4 frame before a full notification");
                self.partial_frame.clear();
                self.fragment_size = 0;
            }
            data
        } else {
            if self.partial_frame.len() + data.len() > expected {
                tracing::warn!("Discarding oversized fragmented v0.4 audio frame");
                self.partial_frame.clear();
                self.fragment_size = 0;
                return None;
            }
            if self.partial_frame.is_empty() {
                self.fragment_size = data.len();
            }
            self.partial_frame.extend_from_slice(data);
            if self.partial_frame.len() < expected {
                if data.len() != self.fragment_size {
                    tracing::warn!("Discarding truncated v0.4 frame at its final fragment");
                    self.partial_frame.clear();
                    self.fragment_size = 0;
                }
                return None;
            }
            assembled = std::mem::take(&mut self.partial_frame);
            self.fragment_size = 0;
            assembled.as_slice()
        };

        // Parse header: seq(2) + id(1) + predictor(2) + step_index(1)
        let seq = u16::from_be_bytes([data[0], data[1]]);
        let predictor = i16::from_be_bytes([data[3], data[4]]);
        let step_index = data[5];

        // Reset decoder from frame header (v0.4: per-frame reset)
        self.decoder.reset(predictor, step_index);

        // Decode ADPCM bytes
        let decoded = self.decoder.decode_bytes(&data[6..]);

        // Build sample array: predictor + decoded
        let mut samples = Vec::with_capacity(1 + decoded.len());
        samples.push(predictor);
        samples.extend_from_slice(&decoded);

        Some(AudioFrame {
            seq,
            codec: self
                .selected_codec
                .expect("codec must be negotiated before decode"),
            samples,
        })
    }

    fn reset_audio(&mut self) {
        self.partial_frame.clear();
        self.fragment_size = 0;
    }

    fn on_audio_sync(&mut self, sync: &AudioSyncData) {
        // v0.4: just note the frame number for gap detection
        // (no decoder state in v0.4 AUDIO_SYNC)
        match sync {
            AudioSyncData::FrameNum { seq } => {
                tracing::trace!(seq, "v0.4 AUDIO_SYNC: frame number");
            }
            AudioSyncData::Full { .. } => {
                tracing::warn!("v0.4 AUDIO_SYNC: unexpected Full sync data (v0.4 should only produce FrameNum)");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── Command encoding tests ──────────────────────────────────────

    #[test]
    fn test_mic_open_cmd_8khz() {
        let mut p = ProtocolV04::new();
        p.selected_codec = Some(Codec::Adpcm8kHz);
        assert_eq!(p.mic_open_cmd(), vec![0x0C, 0x00, 0x01]);
    }

    #[test]
    fn test_mic_open_cmd_16khz() {
        let mut p = ProtocolV04::new();
        p.selected_codec = Some(Codec::Adpcm16kHz);
        assert_eq!(p.mic_open_cmd(), vec![0x0C, 0x00, 0x02]);
    }

    #[test]
    fn test_mic_close_cmd_ignores_stream_id() {
        let p = ProtocolV04::new();
        assert_eq!(p.mic_close_cmd(StreamId::MIC_OPEN), vec![0x0D]);
        assert_eq!(p.mic_close_cmd(StreamId::ANY), vec![0x0D]);
    }

    #[test]
    fn test_keepalive_is_mic_open() {
        let mut p = ProtocolV04::new();
        p.selected_codec = Some(Codec::Adpcm8kHz);
        assert_eq!(p.keepalive_cmd(StreamId::MIC_OPEN), p.mic_open_cmd());
    }

    // ── CTL parsing tests ───────────────────────────────────────────

    #[test]
    fn test_parse_audio_start() {
        let mut p = ProtocolV04::new();
        p.selected_codec = Some(Codec::Adpcm8kHz);
        match p.parse_ctl(&[0x04]) {
            CtlEvent::AudioStart {
                reason,
                codec,
                stream_id,
            } => {
                assert_eq!(reason, AudioStartReason::MicOpen);
                assert_eq!(codec, Codec::Adpcm8kHz);
                assert_eq!(stream_id, StreamId::MIC_OPEN);
            }
            other => panic!("expected AudioStart, got {:?}", other),
        }
    }

    #[test]
    fn test_parse_audio_stop() {
        let p = ProtocolV04::new();
        match p.parse_ctl(&[0x00]) {
            CtlEvent::AudioStop { reason } => {
                assert!(matches!(reason, AudioStopReason::MicClose));
            }
            other => panic!("expected AudioStop, got {:?}", other),
        }
    }

    #[test]
    fn test_parse_start_search() {
        let p = ProtocolV04::new();
        assert!(matches!(p.parse_ctl(&[0x08]), CtlEvent::StartSearch));
    }

    #[test]
    fn test_parse_audio_sync() {
        let p = ProtocolV04::new();
        // v0.4 AUDIO_SYNC: opcode + frame_num(2)
        match p.parse_ctl(&[0x0A, 0x00, 0x05]) {
            CtlEvent::AudioSync(AudioSyncData::FrameNum { seq }) => {
                assert_eq!(seq, 5);
            }
            other => panic!("expected AudioSync::FrameNum, got {:?}", other),
        }
    }

    #[test]
    fn test_parse_caps_resp() {
        let p = ProtocolV04::new();
        // v0.4 CAPS_RESP: [0x0B, version(2), codecs_supported(2), bytes/frame(2), bytes/char(2)]
        let data = [0x0B, 0x00, 0x04, 0x00, 0x01, 0x00, 0x86, 0x00, 0x14];
        match p.parse_ctl(&data) {
            CtlEvent::CapsResp(caps) => {
                assert_eq!(caps.version, ProtocolVersion::V0_4);
                assert!(caps.codecs.contains(Codecs::ADPCM_8KHZ));
                assert!(!caps.codecs.contains(Codecs::ADPCM_16KHZ));
                assert_eq!(caps.interaction_model, InteractionModel::OnRequest);
                assert_eq!(caps.audio_frame_size.0, 0x0086);
            }
            other => panic!("expected CapsResp, got {:?}", other),
        }
    }

    #[test]
    fn test_parse_mic_open_error() {
        let p = ProtocolV04::new();
        match p.parse_ctl(&[0x0C, 0x0F, 0x01]) {
            CtlEvent::MicOpenError(code) => {
                assert!(matches!(code, MicOpenErrorCode::InvalidCodec));
            }
            other => panic!("expected MicOpenError, got {:?}", other),
        }
    }

    #[test]
    fn test_parse_unknown() {
        let p = ProtocolV04::new();
        assert!(matches!(p.parse_ctl(&[0xFF, 0x01]), CtlEvent::Unknown(_)));
    }

    // ── Codec negotiation tests ─────────────────────────────────────

    #[test]
    fn test_on_caps_resp_selects_best_codec() {
        let mut p = ProtocolV04::new();
        // Remote supports both codecs
        let caps = Capabilities {
            version: ProtocolVersion::V0_4,
            codecs: Codecs::ADPCM_8KHZ | Codecs::ADPCM_16KHZ,
            interaction_model: InteractionModel::OnRequest,
            audio_frame_size: AudioFrameSize(134),
        };
        let codec = p.on_caps_resp(&caps).unwrap();
        assert_eq!(codec, Codec::Adpcm16kHz); // prefers best
    }

    #[test]
    fn test_on_caps_resp_8khz_only() {
        let mut p = ProtocolV04::new();
        let caps = Capabilities {
            version: ProtocolVersion::V0_4,
            codecs: Codecs::ADPCM_8KHZ,
            interaction_model: InteractionModel::OnRequest,
            audio_frame_size: AudioFrameSize(134),
        };
        let codec = p.on_caps_resp(&caps).unwrap();
        assert_eq!(codec, Codec::Adpcm8kHz);
    }

    // ── Audio frame decoding tests ──────────────────────────────────

    #[test]
    fn test_shield_fragmented_notifications_decode_one_pcm_frame() {
        let caps = crate::protocol::parse_caps_resp(&[
            0x0B, 0x00, 0x04, 0x00, 0x01, 0x00, 0x86, 0x00, 0x14,
        ])
        .unwrap();
        let (mut protocol, _) = crate::protocol::create_protocol(&caps).unwrap();
        let mut frame = [0; 134];
        frame[..6].copy_from_slice(&[0x00, 0x07, 0x00, 0x03, 0xE8, 0x00]);

        let decoded: Vec<_> = frame
            .chunks(20)
            .filter_map(|fragment| protocol.decode_audio(fragment))
            .collect();

        assert_eq!(decoded.len(), 1);
        assert_eq!(decoded[0].seq, 7);
        assert_eq!(decoded[0].codec, Codec::Adpcm8kHz);
        assert_eq!(decoded[0].samples, vec![1000; 257]);
    }

    #[test]
    fn test_truncated_fragmented_frame_does_not_corrupt_the_next_frame() {
        let caps = crate::protocol::parse_caps_resp(&[
            0x0B, 0x00, 0x04, 0x00, 0x01, 0x00, 0x86, 0x00, 0x14,
        ])
        .unwrap();
        let (mut protocol, _) = crate::protocol::create_protocol(&caps).unwrap();
        let mut frame = [0; 134];
        frame[..6].copy_from_slice(&[0x00, 0x07, 0x00, 0x03, 0xE8, 0x00]);
        let mut decoded = Vec::new();
        for (index, fragment) in frame.chunks(20).enumerate() {
            if index != 2 {
                decoded.extend(protocol.decode_audio(fragment));
            }
        }
        frame[1] = 8;
        for fragment in frame.chunks(20) {
            decoded.extend(protocol.decode_audio(fragment));
        }

        assert_eq!(decoded.len(), 1);
        assert_eq!(decoded[0].seq, 8);
        assert_eq!(decoded[0].samples, vec![1000; 257]);
    }

    #[test]
    fn test_stream_reset_discards_partial_audio_before_the_next_session() {
        let caps = crate::protocol::parse_caps_resp(&[
            0x0B, 0x00, 0x04, 0x00, 0x01, 0x00, 0x86, 0x00, 0x14,
        ])
        .unwrap();
        let (mut protocol, _) = crate::protocol::create_protocol(&caps).unwrap();
        let mut frame = [0; 134];
        frame[..6].copy_from_slice(&[0x00, 0x07, 0x00, 0x03, 0xE8, 0x00]);
        assert!(protocol.decode_audio(&frame[..20]).is_none());
        protocol.reset_audio();
        frame[1] = 8;
        let decoded: Vec<_> = frame
            .chunks(20)
            .filter_map(|fragment| protocol.decode_audio(fragment))
            .collect();

        assert_eq!(decoded.len(), 1);
        assert_eq!(decoded[0].seq, 8);
        assert_eq!(decoded[0].samples, vec![1000; 257]);
    }

    #[test]
    fn test_decode_audio_frame() {
        let mut p = ProtocolV04::new();
        p.selected_codec = Some(Codec::Adpcm8kHz);
        p.audio_frame_size = AudioFrameSize(134);
        // Build a minimal 134-byte frame with known header
        let mut frame = vec![0u8; 134];
        frame[0] = 0x00;
        frame[1] = 0x07; // seq = 7
                         // frame[2] = 0x00; // id/padding
                         // predictor = 0, step_index = 0
        let result = p.decode_audio(&frame);
        assert!(result.is_some());
        let af = result.unwrap();
        assert_eq!(af.seq, 7);
        assert_eq!(af.samples.len(), 257); // 1 predictor + 256 decoded
    }

    #[test]
    fn test_decode_audio_wrong_size() {
        let mut p = ProtocolV04::new();
        p.audio_frame_size = AudioFrameSize(134);
        assert!(p.decode_audio(&[0u8; 100]).is_none());
    }

    // ── parse_caps_resp_payload tests ───────────────────────────────

    #[test]
    fn test_parse_caps_resp_payload_valid() {
        // v0.4 CAPS_RESP: [0x0B, version(2), codecs(2), frame_size(2), bytes_per_char(2)]
        let data: &[u8] = &[0x0B, 0x00, 0x04, 0x00, 0x01, 0x00, 0x86, 0x00, 0x14];
        let caps = parse_caps_resp_payload(data).unwrap();
        assert_eq!(caps.version, ProtocolVersion::V0_4);
        assert_eq!(caps.codecs, Codecs::ADPCM_8KHZ);
        assert_eq!(caps.audio_frame_size, AudioFrameSize(134));
        assert_eq!(caps.interaction_model, InteractionModel::OnRequest);
    }

    #[test]
    fn test_parse_caps_resp_payload_too_short() {
        let data: &[u8] = &[0x0B, 0x00, 0x04];
        assert!(parse_caps_resp_payload(data).is_none());
    }

    #[test]
    fn test_parse_ctl_empty() {
        let p = ProtocolV04::new();
        match p.parse_ctl(&[]) {
            CtlEvent::Unknown(data) => assert!(data.is_empty()),
            other => panic!("expected Unknown for empty CTL, got {:?}", other),
        }
    }

    #[test]
    fn test_on_caps_resp_no_common_codec() {
        let mut p = ProtocolV04::new();
        let caps = Capabilities {
            version: ProtocolVersion::V0_4,
            codecs: Codecs::empty(), // no codecs supported
            interaction_model: InteractionModel::OnRequest,
            audio_frame_size: AudioFrameSize(134),
        };
        assert!(p.on_caps_resp(&caps).is_err());
    }
}
