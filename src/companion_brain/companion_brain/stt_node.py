"""
stt_node.py - Speech-to-text input node for Tales.

What it does:
    Listens to the reSpeaker mic continuously, detects when someone starts
    and stops talking, transcribes each utterance with faster-whisper, and
    publishes the text on /user_input. It is a drop-in replacement for
    text_input_node: claude_bridge_node doesn't know or care which one is
    running.

Data flow:
    reSpeaker mic --> arecord --> VAD (webrtcvad) --> smart endpointing
        --> faster-whisper --> /user_input (String) --> claude_bridge_node

Smart endpointing (how Tales decides you're done talking):
    A fixed silence timer either cuts people off (short) or adds dead air
    to every reply (long). Humans decide a turn is over from meaning, not
    just silence. So this node uses two timers plus a check:

    1. You pause for soft_silence_ms (0.6 s). Listening continues, and
       the transcriber quietly transcribes what you've said so far.
    2. The transcript is checked: does it sound finished?
         Finished ("I'll see you tomorrow.")        -> end the turn now.
         Unfinished ("so I was thinking that", ends in "and", "because",
         a comma, no final punctuation)              -> keep listening.
    3. If you start talking again, the check is thrown away and the
       utterance simply continues.
    4. hard_silence_ms (3 s) of silence always ends the turn, so an
       unfinished-sounding sentence never hangs forever.

    Speed: for utterances up to check_tail_s long, the check transcript
    IS the final transcript, so ending the turn costs no extra
    transcription. Longer utterances only transcribe their last
    check_tail_s for the check (fast), then get one full transcription
    at the end.

Threads:
    capture thread     : reads 30 ms audio frames from arecord nonstop,
                         runs VAD, and runs the endpointing state machine.
    transcriber thread : runs Whisper for both pause checks and final
                         transcripts, and publishes the text.
    ROS spin           : handles /tts_speaking messages from tts_node.
    Capture never waits on Whisper, so the arecord pipe is always drained.

Self-hearing protection (two layers):
    1. The XVF3800 does acoustic echo cancellation, because TTS plays
       through the reSpeaker itself.
    2. tts_node publishes /tts_speaking. While it's True (plus a short
       tail), this node discards mic audio, so Tales never transcribes
       its own voice.

Parameters (override with --ros-args -p name:=value):
    mic_device         : ALSA capture device, pinned by card NAME.
    mic_channel        : which of the reSpeaker's 2 channels to use.
                         Channel 0 is the processed (beamformed, echo
                         cancelled) signal.
    model_size         : faster-whisper model. 'base.en' balances speed and
                         accuracy on the Orin Nano CPU.
    vad_aggressiveness : 0 to 3. Higher rejects more noise but can clip
                         quiet speech.
    soft_silence_ms    : pause length that triggers a "are you done?"
                         check. Lower = snappier, but more checks.
    hard_silence_ms    : pause length that always ends the turn.
    max_utterance_s    : hard cap, so constant noise can't record forever.
    check_tail_s       : how much of the end of the utterance the pause
                         check transcribes.
    min_utterance_ms   : speech shorter than this is dropped (coughs,
                         clicks). Also cuts Whisper's habit of inventing
                         words like "Thank you." on near-silence.
    min_rms            : energy gate. A frame only STARTS an utterance if
                         VAD says speech AND its loudness (RMS) is at
                         least this. 0 disables the gate.
    continue_rms_ratio : once you're talking, frames only need
                         min_rms * this ratio to count as speech
                         (hysteresis). Keeps soft trailing words like
                         "...you know" from being treated as silence.
    log_levels         : when True, logs the room's loudness once a second.
                         Use it to pick min_rms, then turn it off.

Known limitations:
    - No wake word. Tales responds to any speech it hears, including a TV.
    - A pause after a complete sentence ends the turn even if you meant
      to keep going ("I went to the store. [pause] And then..."). That's
      a real ambiguity; fragment merging in the bridge is the follow-up
      if it happens often.
    - Audio spoken while a long utterance gets its final transcription
      is discarded.
"""

import collections  # deque: fixed-size rolling buffers
import queue        # Thread-safe handoff from capture to transcriber
import re           # Word extraction for the completeness check
import subprocess   # Runs arecord
import threading    # Capture and transcriber threads
import time         # Monotonic clock

import numpy as np                  # Audio sample math
import webrtcvad                    # Voice activity detection
from faster_whisper import WhisperModel  # Speech recognition

import rclpy                           # ROS 2 Python client library
from rclpy.node import Node            # Base class for ROS 2 nodes
from std_msgs.msg import Bool, String  # Message types

# ---- Audio format constants ---------------------------------------------
# Whisper and webrtcvad both expect 16 kHz, 16-bit audio.
SAMPLE_RATE = 16000
# webrtcvad only accepts 10, 20, or 30 ms frames. 30 ms = fewer calls.
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples per channel
CHANNELS = 2                                    # reSpeaker records stereo
BYTES_PER_SAMPLE = 2                            # 16-bit = 2 bytes
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * BYTES_PER_SAMPLE  # 1920 bytes

# How long to keep the mic muted after TTS finishes, so the tail end of
# playback (and room echo) isn't picked up.
MUTE_TAIL_S = 0.4

# ---- Completeness check -------------------------------------------------
# Words that almost never end a finished thought in speech. If the
# transcript so far ends with one of these, the speaker is mid-sentence.
CONTINUATION_WORDS = {
    # Conjunctions
    'and', 'but', 'or', 'so', 'because', 'cause', 'cuz', 'if', 'then',
    'than', 'that', 'which', 'who', 'when', 'while', 'where', 'whether',
    'although', 'though', 'unless', 'until', 'since',
    # Fillers
    'um', 'uh', 'erm', 'hmm', 'like', 'basically', 'actually', 'also',
    # Articles and determiners
    'the', 'a', 'an', 'my', 'your', 'our', 'their', 'his', 'her', 'its',
    'this', 'these', 'those', 'some', 'any', 'every',
    # Prepositions
    'to', 'of', 'with', 'for', 'about', 'at', 'in', 'on', 'from', 'into',
    'by', 'as', 'over', 'under', 'between', 'through', 'after', 'before',
    # Subject pronouns and auxiliaries that need something after them
    'i', 'we', 'they', 'is', 'are', 'was', 'were', 'am', 'be', 'been',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'can', 'might', 'must', 'gonna', 'wanna', 'just', 'really',
    'very', 'more', 'most', 'not',
}


def sounds_complete(text):
    """
    Guess whether a transcript is a finished thought.

    Order matters: the last word is checked before punctuation, because
    Whisper often adds a period to a cut-off sentence ("I was thinking
    about." still ends in a preposition, so it's unfinished).

    Empty text counts as complete: the "speech" was probably noise, and
    ending quickly lets it be dropped.
    """
    text = text.strip()
    if not text:
        return True

    # Trailing comma, dash, or ellipsis: explicitly mid-thought.
    if text.endswith((',', '-', '...', '\u2026', ';', ':')):
        return False

    words = re.findall(r"[a-zA-Z']+", text.lower())
    if words and words[-1] in CONTINUATION_WORDS:
        return False

    # A finished sentence ends with terminal punctuation. Whisper adds it
    # when the audio sounds like a sentence ending.
    return text.endswith(('.', '?', '!'))


class STTNode(Node):
    """ROS 2 node that turns speech from the reSpeaker into /user_input text."""

    def __init__(self):
        super().__init__('stt_node')

        # ---- Parameters ----------------------------------------------------
        self.declare_parameter('mic_device', 'plughw:CARD=Array,DEV=0')
        self.declare_parameter('mic_channel', 0)
        self.declare_parameter('model_size', 'base.en')
        self.declare_parameter('vad_aggressiveness', 2)
        self.declare_parameter('soft_silence_ms', 600)
        self.declare_parameter('hard_silence_ms', 3000)
        self.declare_parameter('max_utterance_s', 60)
        self.declare_parameter('check_tail_s', 8)
        self.declare_parameter('min_utterance_ms', 400)
        self.declare_parameter('min_rms', 0)
        self.declare_parameter('continue_rms_ratio', 0.5)
        self.declare_parameter('log_levels', False)

        self.mic_device = self.get_parameter('mic_device').value
        self.mic_channel = self.get_parameter('mic_channel').value
        model_size = self.get_parameter('model_size').value
        vad_level = self.get_parameter('vad_aggressiveness').value
        soft_ms = self.get_parameter('soft_silence_ms').value
        hard_ms = self.get_parameter('hard_silence_ms').value
        max_utterance_s = self.get_parameter('max_utterance_s').value
        check_tail_s = self.get_parameter('check_tail_s').value
        min_utterance_ms = self.get_parameter('min_utterance_ms').value
        self.min_rms = self.get_parameter('min_rms').value
        ratio = self.get_parameter('continue_rms_ratio').value
        self.log_levels = self.get_parameter('log_levels').value

        # Convert time-based settings into frame and sample counts, since
        # the capture loop works one 30 ms frame at a time.
        self.soft_limit = max(1, soft_ms // FRAME_MS)         # ~20 frames
        self.hard_limit = max(self.soft_limit + 1, hard_ms // FRAME_MS)
        self.max_frames = max_utterance_s * 1000 // FRAME_MS  # ~2000 frames
        self.min_speech_frames = max(1, min_utterance_ms // FRAME_MS)
        self.check_tail_samples = int(check_tail_s * SAMPLE_RATE)
        self.continue_rms = self.min_rms * ratio

        # ---- Speech recognition model -------------------------------------
        # device='cpu' + compute_type='int8': the pip build of faster-whisper
        # has no Jetson GPU support, and int8 is the fastest CPU mode.
        self.get_logger().info(f'Loading Whisper model: {model_size}')
        self.model = WhisperModel(model_size, device='cpu', compute_type='int8')

        # ---- Voice activity detector --------------------------------------
        self.vad = webrtcvad.Vad(vad_level)

        # ---- ROS interfaces -----------------------------------------------
        self.publisher = self.create_publisher(String, '/user_input', 10)
        self.create_subscription(
            Bool, '/tts_speaking', self.on_tts_speaking, 10
        )

        # ---- Shared state between threads ---------------------------------
        self.speaking = False      # True while tts_node is playing audio
        self.unmute_at = 0.0       # Monotonic time when the mute tail ends
        self.transcribing = False  # True during a FINAL transcription only
        self.running = True        # Cleared on shutdown to stop threads
        self.jobs = queue.Queue()  # Capture -> transcriber handoff

        # Latest pause-check result, written by the transcriber and read by
        # capture: (check_id, text, complete, covers_all). A single tuple
        # assignment is atomic in Python, so no lock is needed.
        self.check_result = None
        self.check_seq = 0  # Increments per check, so stale results are ignored

        # ---- Start the microphone -----------------------------------------
        # arecord streams raw PCM with no WAV header (-t raw). PulseAudio is
        # disabled on this Jetson, so arecord gets exclusive access.
        self.arecord = subprocess.Popen(
            [
                'arecord', '-q',
                '-D', self.mic_device,
                '-f', 'S16_LE',
                '-r', str(SAMPLE_RATE),
                '-c', str(CHANNELS),
                '-t', 'raw',
            ],
            stdout=subprocess.PIPE,
        )

        threading.Thread(target=self.capture_loop, daemon=True).start()
        threading.Thread(target=self.transcriber_loop, daemon=True).start()

        self.get_logger().info(
            f'STT node ready. Listening on {self.mic_device} '
            f'(check at {soft_ms} ms pause, always end at {hard_ms} ms)'
        )

    # ------------------------------------------------------------------------
    # ROS callback
    # ------------------------------------------------------------------------

    def on_tts_speaking(self, msg):
        """Track whether Tales is talking. Start the mute tail when it stops."""
        if msg.data:
            self.speaking = True
        else:
            self.speaking = False
            self.unmute_at = time.monotonic() + MUTE_TAIL_S

    def is_muted(self):
        """True when mic audio should be ignored."""
        return (
            self.speaking
            or time.monotonic() < self.unmute_at
            or self.transcribing
        )

    # ------------------------------------------------------------------------
    # Capture thread: frames in, endpointing decisions out
    # ------------------------------------------------------------------------

    def capture_loop(self):
        """Read mic frames, detect speech, and decide when each turn ends."""

        # Pre-roll: the last 300 ms before speech is detected, so the first
        # syllable isn't cut off while VAD makes up its mind.
        pre_roll = collections.deque(maxlen=10)

        # Rolling record of the last 10 VAD decisions, used to decide when
        # speech has really started (not just a single noisy frame).
        recent = collections.deque(maxlen=10)

        level_rms = []
        level_vad = []

        triggered = False     # True while inside an utterance
        voiced = []           # Frames collected for the current utterance
        silence_frames = 0    # Consecutive non-speech frames inside it
        pending_check = None  # check_id we're waiting on, or None

        def reset():
            nonlocal triggered, voiced, silence_frames, pending_check
            triggered = False
            voiced = []
            silence_frames = 0
            pending_check = None

        while self.running:
            data = self.arecord.stdout.read(FRAME_BYTES)

            # A short read means arecord exited (device unplugged or busy).
            if len(data) < FRAME_BYTES:
                if self.running:
                    self.get_logger().error(
                        'Mic stream ended. Is another process using the '
                        'device? Check with: fuser -v /dev/snd/*'
                    )
                return

            # While muted, keep draining the pipe but throw the audio away,
            # and reset so a half-finished utterance isn't resumed later.
            if self.is_muted():
                pre_roll.clear()
                recent.clear()
                reset()
                continue

            # Interleaved stereo (L R L R ...): take one channel.
            stereo = np.frombuffer(data, dtype=np.int16)
            mono = stereo[self.mic_channel::CHANNELS]

            # Loudness (RMS). Float math avoids int16 overflow.
            rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
            vad_speech = self.vad.is_speech(mono.tobytes(), SAMPLE_RATE)

            if self.log_levels:
                level_rms.append(rms)
                level_vad.append(vad_speech)
                if len(level_rms) >= 33:
                    self.get_logger().info(
                        f'Level: avg RMS {np.mean(level_rms):.0f}, '
                        f'peak {max(level_rms):.0f}, '
                        f'VAD speech {100 * sum(level_vad) / len(level_vad):.0f}%'
                    )
                    level_rms.clear()
                    level_vad.clear()

            if not triggered:
                # Waiting for speech. Starting needs the full energy gate.
                is_speech = vad_speech and rms >= self.min_rms
                pre_roll.append(mono)
                recent.append(is_speech)

                # Start when 8 of the last 10 frames (~240 ms) are speech.
                if len(recent) == recent.maxlen and sum(recent) >= 8:
                    triggered = True
                    voiced = list(pre_roll)
                    pre_roll.clear()
                    recent.clear()
                    silence_frames = 0
                continue

            # ---- Inside an utterance ----------------------------------------
            # Continuing only needs the lower threshold (hysteresis), so soft
            # trailing words don't start the silence countdown.
            is_speech = vad_speech and rms >= self.continue_rms
            voiced.append(mono)

            if is_speech:
                silence_frames = 0
                pending_check = None  # Speech resumed: any check is stale
            else:
                silence_frames += 1

            # Soft pause reached: ask the transcriber whether it sounds done.
            if silence_frames == self.soft_limit:
                self.check_seq += 1
                pending_check = self.check_seq
                audio = np.concatenate(voiced)
                covers_all = len(audio) <= self.check_tail_samples
                tail = audio[-self.check_tail_samples:]
                self.jobs.put(('check', pending_check, tail, covers_all))

            # Latest check result, if it belongs to this pause.
            result = self.check_result
            ours = (
                pending_check is not None
                and result is not None
                and result[0] == pending_check
            )

            end_reason = None
            final_text = None  # Reusable transcript, if we have one

            if ours and result[2]:
                end_reason = 'sounds finished'
                final_text = result[1] if result[3] else None
            elif silence_frames >= self.hard_limit:
                end_reason = 'long pause'
                if ours and result[3]:
                    final_text = result[1]
            elif len(voiced) >= self.max_frames:
                end_reason = 'max length'

            if end_reason is None:
                continue

            # ---- End of turn ------------------------------------------------
            speech_frames = len(voiced) - silence_frames
            if speech_frames >= self.min_speech_frames:
                audio = np.concatenate(voiced)
                if final_text is None:
                    # Mute right away so new speech isn't half-captured while
                    # the full transcription runs.
                    self.transcribing = True
                self.jobs.put(('final', audio, final_text, end_reason))
            reset()

    # ------------------------------------------------------------------------
    # Transcriber thread: checks and final transcripts
    # ------------------------------------------------------------------------

    def transcribe(self, audio):
        """Run Whisper on int16 audio. Returns (text, seconds taken)."""
        audio_f32 = audio.astype(np.float32) / 32768.0
        start = time.monotonic()
        # beam_size=1 (greedy) is much faster on CPU with little accuracy
        # loss. vad_filter=True trims silence that causes made-up words.
        segments, _ = self.model.transcribe(
            audio_f32,
            language='en',
            beam_size=1,
            vad_filter=True,
        )
        # segments is a lazy generator. Joining it runs the model.
        text = ' '.join(s.text.strip() for s in segments).strip()
        return text, time.monotonic() - start

    def transcriber_loop(self):
        """Handle pause checks and final transcriptions, in order."""
        while self.running:
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue

            if job[0] == 'check':
                _, check_id, audio, covers_all = job
                try:
                    text, elapsed = self.transcribe(audio)
                except Exception as e:
                    self.get_logger().error(f'Pause check failed: {e}')
                    text, elapsed = '', 0.0
                complete = sounds_complete(text)
                self.check_result = (check_id, text, complete, covers_all)
                verdict = 'finished' if complete else 'unfinished, waiting'
                self.get_logger().info(
                    f'Pause check ({elapsed:.1f}s): "{text}" -> {verdict}'
                )
                continue

            # Final transcript.
            _, audio, text, reason = job
            elapsed = 0.0
            try:
                if text is None:
                    text, elapsed = self.transcribe(audio)
            except Exception as e:
                self.get_logger().error(f'Transcription failed: {e}')
                text = ''
            finally:
                self.transcribing = False

            if not text:
                continue

            seconds = len(audio) / SAMPLE_RATE
            source = f'{elapsed:.1f}s to transcribe' if elapsed else 'reused check'
            self.get_logger().info(
                f'Heard ({seconds:.1f}s audio, {source}, ended: {reason}): {text}'
            )
            self.publisher.publish(String(data=text))

    # ------------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------------

    def destroy_node(self):
        """Stop the threads and release the mic before shutting down."""
        self.running = False
        if self.arecord.poll() is None:
            self.arecord.terminate()
        super().destroy_node()


def main(args=None):
    """Entry point, registered in setup.py as 'stt_node'."""
    rclpy.init(args=args)
    node = STTNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():  # Ctrl+C may have already shut rclpy down
            rclpy.shutdown()


if __name__ == '__main__':
    main()
