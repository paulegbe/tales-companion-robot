"""Shared pytest configuration for companion_brain tests.

Registers the `ros` marker and lets the test suite run on a Mac without ROS.

The generated ament linter tests (copyright, flake8, pep257) import ament_*
at module level, so on a machine without ROS they fail at collection, before
`-m "not ros"` can deselect anything. Rather than editing those generated
files, this conftest reports each one as skipped when its ament module is
missing. The linters still run under `colcon test` on the Jetson.

A file is skipped only when its module cannot be found AND AMENT_PREFIX_PATH
is unset. With ROS sourced, a missing linter should fail loudly, not vanish.
"""

import importlib.util
import os

import pytest

AMENT_TEST_MODULES = {
    'test_copyright.py': 'ament_copyright',
    'test_flake8.py': 'ament_flake8',
    'test_pep257.py': 'ament_pep257',
}


def pytest_configure(config):
    """Register the ros marker so pytest does not warn about it."""
    config.addinivalue_line(
        'markers', 'ros: needs ROS 2 or hardware; deselect with -m "not ros"')


def _should_skip(module):
    """Return True when the ament module is missing and ROS is not sourced."""
    if os.environ.get('AMENT_PREFIX_PATH'):
        return False
    return importlib.util.find_spec(module) is None


# This hook and its single `collector` argument are stable from pytest 6.2
# through 8, unlike pytest_ignore_collect, whose argument names changed in
# pytest 7. The Jetson's apt pytest is older than the Mac venv's, so a
# version-sensitive hook could break `colcon test` there.
@pytest.hookimpl(tryfirst=True)
def pytest_make_collect_report(collector):
    """Report an ament linter module as skipped instead of importing it."""
    if not isinstance(collector, pytest.Module):
        return None
    filename = collector.nodeid.rsplit('/', 1)[-1]
    module = AMENT_TEST_MODULES.get(filename)
    if module is None or not _should_skip(module):
        return None
    reason = '%s not installed (no ROS). Runs under colcon test on the Jetson.' % module
    # The skip summary unpacks longrepr as (path, lineno, reason).
    longrepr = (collector.nodeid, 0, 'Skipped: ' + reason)
    return pytest.CollectReport(collector.nodeid, 'skipped', longrepr, [])
