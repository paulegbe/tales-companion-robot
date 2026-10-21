"""
Audio timing helpers shared by tts_node and stt_node.

Plain Python with no ROS or third-party imports, so it is testable on the Mac.

tts_node uses sox_convert_cmd to turn each synthesized sentence into the
48 kHz stereo WAV that aplay plays. It can append trailing silence
(tail_pad_s), because some sinks, like the HDMI display speaker, replay
their final buffer when playback stops abruptly, which makes the last word
of every sentence stutter.

stt_node uses unmute_time to decide when the mic may reopen after Tales
stops talking (mute_tail_s).

Both nodes read these values from ROS parameters through
seconds_or_default, which falls back to the default with a warning on a
bad value. A typo in a launch argument must not leave Tales mute or deaf.
"""

import math


def parse_seconds(value, name):
    """Return value as a finite float >= 0, or raise ValueError naming the parameter."""
    # bool is an int subclass; True must not quietly become 1 second.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('%s must be a number of seconds, got %r' % (name, value))
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('%s must be a finite number >= 0, got %r' % (name, value))
    return seconds


def seconds_or_default(value, name, default):
    """
    Return (seconds, warning) for a parameter value.

    A valid value comes back with warning None. An invalid one comes back as
    the default, with a warning message for the node to log.
    """
    try:
        return parse_seconds(value, name), None
    except ValueError as e:
        return float(default), '%s. Using the default %s.' % (e, default)


def format_seconds(seconds):
    """Format seconds for sox at millisecond precision, e.g. 0.3 -> '0.300'."""
    # Fixed precision keeps float noise like 0.30000000000000004 out of sox.
    return '%.3f' % seconds


def sox_convert_cmd(raw_path, out_path, tail_pad_s=0.0):
    """
    Build the sox command that converts a sentence to 48 kHz stereo.

    -G (guard) prevents clipping during conversion. When tail_pad_s rounds to
    more than zero milliseconds, 'pad 0 <tail_pad_s>' appends that much
    trailing silence. Otherwise the pad effect is left out entirely.
    """
    pad = format_seconds(parse_seconds(tail_pad_s, 'tail_pad_s'))
    cmd = ['sox', '-G', raw_path, '-r', '48000', '-c', '2', out_path]
    if pad != format_seconds(0):
        # sox effects go after the output file.
        cmd += ['pad', '0', pad]
    return cmd


def unmute_time(now, mute_tail_s):
    """Return the monotonic time at which the mic may reopen."""
    return now + mute_tail_s
