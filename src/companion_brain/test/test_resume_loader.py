"""Tests for resume_loader on a small PDF built by hand in the test."""

import importlib.metadata
import os
import re

from companion_brain import resume_loader
from companion_brain.resume_loader import CACHE_NAME, load_resume_text
import pytest


def make_pdf(text='Hello Resume'):
    """Return bytes of a one-page PDF that shows text, or a blank page if text is None."""
    if text is None:
        stream = b''
    else:
        stream = ('BT /F1 12 Tf 72 712 Td (%s) Tj ET' % text).encode('latin-1')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        (b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
         b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>'),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % i + body + b'\nendobj\n'
    xref_at = len(out)
    out += b'xref\n0 %d\n' % (len(objects) + 1)
    out += b'0000000000 65535 f \n'
    for off in offsets:
        out += b'%010d 00000 n \n' % off
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\n' % (len(objects) + 1)
    out += b'startxref\n%d\n%%%%EOF\n' % xref_at
    return bytes(out)


def write_pdf(path, text='Hello Resume'):
    """Write a generated PDF to path and return it as a string."""
    path.write_bytes(make_pdf(text))
    return str(path)


@pytest.mark.parametrize('text', ['Hello Resume', None])
def test_generated_pdf_xref_offsets_are_correct(text):
    """Point every xref entry at the start of its object."""
    data = make_pdf(text)
    xref_at = int(re.search(rb'startxref\n(\d+)', data).group(1))
    assert data[xref_at:xref_at + 4] == b'xref'
    entries = re.findall(rb'(\d{10}) 00000 n ', data[xref_at:])
    for num, off in enumerate(entries, start=1):
        assert data[int(off):].startswith(b'%d 0 obj' % num)


def test_extracts_text_and_writes_cache(tmp_path):
    """Extract text from the PDF and write resume.txt next to it."""
    pdf = write_pdf(tmp_path / 'resume.pdf')
    text, origin = load_resume_text(pdf)
    assert origin == 'extracted'
    assert 'Hello Resume' in text
    cache = tmp_path / CACHE_NAME
    assert CACHE_NAME == 'resume.txt'
    assert cache.is_file()
    assert cache.read_text(encoding='utf-8') == text


def test_second_call_uses_cache(tmp_path, monkeypatch):
    """Read resume.txt on the second call without touching PdfReader."""
    pdf = write_pdf(tmp_path / 'resume.pdf')
    first, _ = load_resume_text(pdf)

    def boom(*args, **kwargs):
        raise AssertionError('PdfReader should not be called on a cache hit')

    monkeypatch.setattr(resume_loader, 'PdfReader', boom)
    text, origin = load_resume_text(pdf)
    assert origin == 'cache'
    assert text == first


def test_newer_pdf_forces_reextract(tmp_path):
    """Re-extract when the PDF is newer than the cached text."""
    pdf_path = tmp_path / 'resume.pdf'
    pdf = write_pdf(pdf_path, 'Old Resume')
    load_resume_text(pdf)
    cache = tmp_path / CACHE_NAME
    os.utime(cache, (1_000_000, 1_000_000))
    write_pdf(pdf_path, 'New Resume')
    os.utime(pdf_path, (2_000_000, 2_000_000))
    text, origin = load_resume_text(pdf)
    assert origin == 'extracted'
    assert 'New Resume' in text
    assert 'New Resume' in cache.read_text(encoding='utf-8')


def test_stale_cache_content_is_not_returned(tmp_path):
    """Ignore an older resume.txt with unrelated content when the PDF is newer."""
    pdf_path = tmp_path / 'resume.pdf'
    cache = tmp_path / CACHE_NAME
    cache.write_text('stale text', encoding='utf-8')
    os.utime(cache, (1_000_000, 1_000_000))
    pdf = write_pdf(pdf_path)
    os.utime(pdf_path, (2_000_000, 2_000_000))
    text, origin = load_resume_text(pdf)
    assert origin == 'extracted'
    assert 'stale text' not in text


def test_missing_pdf(tmp_path):
    """Return (None, 'missing') when the PDF does not exist."""
    assert load_resume_text(str(tmp_path / 'nope.pdf')) == (None, 'missing')


def test_directory_path_is_missing(tmp_path):
    """Return (None, 'missing') when the path is a directory."""
    assert load_resume_text(str(tmp_path)) == (None, 'missing')


def test_garbage_bytes_is_error(tmp_path):
    """Return (None, 'error') for a file that is not a PDF, and write no cache."""
    bad = tmp_path / 'resume.pdf'
    bad.write_bytes(b'this is not a pdf at all \x00\x01\x02' * 10)
    assert load_resume_text(str(bad)) == (None, 'error')
    assert not (tmp_path / CACHE_NAME).exists()


def test_empty_file_is_error(tmp_path):
    """Return (None, 'error') for an empty file."""
    bad = tmp_path / 'resume.pdf'
    bad.write_bytes(b'')
    assert load_resume_text(str(bad)) == (None, 'error')


def test_pypdf_unavailable_is_error(tmp_path, monkeypatch):
    """Return (None, 'error') when pypdf is not installed and there is no cache."""
    pdf = write_pdf(tmp_path / 'resume.pdf')
    monkeypatch.setattr(resume_loader, 'PdfReader', None)
    assert load_resume_text(pdf) == (None, 'error')
    assert not (tmp_path / CACHE_NAME).exists()


def test_pypdf_unavailable_still_uses_cache(tmp_path, monkeypatch):
    """Serve a fresh resume.txt even when pypdf is not installed."""
    pdf = write_pdf(tmp_path / 'resume.pdf')
    first, _ = load_resume_text(pdf)
    monkeypatch.setattr(resume_loader, 'PdfReader', None)
    assert load_resume_text(pdf) == (first, 'cache')


def test_blank_pdf_is_empty_and_not_cached(tmp_path):
    """Return (None, 'empty') for a PDF with no text layer and write no cache."""
    pdf = write_pdf(tmp_path / 'resume.pdf', text=None)
    assert load_resume_text(pdf) == (None, 'empty')
    assert not (tmp_path / CACHE_NAME).exists()
    assert load_resume_text(pdf) == (None, 'empty')


def test_whitespace_only_pdf_is_empty(tmp_path):
    """Treat a PDF whose only text is spaces as empty."""
    pdf = write_pdf(tmp_path / 'resume.pdf', text='   ')
    assert load_resume_text(pdf) == (None, 'empty')
    assert not (tmp_path / CACHE_NAME).exists()


def test_tilde_path_expansion(tmp_path, monkeypatch):
    """Expand a leading ~ using HOME."""
    monkeypatch.setenv('HOME', str(tmp_path))
    data = tmp_path / 'companion_ws' / 'data'
    data.mkdir(parents=True)
    write_pdf(data / 'resume.pdf')
    text, origin = load_resume_text('~/companion_ws/data/resume.pdf')
    assert origin == 'extracted'
    assert 'Hello Resume' in text
    assert (data / CACHE_NAME).is_file()


def test_unwritable_cache_still_returns_text(tmp_path):
    """Return extracted text even if resume.txt cannot be written."""
    pdf = write_pdf(tmp_path / 'resume.pdf')
    (tmp_path / CACHE_NAME).mkdir()
    text, origin = load_resume_text(pdf)
    assert origin == 'extracted'
    assert 'Hello Resume' in text


def test_pypdf_does_not_require_numpy():
    """Keep pypdf free of a numpy requirement so system numpy stays 1.26.4."""
    reqs = importlib.metadata.requires('pypdf') or []
    base = [r for r in reqs if 'extra ==' not in r]
    assert not any(r.lower().startswith('numpy') for r in base)
