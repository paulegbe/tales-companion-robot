"""Check that package.xml parses and declares the dependencies the code imports.

These tests use only the standard library so they run on a Mac without ROS.
Source modules are scanned with ast rather than imported, because importing
them would pull in rclpy and cv_bridge.
"""

import ast
from pathlib import Path
import xml.etree.ElementTree as ET

PKG_DIR = Path(__file__).resolve().parents[1]
PACKAGE_XML = PKG_DIR / 'package.xml'
SOURCE_DIR = PKG_DIR / 'companion_brain'

DEP_TAGS = ('depend', 'build_depend', 'exec_depend', 'test_depend')
RUNTIME_DEP_TAGS = ('depend', 'exec_depend')


def _root():
    return ET.parse(PACKAGE_XML).getroot()


def _dep_names(root, tags):
    names = []
    for tag in tags:
        for elem in root.findall(tag):
            names.append((elem.text or '').strip())
    return names


def _imports_module(path, module):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[0] == module:
                    return True
        elif isinstance(node, ast.ImportFrom):
            # level > 0 means a relative import, which is never a top level package.
            if node.level == 0 and node.module:
                if node.module.split('.')[0] == module:
                    return True
    return False


def test_package_xml_parses():
    """package.xml is well formed with a format 3 package root."""
    root = _root()
    assert root.tag == 'package'
    assert root.get('format') == '3'


def test_cv_bridge_declared():
    """cv_bridge is declared as a depend or exec_depend."""
    assert 'cv_bridge' in _dep_names(_root(), RUNTIME_DEP_TAGS)


def test_no_duplicate_depends():
    """No dependency name appears more than once across dependency tags."""
    names = _dep_names(_root(), DEP_TAGS)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, 'Duplicate dependencies: %s' % dupes


def test_cv_bridge_importers_declared():
    """Modules that import cv_bridge exist, and cv_bridge is declared for them."""
    importers = sorted(
        p.name for p in SOURCE_DIR.glob('*.py') if _imports_module(p, 'cv_bridge')
    )
    assert importers, 'Expected at least one module to import cv_bridge'
    declared = _dep_names(_root(), RUNTIME_DEP_TAGS)
    assert 'cv_bridge' in declared, (
        'cv_bridge imported by %s but not declared in package.xml' % importers
    )
