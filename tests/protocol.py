"""A wire-level client for the chunked upload protocol.

This module is the executable half of ``docs/upload_protocol.md``. Every JSON
key, header name and content type below is spelled out as a literal, because the
browser uploader and the Flask routes were written in parallel against the
document and never spoke to each other before this suite existed. If the two
sides ever disagree about a name, a test using this helper is where it shows up.

The method bodies deliberately mirror ``static/js/uploader.js``:

    init()      <- Uploader.prototype._uploadChunked / init()
    status()    <- Uploader.prototype._prepareResume
    chunk()     <- Uploader.prototype._uploadChunked / sendChunk()
    complete()  <- Uploader.prototype._uploadChunked / complete()
    cancel()    <- Uploader.prototype._cancelSession

Anything here that the JS does not do is a bug in this file, not a licence for
the server to accept it.
"""

import hashlib

#: Exactly what uploader.js sets on a chunk request. Casing included: HTTP header
#: names are case-insensitive on the wire, but a test that hard-codes the spelling
#: is what catches a rename on either side.
HEADER_OFFSET = "X-Upload-Offset"
HEADER_SHA256 = "X-Chunk-SHA256"
CHUNK_CONTENT_TYPE = "application/octet-stream"


_DEFAULT = object()  # "argument not supplied", distinct from a supplied falsy value


def client_key_for(filename, size, last_modified=1700000000000):
    """Reproduce §7's fingerprint: ``sha256_hex(name \\0 size \\0 lastModified)``.

    The server treats this as opaque and only checks it is 64 lowercase hex
    characters, so the exact recipe cannot drift the two sides apart — but using
    the real one keeps the tests honest about what the client sends.
    """
    material = f"{filename}\0{size}\0{last_modified}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def sha256_hex(data):
    """The per-chunk digest the client computes with ``crypto.subtle``."""
    return hashlib.sha256(data).hexdigest()


class ProtocolClient:
    """Issues the four chunked-upload requests against one album share token."""

    def __init__(self, client, token):
        self.client = client
        self.token = token
        self.base = f"/share/{token}/upload"

    # ── §6.1 init ──────────────────────────────────────────────────────────

    def init(self, filename, total_size, client_key=_DEFAULT,
             content_type="application/octet-stream", **overrides):
        """POST /upload/init with the client's exact JSON payload.

        ``client_key`` defaults to a correctly derived one, but an explicit ``""`` or
        ``None`` is sent verbatim — a falsy-value fallback here would quietly swap a
        deliberately bad key for a valid one and make the rejection tests vacuous.
        """
        payload = {
            "client_key": (client_key_for(filename, total_size)
                           if client_key is _DEFAULT else client_key),
            "filename": filename,
            "total_size": total_size,
            "content_type": content_type,
        }
        payload.update(overrides)
        return self.client.post(f"{self.base}/init", json=payload)

    def init_for(self, filename, data, **kwargs):
        """``init`` for a file whose bytes we already hold."""
        return self.init(filename, len(data), **kwargs)

    # ── §6.2 status ────────────────────────────────────────────────────────

    def status(self, upload_id):
        """GET /upload/status/<upload_id> — the presentation-only resume probe."""
        return self.client.get(f"{self.base}/status/{upload_id}")

    # ── §6.3 chunk ─────────────────────────────────────────────────────────

    def chunk(self, upload_id, offset, data, digest=None, headers=None):
        """POST /upload/chunk/<upload_id> with a raw octet-stream body.

        Not multipart and not JSON: the body is the chunk's bytes and nothing
        else, exactly as ``xhr.send(arrayBuffer)`` produces.
        """
        request_headers = {
            HEADER_OFFSET: str(offset),
            HEADER_SHA256: sha256_hex(data) if digest is None else digest,
        }
        if headers:
            request_headers.update(headers)
        return self.client.post(
            f"{self.base}/chunk/{upload_id}",
            data=data,
            content_type=CHUNK_CONTENT_TYPE,
            headers=request_headers,
        )

    # ── §6.4 complete ──────────────────────────────────────────────────────

    def complete(self, upload_id):
        """POST /upload/complete/<upload_id> with an empty body."""
        return self.client.post(f"{self.base}/complete/{upload_id}")

    # ── §6.5 cancel ────────────────────────────────────────────────────────

    def cancel(self, upload_id):
        """DELETE /upload/cancel/<upload_id> — what removeItem fires, body-less."""
        return self.client.delete(f"{self.base}/cancel/{upload_id}")

    # ── Composite flows ────────────────────────────────────────────────────

    def send_chunks(self, upload_id, data, chunk_size, start=0, stop=None):
        """Send ``data[start:stop]`` as consecutive chunks, returning the responses.

        Stops early and returns what it has if any chunk is not a 200, so a
        caller can assert on the first failure rather than on a cascade.
        """
        stop = len(data) if stop is None else stop
        responses = []
        offset = start
        while offset < stop:
            piece = data[offset:min(offset + chunk_size, stop)]
            response = self.chunk(upload_id, offset, piece)
            responses.append(response)
            if response.status_code != 200:
                break
            offset += len(piece)
        return responses

    def upload(self, filename, data, chunk_size, **kwargs):
        """Run the whole init -> chunks -> complete dance, returning every response.

        Returns ``(init_response, chunk_responses, complete_response)``.
        """
        init_response = self.init_for(filename, data, **kwargs)
        assert init_response.status_code in (200, 201), init_response.get_json()
        upload_id = init_response.get_json()["upload_id"]
        chunk_responses = self.send_chunks(
            upload_id, data, chunk_size,
            start=init_response.get_json()["received_bytes"],
        )
        return init_response, chunk_responses, self.complete(upload_id)
