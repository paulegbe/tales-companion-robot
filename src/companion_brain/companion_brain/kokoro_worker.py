#!/usr/bin/env python3
"""
kokoro_worker.py - Kokoro speech synthesis in its own process.

Why a separate process:
    Kokoro needs numpy 2.x. ROS Humble's cv_bridge needs numpy 1.x. They
    can't share one Python, so Kokoro lives in its own venv
    (~/companion_ws/voices/kokoro/.venv-gpu) and tts_node talks to it
    through this worker. It also means a TTS crash can't take down the
    ROS node, and the engine's dependencies can't break vision again.

    This file must NOT import rclpy or anything from ROS. It runs under
    the venv's Python, which has no ROS on its path.

Protocol (one JSON object per line):
    startup  worker -> node : {"ready": true, "provider": "..."}
                           or {"ready": false, "error": "..."}
    request  node -> worker : {"text": "...", "voice": "af_heart",
                               "speed": 1.0, "out": "/tmp/.../raw.wav"}
    reply    worker -> node : {"ok": true, "synth_s": 0.5, "audio_s": 2.1}
                           or {"ok": false, "error": "..."}

    stdout carries ONLY protocol lines. Everything else (onnxruntime
    warnings, errors) goes to stderr, which shows in tts_node's terminal.
"""

import argparse
import json
import os
import sys
import time

# ---- Protect the protocol channel ------------------------------------------
# Libraries (onnxruntime, espeak) sometimes print to stdout, which would
# corrupt the JSON stream. Keep a private copy of the real stdout for
# protocol replies, then point file descriptor 1 at stderr so any stray
# output, even from C code, goes to the log instead.
_proto_fd = os.dup(1)
os.dup2(2, 1)
PROTO = os.fdopen(_proto_fd, 'w', buffering=1)  # Line-buffered
sys.stdout = sys.stderr


def reply(obj):
    PROTO.write(json.dumps(obj) + '\n')
    PROTO.flush()


def load(model, voices, provider, gpu_mem_mb):
    """Build the ONNX session explicitly, so we control where it runs."""
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    if provider == 'cuda':
        cuda_opts = {'arena_extend_strategy': 'kSameAsRequested'}
        if gpu_mem_mb:
            # GPU and CPU share the Jetson's 8 GB. Capping the arena keeps
            # Kokoro from competing with Whisper, vision, and ChromaDB.
            cuda_opts['gpu_mem_limit'] = gpu_mem_mb * 1024 * 1024
        providers = [('CUDAExecutionProvider', cuda_opts), 'CPUExecutionProvider']
    else:
        providers = ['CPUExecutionProvider']

    opts = ort.SessionOptions()
    opts.log_severity_level = 3  # Hide known-harmless warnings
    session = ort.InferenceSession(model, sess_options=opts, providers=providers)
    active = session.get_providers()[0]
    if provider == 'cuda' and active != 'CUDAExecutionProvider':
        raise RuntimeError(f'asked for CUDA but got {active}')
    return Kokoro.from_session(session, voices), active


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--voices', required=True)
    parser.add_argument('--voice', default='af_heart', help='Used for warm-up')
    parser.add_argument('--provider', choices=['cuda', 'cpu'], default='cuda')
    parser.add_argument('--gpu-mem-mb', type=int, default=1024)
    args = parser.parse_args()

    try:
        import soundfile as sf
        kokoro, active = load(args.model, args.voices, args.provider, args.gpu_mem_mb)
        # Warm-up: the first call pays one-time CUDA setup. Do it now so
        # Tales's first sentence doesn't.
        kokoro.create('Warming up.', voice=args.voice, speed=1.0, lang='en-us')
    except Exception as e:
        reply({'ready': False, 'error': f'{type(e).__name__}: {e}'})
        return 1

    reply({'ready': True, 'provider': active})

    # One request per line until tts_node closes our stdin.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            start = time.monotonic()
            samples, rate = kokoro.create(
                req['text'],
                voice=req.get('voice', args.voice),
                speed=float(req.get('speed', 1.0)),
                lang='en-us',
            )
            synth_s = time.monotonic() - start
            sf.write(req['out'], samples, rate)
            reply({'ok': True, 'synth_s': round(synth_s, 3),
                   'audio_s': round(len(samples) / rate, 3)})
        except Exception as e:
            reply({'ok': False, 'error': f'{type(e).__name__}: {e}'})
    return 0


if __name__ == '__main__':
    sys.exit(main())
