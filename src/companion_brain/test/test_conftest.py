"""Tests for the ament skip logic and ros marker in conftest.py.

Each subprocess test builds a throwaway test tree in tmp_path containing a copy
of the real conftest.py, a fake ament linter test, a plain test, and a ros test,
then runs pytest on that tree in a child process. The real test directory is
never run from here, which would recurse.

On a machine where ament_flake8 is importable (the Jetson), the fake linter test
would import the real module, so the subprocess tests are skipped there.
"""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

TEST_DIR = Path(__file__).resolve().parent
REAL_CONFTEST = TEST_DIR / 'conftest.py'

AMENT_AVAILABLE = importlib.util.find_spec('ament_flake8') is not None
needs_no_ament = pytest.mark.skipif(
    AMENT_AVAILABLE,
    reason='ament_flake8 is importable; the fake linter test would import it.')

FAKE_FLAKE8 = (
    '"""Fake ament flake8 test."""\n'
    '\n'
    'from ament_flake8.main import main_with_errors\n'
    '\n'
    '\n'
    'def test_flake8():\n'
    '    """Reference the import so it is used."""\n'
    '    assert main_with_errors\n'
)

PLAIN_TEST = (
    '"""Plain test."""\n'
    '\n'
    '\n'
    'def test_plain():\n'
    '    """Pass."""\n'
    '    assert 1 + 1 == 2\n'
)

ROS_TEST = (
    '"""Test that needs ROS."""\n'
    '\n'
    'import pytest\n'
    '\n'
    '\n'
    '@pytest.mark.ros\n'
    'def test_needs_ros():\n'
    '    """Fail if it is ever run."""\n'
    '    assert False, "ros test should have been deselected"\n'
)


def _build_tree(root):
    """Populate root with the conftest copy and the three test files."""
    shutil.copy(REAL_CONFTEST, root / 'conftest.py')
    (root / 'test_flake8.py').write_text(FAKE_FLAKE8)
    (root / 'test_plain.py').write_text(PLAIN_TEST)
    (root / 'test_rosonly.py').write_text(ROS_TEST)


def _run(root, ament_prefix=None):
    """Run pytest on root in a child process and return the result."""
    env = dict(os.environ)
    env.pop('AMENT_PREFIX_PATH', None)
    if ament_prefix is not None:
        env['AMENT_PREFIX_PATH'] = ament_prefix
    cmd = [
        sys.executable, '-m', 'pytest', str(root),
        '-m', 'not ros', '-q', '-rs',
        '-p', 'no:cacheprovider',
        # The temp dir has no setup.cfg, so repeat its anyio opt-out here.
        '-p', 'no:anyio',
        '-W', 'error::pytest.PytestUnknownMarkWarning',
    ]
    return subprocess.run(
        cmd, env=env, cwd=root, capture_output=True, text=True, timeout=120)


def _output(result):
    """Join stdout and stderr for assertions."""
    return result.stdout + result.stderr


@needs_no_ament
def test_missing_ament_is_skipped_not_errored(tmp_path):
    """Report the fake linter file as skipped when ament is missing."""
    _build_tree(tmp_path)
    result = _run(tmp_path)
    out = _output(result)
    assert result.returncode == 0, out
    assert '1 skipped' in out, out
    assert 'ament_flake8' in out, out
    assert 'ModuleNotFoundError' not in out, out
    assert 'ImportError' not in out, out


@needs_no_ament
def test_ros_marker_is_registered(tmp_path):
    """Deselect the ros test with no unknown mark warning turned error."""
    _build_tree(tmp_path)
    result = _run(tmp_path)
    out = _output(result)
    assert result.returncode == 0, out
    assert '1 deselected' in out, out
    assert 'PytestUnknownMarkWarning' not in out, out


@needs_no_ament
def test_ros_marker_unknown_without_conftest(tmp_path):
    """Fail the -W error run when no conftest registers the ros marker."""
    _build_tree(tmp_path)
    (tmp_path / 'conftest.py').unlink()
    (tmp_path / 'test_flake8.py').unlink()
    result = _run(tmp_path)
    assert result.returncode != 0, _output(result)
    assert 'PytestUnknownMarkWarning' in _output(result)


@needs_no_ament
def test_ament_prefix_set_surfaces_import_error(tmp_path):
    """Let the import error surface when AMENT_PREFIX_PATH is set."""
    _build_tree(tmp_path)
    result = _run(tmp_path, ament_prefix='/nonexistent')
    out = _output(result)
    assert result.returncode != 0, out
    assert 'ModuleNotFoundError' in out or 'ImportError' in out, out


@needs_no_ament
def test_empty_ament_prefix_counts_as_unset(tmp_path):
    """Treat an empty AMENT_PREFIX_PATH the same as an unset one."""
    _build_tree(tmp_path)
    result = _run(tmp_path, ament_prefix='')
    out = _output(result)
    assert result.returncode == 0, out
    assert '1 skipped' in out, out


@needs_no_ament
def test_unrelated_files_collected_normally(tmp_path):
    """Run plain tests as usual alongside the skipped linter file."""
    _build_tree(tmp_path)
    result = _run(tmp_path)
    out = _output(result)
    assert result.returncode == 0, out
    assert '1 passed' in out, out
    assert '1 skipped' in out, out
    assert '1 deselected' in out, out


@needs_no_ament
def test_unlisted_file_with_missing_import_still_errors(tmp_path):
    """Leave a missing import in a non ament file as a collection error."""
    _build_tree(tmp_path)
    (tmp_path / 'test_other.py').write_text(
        FAKE_FLAKE8.replace('ament_flake8', 'no_such_module_xyz'))
    result = _run(tmp_path)
    out = _output(result)
    assert result.returncode != 0, out
    assert 'no_such_module_xyz' in out, out


@needs_no_ament
def test_ament_file_in_subdirectory_is_skipped(tmp_path):
    """Skip a linter file by name even when it lives in a subdirectory."""
    _build_tree(tmp_path)
    sub = tmp_path / 'sub'
    sub.mkdir()
    (tmp_path / 'test_flake8.py').rename(sub / 'test_flake8.py')
    result = _run(tmp_path)
    out = _output(result)
    assert result.returncode == 0, out
    assert '1 skipped' in out, out


@pytest.mark.parametrize('filename, module', [
    ('test_copyright.py', 'ament_copyright'),
    ('test_flake8.py', 'ament_flake8'),
    ('test_pep257.py', 'ament_pep257'),
])
def test_generated_ament_files_unchanged(filename, module):
    """Check each generated linter file still imports its ament module."""
    lines = (TEST_DIR / filename).read_text().splitlines()
    prefix = 'from %s.main import ' % module
    assert any(line.startswith(prefix) for line in lines), filename
