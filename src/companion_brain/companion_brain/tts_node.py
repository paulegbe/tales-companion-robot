"""
tts_node.py - Text-to-speech output node for Tales.

What it does:
    Listens on /companion_speech for sentences from claude_bridge_node,
    turns each one into speech, and plays it through the reSpeaker.

Data flow:
    claude_bridge_node --(/companion_speech: String, one per sentence)
        --> tts_node --> speaker

Engines (pick with -p engine:=kokoro or -p engine:=piper):
    kokoro : Kokoro-82M on the Jetson GPU. Much more natural voice.
             Runs in a separate worker process (kokoro_worker.py) inside
             its own venv, because Kokoro needs numpy 2.x and ROS Humble's
             cv_bridge needs numpy 1.x. Benchmarked on the Orin Nano:
             ~0.5 s first sentence, synthesis ~4x faster than playback.
    piper  : Piper on the CPU. Robotic but light and dependable.

    Fallback: if the Kokoro worker won't start, or dies and can't be
    restarted, the node switches to Piper and keeps talking. A voice
    downgrade beats silence.

Why sentences, not whole replies:
    The bridge streams Claude's reply and publishes each sentence the
    moment it's complete. Speaking sentence 1 while sentences 2 and 3 are
    still being written is what makes Tales start talking fast.

Pipeline (two threads, so there are no gaps between sentences):
    ROS callback     : queues incoming text immediately, never blocks.
    synth thread     : text -> engine -> sox -> WAV file, queued for playback.
    playback thread  : plays WAV files in order.

Audio steps:
    1. The engine writes a mono WAV (Piper 22050 Hz, Kokoro 24000 Hz).
    2. sox converts to 48 kHz stereo, the only format the reSpeaker plays
       correctly. -G (guard) prevents clipping during conversion.
    3. aplay sends it straight to the reSpeaker. PulseAudio is disabled,
       so the node gets exclusive, direct access to the device.

Publishes:
    /tts_speaking (Bool): True when playback starts, False once every
    queued sentence has played. stt_node uses this to stop listening while
    Tales talks, so it never transcribes its own voice.

Parameters (override with --ros-args -p name:=value):
    engine             : 'kokoro' or 'piper'.
    audio_device       : ALSA output device, pinned by card NAME.
    voice_model        : Piper .onnx voice (its .onnx.json must sit next to it).
    kokoro_dir         : folder holding the Kokoro model, voices, and venv.
    kokoro_python      : Python inside the Kokoro venv.
    kokoro_model       : Kokoro model file name, inside kokoro_dir.
    kokoro_voices      : voices file name, inside kokoro_dir.
    kokoro_voice       : which Kokoro voice to speak with, e.g. af_heart.
    kokoro_speed       : 1.0 is normal. 0.9 is calmer, 1.1 is brisker.
    kokoro_provider    : 'cuda' (GPU) or 'cpu'.
    kokoro_gpu_mem_mb  : cap on Kokoro's GPU memory pool (shared 8 GB).
"""

import json        # Worker protocol
import os          # Path handling
import queue       # Thread-safe queues between pipeline stages
import shutil      # Deleting each sentence's temp folder
import subprocess  # Runs the Kokoro worker, sox, and aplay
import tempfile    # Per-sentence scratch folders
import threading   # Synth and playback threads
import time        # Synthesis timing
import wave        # WAV writer used by Piper

import rclpy
from rclpy.node import Node
from std_msgs.msg import Bool, String

KOKORO_DIR = os.path.expanduser('~/companion_ws/voices/kokoro')

# How long to wait for the Kokoro worker to load and warm up.
WORKER_START_TIMEOUT_S = 60


class EngineError(Exception):
    """The engine itself is broken (not just one bad sentence)."""


# ----------------------------------------------------------------------------
# Engines: each one turns text into a mono WAV file at out_path.
# ----------------------------------------------------------------------------

class PiperEngine:
    name = 'piper'

    def __init__(self, voice_path, logger):
        # Imported here so Kokoro mode never loads Piper unless it has to.
        from piper import PiperVoice
        logger.info(f'Loading Piper voice: {voice_path}')
        self.voice = PiperVoice.load(voice_path)

    def synth(self, text, out_path):
        with wave.open(out_path, 'wb') as wav_file:
            self.voice.synthesize_wav(text, wav_file)

    def close(self):
        pass


class KokoroEngine:
    """Client for kokoro_worker.py, running in the Kokoro venv."""
    name = 'kokoro'

    def __init__(self, python, model, voices, voice, speed, provider,
                 gpu_mem_mb, logger):
        self.voice = voice
        self.speed = speed
        self.logger = logger
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'kokoro_worker.py')
        for path in (python, worker, model, voices):
            if not os.path.exists(path):
                raise EngineError(f'Missing: {path}')

        self.cmd = [
            python, worker,
            '--model', model, '--voices', voices, '--voice', voice,
            '--provider', provider, '--gpu-mem-mb', str(gpu_mem_mb),
        ]
        self.proc = None
        self.start()

    def start(self):
        """Launch the worker and wait until the model is loaded and warm."""
        self.logger.info('Starting Kokoro worker (loading model)...')
        # stderr is inherited, so worker warnings show in this terminal.
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        msg = self._read(timeout=WORKER_START_TIMEOUT_S)
        if not msg.get('ready'):
            self.close()
            raise EngineError(f'Kokoro worker failed: {msg.get("error")}')
        self.logger.info(f'Kokoro ready on {msg.get("provider")}, voice {self.voice}')

    def _read(self, timeout=30):
        """Read one protocol reply, with a timeout so a hang can't freeze TTS."""
        result = {}

        def reader():
            line = self.proc.stdout.readline()
            if line:
                try:
                    result['msg'] = json.loads(line)
                except json.JSONDecodeError:
                    result['msg'] = {'ok': False, 'ready': False,
                                     'error': f'bad reply: {line.strip()}'}

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        if 'msg' in result:
            return result['msg']
        if self.proc.poll() is not None:
            raise EngineError(f'Kokoro worker exited (code {self.proc.returncode})')
        raise EngineError(f'Kokoro worker timed out after {timeout}s')

    def synth(self, text, out_path):
        if self.proc is None or self.proc.poll() is not None:
            raise EngineError('Kokoro worker is not running')
        request = {'text': text, 'voice': self.voice,
                   'speed': self.speed, 'out': out_path}
        try:
            self.proc.stdin.write(json.dumps(request) + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise EngineError(f'Kokoro worker pipe broke: {e}')
        msg = self._read()
        if not msg.get('ok'):
            # One bad sentence, not a dead engine.
            raise RuntimeError(msg.get('error', 'unknown Kokoro error'))

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # Worker exits when stdin closes
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


# ----------------------------------------------------------------------------
# Node
# ----------------------------------------------------------------------------

class TTSNode(Node):
    """Speaks every sentence published on /companion_speech, in order."""

    def __init__(self):
        super().__init__('tts_node')

        # ---- Parameters ----------------------------------------------------
        self.declare_parameter('engine', 'kokoro')
        self.declare_parameter('audio_device', 'plughw:CARD=Array,DEV=0')
        self.declare_parameter(
            'voice_model',
            os.path.expanduser('~/companion_ws/voices/en_US-lessac-medium.onnx'),
        )
        self.declare_parameter('kokoro_dir', KOKORO_DIR)
        self.declare_parameter('kokoro_python', '')  # Default: <dir>/.venv-gpu
        self.declare_parameter('kokoro_model', 'kokoro-v1.0.fp16.onnx')
        self.declare_parameter('kokoro_voices', 'voices-v1.0.bin')
        self.declare_parameter('kokoro_voice', 'af_heart')
        self.declare_parameter('kokoro_speed', 1.0)
        self.declare_parameter('kokoro_provider', 'cuda')
        self.declare_parameter('kokoro_gpu_mem_mb', 1024)

        p = lambda name: self.get_parameter(name).value  # noqa: E731
        self.audio_device = p('audio_device')
        self.piper_voice_path = p('voice_model')

        # ---- Load the engine ------------------------------------------------
        self.engine = None
        if p('engine') == 'kokoro':
            kdir = os.path.expanduser(p('kokoro_dir'))
            python = p('kokoro_python') or os.path.join(kdir, '.venv-gpu/bin/python')
            try:
                self.engine = KokoroEngine(
                    python=python,
                    model=os.path.join(kdir, p('kokoro_model')),
                    voices=os.path.join(kdir, p('kokoro_voices')),
                    voice=p('kokoro_voice'),
                    speed=float(p('kokoro_speed')),
                    provider=p('kokoro_provider'),
                    gpu_mem_mb=int(p('kokoro_gpu_mem_mb')),
                    logger=self.get_logger(),
                )
            except EngineError as e:
                self.get_logger().error(f'{e}. Falling back to Piper.')
        if self.engine is None:
            self.engine = PiperEngine(self.piper_voice_path, self.get_logger())

        # ---- Pipeline state --------------------------------------------------
        self.text_queue = queue.Queue()  # callback -> synth thread
        self.play_queue = queue.Queue()  # synth thread -> playback thread

        # Sentences received but not yet finished playing. When it drops to
        # 0, Tales is done talking and the mic can reopen.
        self.pending = 0
        self.pending_lock = threading.Lock()
        self.speaking = False

        # ---- ROS interfaces ------------------------------------------------
        self.speaking_pub = self.create_publisher(Bool, '/tts_speaking', 10)
        self.create_subscription(String, '/companion_speech', self.on_speech, 10)

        # ---- Threads -------------------------------------------------------
        threading.Thread(target=self.synth_loop, daemon=True).start()
        threading.Thread(target=self.play_loop, daemon=True).start()

        self.get_logger().info(
            f'TTS node ready. Engine: {self.engine.name}, output: {self.audio_device}'
        )

    # ------------------------------------------------------------------------
    # ROS callback: queue and return immediately
    # ------------------------------------------------------------------------

    def on_speech(self, msg):
        text = msg.data.strip()
        if not text:
            return
        with self.pending_lock:
            self.pending += 1
        self.text_queue.put(text)

    # ------------------------------------------------------------------------
    # Synth thread: text -> playable WAV
    # ------------------------------------------------------------------------

    def synthesize(self, text, raw_path):
        """Run the engine, recovering from a dead Kokoro worker once."""
        try:
            self.engine.synth(text, raw_path)
            return
        except EngineError as e:
            self.get_logger().error(f'{e}')

        # Kokoro died. Try one restart, then fall back to Piper.
        if isinstance(self.engine, KokoroEngine):
            try:
                self.engine.close()
                self.engine.start()
                self.engine.synth(text, raw_path)
                return
            except EngineError as e:
                self.get_logger().error(f'Restart failed ({e}). Switching to Piper.')
                self.engine.close()
                self.engine = PiperEngine(self.piper_voice_path, self.get_logger())
        self.engine.synth(text, raw_path)

    def synth_loop(self):
        while True:
            text = self.text_queue.get()

            # Each sentence gets its own temp folder. The playback thread
            # deletes it after playing.
            tmpdir = tempfile.mkdtemp(prefix='tts_')
            raw_path = os.path.join(tmpdir, 'raw.wav')
            out_path = os.path.join(tmpdir, 'out.wav')

            try:
                start = time.monotonic()
                self.synthesize(text, raw_path)
                synth_s = time.monotonic() - start
                self.get_logger().info(
                    f'[{self.engine.name}] {synth_s:.2f}s synth: {text}'
                )

                # Convert to 48 kHz stereo for the reSpeaker.
                subprocess.run(
                    ['sox', '-G', raw_path, '-r', '48000', '-c', '2', out_path],
                    check=True,
                )
                self.play_queue.put((out_path, tmpdir))

            except Exception as e:
                # Skip this sentence but keep the pipeline alive.
                self.get_logger().error(f'Synthesis failed: {e}')
                shutil.rmtree(tmpdir, ignore_errors=True)
                self.sentence_done()

    # ------------------------------------------------------------------------
    # Playback thread: WAV -> speaker, in order
    # ------------------------------------------------------------------------

    def play_loop(self):
        while True:
            out_path, tmpdir = self.play_queue.get()

            # Mute the mic before the first sound comes out.
            if not self.speaking:
                self.speaking = True
                self.speaking_pub.publish(Bool(data=True))

            try:
                subprocess.run(
                    ['aplay', '-q', '-D', self.audio_device, out_path],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                self.get_logger().error(f'Playback failed: {e}')
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                self.sentence_done()

    def sentence_done(self):
        """Count one sentence finished. Reopen the mic when none remain."""
        with self.pending_lock:
            self.pending -= 1
            done = self.pending <= 0
        if done and self.speaking:
            self.speaking = False
            self.speaking_pub.publish(Bool(data=False))

    def destroy_node(self):
        """Shut the Kokoro worker down cleanly so it doesn't hold GPU memory."""
        if self.engine:
            self.engine.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = TTSNode()
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
