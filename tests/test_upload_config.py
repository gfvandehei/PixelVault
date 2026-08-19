"""Coherence of the four upload caps against each other.

``MAX_UPLOAD_MB``, ``MAX_INFLIGHT_UPLOAD_MB_PER_USER``,
``MAX_CONCURRENT_UPLOADS_PER_USER`` and ``UPLOAD_CHUNK_SIZE`` are independent env
vars that have to agree. The pairing that prompted this check was a real
deployment running ``MAX_UPLOAD_MB=5000`` against the default 2048 MB in-flight
cap: ``init`` accepted the declared size against the request ceiling and the
per-user quota then refused the very same number, so **no large file could ever
be uploaded** and nothing in the logs connected the refusal to configuration.

``validate_upload_limits`` reports; it never raises. That is load-bearing and
pinned below — this suite's own conftest deliberately inverts the two byte caps
so a 429 can be told apart from a 413, and existing self-hosted deployments must
degrade rather than crash-loop.
"""

import pixelvault.config as config
from pixelvault.config import validate_upload_limits

MB = 1024 * 1024


def _by_severity(problems, severity):
    return [message for level, message in problems if level == severity]


# ── The pairing that started this ──────────────────────────────────────────

def test_an_inflight_cap_below_the_request_ceiling_is_an_error():
    problems = validate_upload_limits(max_file_bytes=5000 * MB,
                                      max_inflight_bytes=2048 * MB)

    errors = _by_severity(problems, 'error')
    assert len(errors) == 1
    assert 'MAX_INFLIGHT_UPLOAD_MB_PER_USER' in errors[0]
    assert 'MAX_UPLOAD_MB' in errors[0]


def test_the_error_names_the_value_that_would_fix_it():
    """A message that only says "these disagree" leaves the operator guessing which
    of the two to move and to what. It quotes 3 x MAX_UPLOAD_MB, the value that also
    clears the parallel-batch advisory below."""
    problems = validate_upload_limits(max_file_bytes=5000 * MB,
                                      max_inflight_bytes=2048 * MB,
                                      concurrency=3)

    assert '15000' in _by_severity(problems, 'error')[0]


def test_a_cap_that_fits_one_file_but_not_a_batch_is_only_a_warning():
    """The uploader starts three files at once and reserves each declared size in
    full, so this configuration works one file at a time and refuses part of a
    batch — annoying, not broken, and a legitimate choice for a small disk."""
    problems = validate_upload_limits(max_file_bytes=500 * MB,
                                      max_inflight_bytes=800 * MB,
                                      concurrency=3)

    assert _by_severity(problems, 'error') == []
    assert len(_by_severity(problems, 'warning')) == 1


def test_the_two_byte_caps_produce_one_finding_not_two():
    """The below-ceiling error and the below-batch warning are mutually exclusive:
    reporting both would have the operator fix one and still see a complaint."""
    problems = validate_upload_limits(max_file_bytes=5000 * MB,
                                      max_inflight_bytes=100 * MB,
                                      concurrency=3)

    assert len(problems) == 1


# ── The other contradictions ───────────────────────────────────────────────

def test_a_chunk_size_above_the_request_ceiling_is_an_error():
    """``init`` hands ``chunk_size`` to the client, which then sends slices no request
    may carry — every chunk 413s while init itself looks healthy."""
    problems = validate_upload_limits(max_file_bytes=4 * MB,
                                      max_inflight_bytes=64 * MB,
                                      chunk_size=8 * MB)

    assert any('UPLOAD_CHUNK_SIZE' in m for m in _by_severity(problems, 'error'))


def test_a_zero_session_cap_is_an_error():
    problems = validate_upload_limits(max_file_bytes=8 * MB,
                                      max_inflight_bytes=64 * MB,
                                      max_sessions=0)

    assert any('MAX_CONCURRENT_UPLOADS_PER_USER' in m
               for m in _by_severity(problems, 'error'))


# ── Shape of the check itself ──────────────────────────────────────────────

def test_the_shipped_defaults_are_coherent():
    """The values in ``.env.example`` and ``config.py`` must not themselves trip the
    check, or the warning becomes noise every operator learns to ignore."""
    assert validate_upload_limits(max_file_bytes=500 * MB,
                                  max_inflight_bytes=2048 * MB,
                                  max_sessions=10,
                                  chunk_size=8 * MB) == []


def test_the_caps_are_read_at_call_time(monkeypatch):
    """Defaults resolve from the module globals when called, not when defined — a
    ``def``-time binding would make the check untestable and would freeze it against
    whatever the environment held at import."""
    monkeypatch.setattr(config, 'MAX_CONTENT_LENGTH', 5000 * MB)
    monkeypatch.setattr(config, 'MAX_INFLIGHT_UPLOAD_BYTES_PER_USER', 2048 * MB)
    monkeypatch.setattr(config, 'UPLOAD_CHUNK_SIZE', 8 * MB)
    monkeypatch.setattr(config, 'MAX_CONCURRENT_UPLOADS_PER_USER', 10)

    assert _by_severity(validate_upload_limits(), 'error') != []


def test_a_contradictory_pairing_does_not_stop_the_app_booting(app):
    """The suite's own conftest runs an in-flight cap below the request ceiling on
    purpose. If the check ever raises instead of logging, this fixture stops
    resolving — and so does every existing deployment with the defaults half-set."""
    assert app is not None
    assert _by_severity(validate_upload_limits(), 'error') != []
