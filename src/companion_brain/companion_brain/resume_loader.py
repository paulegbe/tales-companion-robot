"""
Load the candidate's resume text for interview practice.

Plain Python, no ROS imports. Extracts text from the resume PDF with pypdf
(pure Python, no numpy) and caches it as resume.txt next to the PDF, so
later startups skip extraction. The cache is rebuilt when the PDF is newer.

Never raises: a missing or unreadable PDF returns None, and interviews still
run without resume grounding. The resume lives in data/, which is gitignored.
"""

import os

try:
    from pypdf import PdfReader
except ImportError:
    # Without pypdf the bridge must still start; interviews just lack the resume.
    PdfReader = None

CACHE_NAME = 'resume.txt'


def load_resume_text(pdf_path):
    """Return (text, origin): origin is cache, extracted, missing, empty or error."""
    pdf_path = os.path.expanduser(pdf_path)
    if not os.path.isfile(pdf_path):
        return None, 'missing'

    cache_path = os.path.join(os.path.dirname(pdf_path), CACHE_NAME)
    try:
        if (os.path.isfile(cache_path)
                and os.path.getmtime(cache_path) >= os.path.getmtime(pdf_path)):
            with open(cache_path, 'r', encoding='utf-8') as f:
                return f.read(), 'cache'
    except OSError:
        pass  # Fall through and re-extract.

    if PdfReader is None:
        return None, 'error'
    try:
        reader = PdfReader(pdf_path)
        text = '\n'.join((page.extract_text() or '') for page in reader.pages).strip()
    except Exception:
        # pypdf raises many types (encrypted, malformed, truncated). Any of
        # them just means no resume for this run.
        return None, 'error'

    if not text:
        # A scanned PDF has no text layer. Do not cache an empty resume.
        return None, 'empty'

    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            f.write(text)
    except OSError:
        pass  # The text is still usable; it just gets re-extracted next time.
    return text, 'extracted'
