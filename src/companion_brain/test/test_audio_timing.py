"""
Tests for companion_brain.audio_timing.

Covers the sox conversion command (with and without trailing silence), the
seconds parameter validation used by tts_node and stt_node, and the mic
unmute time. Pure Python, no ROS.
"""

import math

from companion_brain.audio_timing import (
    format_seconds,
    parse_seconds,
    seconds_or_default,
    sox_convert_cmd,
    unmute_time,
)
import pytest

RAW = '/t/raw.wav'
OUT = '/t/out.wav'
BASE_CMD = ['sox', '-G', RAW, '-r', '48000', '-c', '2', OUT]


def test_sox_default_matches_pre_change_command():
    """Keep the default command identical to the one used before tail padding."""
    assert sox_convert_cmd(RAW, OUT) == BASE_CMD


def test_sox_explicit_zero_matches_default():
    """Omit the pad effect when tail_pad_s is explicitly 0.0."""
    assert sox_convert_cmd(RAW, OUT, 0.0) == BASE_CMD


def test_sox_pad_goes_after_output_path():
    """Append 'pad 0 0.300' after the output file for 0.3 s."""
    cmd = sox_convert_cmd(RAW, OUT, 0.3)
    assert cmd == BASE_CMD + ['pad', '0', '0.300']
    assert cmd.index('pad') > cmd.index(OUT)


@pytest.mark.parametrize('tail, expected', [
    (0.5, '0.500'),
    (1, '1.000'),
    (0.25, '0.250'),
    (0.1 + 0.2, '0.300'),
    (2.0, '2.000'),
])
def test_sox_pad_values_formatted(tail, expected):
    """Format the pad length at millisecond precision without float noise."""
    assert sox_convert_cmd(RAW, OUT, tail) == BASE_CMD + ['pad', '0', expected]


@pytest.mark.parametrize('tail', [0, 0.0, 0.0004])
def test_sox_no_pad_when_rounds_to_zero(tail):
    """Leave out the pad effect when tail_pad_s rounds to 0 ms."""
    cmd = sox_convert_cmd(RAW, OUT, tail)
    assert cmd == BASE_CMD
    assert 'pad' not in cmd


@pytest.mark.parametrize('tail', [0.0004, 0.0005, 0.0006, 0.001, 0.0015])
def test_sox_pad_present_iff_formatted_nonzero(tail):
    """Include pad exactly when the formatted value is not '0.000'."""
    cmd = sox_convert_cmd(RAW, OUT, tail)
    formatted = format_seconds(tail)
    if formatted == '0.000':
        assert cmd == BASE_CMD
    else:
        assert cmd == BASE_CMD + ['pad', '0', formatted]


def test_sox_paths_passed_through_unchanged():
    """Pass raw and output paths through verbatim, including spaces."""
    cmd = sox_convert_cmd('/a b/in.wav', '/c d/out.wav', 0.3)
    assert cmd[2] == '/a b/in.wav'
    assert cmd[7] == '/c d/out.wav'


@pytest.mark.parametrize('bad', [
    -0.1, -1, float('nan'), float('inf'), float('-inf'), True, False, '0.3', None,
])
def test_sox_rejects_bad_tail_pad(bad):
    """Raise ValueError naming tail_pad_s for invalid pad values."""
    with pytest.raises(ValueError, match='tail_pad_s'):
        sox_convert_cmd(RAW, OUT, bad)


@pytest.mark.parametrize('value, expected', [
    (0, 0.0),
    (0.4, 0.4),
    (2, 2.0),
    (0.0, 0.0),
    (1e-9, 1e-9),
])
def test_parse_seconds_accepts_valid(value, expected):
    """Return valid non-negative finite numbers as floats."""
    result = parse_seconds(value, 'mute_tail_s')
    assert type(result) is float
    assert result == expected


@pytest.mark.parametrize('bad', [
    -0.1, -1, float('nan'), float('inf'), float('-inf'), 'abc', '0.4', None,
    True, False, [0.4],
])
def test_parse_seconds_rejects_invalid(bad):
    """Raise ValueError whose message names the parameter."""
    with pytest.raises(ValueError) as excinfo:
        parse_seconds(bad, 'my_param')
    assert 'my_param' in str(excinfo.value)


@pytest.mark.parametrize('value, expected', [
    (0.0, 0.0),
    (0.4, 0.4),
    (2, 2.0),
])
def test_seconds_or_default_valid(value, expected):
    """Return the parsed value and no warning for a valid input."""
    seconds, warning = seconds_or_default(value, 'mute_tail_s', 0.4)
    assert seconds == expected
    assert type(seconds) is float
    assert warning is None


@pytest.mark.parametrize('bad', [-1.0, float('nan'), True, 'x'])
def test_seconds_or_default_invalid_falls_back(bad):
    """Fall back to the default with a warning naming param, bad value, default."""
    seconds, warning = seconds_or_default(bad, 'tail_pad_s', 0.3)
    assert seconds == 0.3
    assert type(seconds) is float
    assert warning is not None
    assert 'tail_pad_s' in warning
    assert repr(bad) in warning
    assert '0.3' in warning


def test_seconds_or_default_int_default_returned_as_float():
    """Return an int default as a float."""
    seconds, warning = seconds_or_default(-5, 'mute_tail_s', 1)
    assert seconds == 1.0
    assert type(seconds) is float
    assert warning is not None
    assert 'mute_tail_s' in warning
    assert '-5' in warning
    assert '1' in warning


def test_seconds_or_default_zero_is_valid_not_fallback():
    """Treat 0 as valid rather than falling back to the default."""
    assert seconds_or_default(0, 'tail_pad_s', 0.3) == (0.0, None)


@pytest.mark.parametrize('seconds, expected', [
    (0.3, '0.300'),
    (0, '0.000'),
    (1.23456, '1.235'),
    (0.1 + 0.2, '0.300'),
])
def test_format_seconds(seconds, expected):
    """Format at millisecond precision."""
    assert format_seconds(seconds) == expected


def test_unmute_time_adds_tail():
    """Add the mute tail to the current time."""
    assert unmute_time(100.0, 0.4) == pytest.approx(100.4)


def test_unmute_time_zero_tail():
    """Return now unchanged when the tail is zero."""
    assert unmute_time(100.0, 0.0) == 100.0


def test_unmute_time_monotonic_in_tail():
    """Reopen later for a longer tail."""
    assert unmute_time(5.0, 1.0) > unmute_time(5.0, 0.4) > 5.0
    assert not math.isnan(unmute_time(5.0, 0.4))
