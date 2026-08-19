from flask import (render_template, request, url_for, abort, jsonify, send_file,
                   redirect, session, current_app, make_response)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from ..extensions import db, limiter
from ..models import Album, Photo, AlbumAccess
from ..config import UPLOAD_CHUNK_SIZE, UPLOAD_SESSION_TTL_HOURS
from ..uploads import (UploadError, OffsetMismatch, SessionUnusable, append_chunk,
                       discard_session, ensure_offset_matches, get_session,
                       is_valid_client_key, maybe_sweep_expired_sessions,
                       open_or_recover_session)
from ..utils import (ImageTooLargeError, validate_file, validate_file_header, validate_stored_file,
                     save_file, store_upload, build_album_zip)

# Headroom over one chunk for the per-route body cap. A chunk is exactly
# UPLOAD_CHUNK_SIZE, but a client is entitled to send a short final one and the
# slack absorbs any framing a proxy adds without admitting a second chunk's worth.
_CHUNK_BODY_SLACK = 64 * 1024


def _store_failure_message(exc):
    """Return the per-file message for a store that raised.

    An image refused for its dimensions is a decision the user can act on — shrink it,
    or accept that the album will not take it — so it says so. Everything else stays
    deliberately vague: the exception text can name paths and library internals, and
    this string is handed straight to the uploader.
    """
    if isinstance(exc, ImageTooLargeError):
        return str(exc)
    return 'Upload failed.'


def _upload_error(err):
    """Translate an UploadError into the JSON body and status the protocol assigns it.

    One helper rather than a try/except ladder per endpoint: the status table in
    docs/upload_protocol.md §8 is already encoded on the exception classes, so a route
    only has to hand it back. ``extra`` carries whatever the client needs to act —
    notably the true ``received_bytes`` on a 409, so a re-seek costs no extra round trip.
    """
    body = {'error': err.message}
    body.update(err.extra)
    return jsonify(body), err.status_code


def _album_for_token(token):
    """Return the album behind a share token, or 404. No permission check."""
    album = db.session.query(Album).filter_by(token=token).one_or_none()
    if album is None:
        abort(404)
    return album


def _album_for_upload(token):
    """Return the album behind an upload share token, aborting if it cannot accept uploads.

    Called at the top of every chunked endpoint, not just at init. A 470 MB file is
    ~59 requests spread over minutes, and the owner can switch ``allow_upload`` off at
    any point in that window; the permission that let the first chunk through says
    nothing about the last one, or about ``complete``.

    ``cancel`` deliberately does not use this — see the endpoint.
    """
    album = _album_for_token(token)
    if not album.allow_upload:
        abort(make_response(jsonify({'error': 'Uploads are disabled for this album.'}), 403))
    return album


def _session_for_upload(album, upload_id):
    """Return the caller's live session for this album, aborting 404 if there is none.

    The lookup is scoped to ``current_user.id`` and ``album.id`` on every request
    because ``upload_id`` is a bearer handle: without the scope, a leaked or guessed
    handle becomes a write target inside someone else's album. A session that exists
    but belongs to another caller is reported as missing rather than forbidden — a
    stranger has no business learning that the handle is real.

    Expired sessions are reported the same way, which is what the client expects:
    unknown, expired and completed all mean "evict the stored mapping and re-init".
    """
    upload_dir = current_app.config['UPLOAD_FOLDER']
    upload_session = get_session(db.session, upload_id,
                                 user_id=current_user.id, album_id=album.id)
    if upload_session is not None and upload_session.is_expired(UPLOAD_SESSION_TTL_HOURS):
        discard_session(db.session, upload_session, upload_dir)
        upload_session = None
    if upload_session is None:
        abort(make_response(jsonify({'error': 'Upload session not found or expired.'}), 404))
    return upload_session


def register(app):

    @app.route('/share/<token>', methods=['GET'])
    def album_upload(token):
        """Redirect the share link to the album page. Sets the upload session token so the guest can upload."""
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        session['album_upload_token'] = token
        return redirect(url_for('album_view', token=token))

    @app.route('/view/<view_token>', methods=['GET'])
    @login_required
    def album_view_only(view_token):
        """Render the view-only shared page for an album. Guests can browse photos but cannot upload."""
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        session['album_access_token'] = album.token
        photos = db.session.query(Photo).filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
        if not db.session.query(AlbumAccess).filter_by(
            user_id=current_user.id, album_id=album.id
        ).first():
            db.session.add(AlbumAccess(user_id=current_user.id, album_id=album.id, access_type='view'))
            db.session.commit()
        return render_template('album_view.html', album=album, photos=photos,
            can_upload=False,
            is_owner=False,
            share_url=None,
            view_share_url=None,
            download_url=url_for('download_album_view', view_token=view_token))

    @app.route('/share/<token>/upload', methods=['POST'])
    @login_required
    @limiter.limit("600 per hour")
    def do_upload(token):
        """
        Accept a batch of files uploaded via the share link and save them to the album.

        Validates each file by extension and MIME type, converts HEIC images to JPEG,
        generates thumbnails, and records each file in the database. Returns a JSON
        array of per-file results indicating success or a descriptive error.
        Rejects the entire request if uploads are disabled on the album.
        """
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)

        if not album.allow_upload:
            return jsonify({'error': 'Uploads are disabled for this album.'}), 403

        files = request.files.getlist('files')
        if not files:
            return jsonify({'error': 'No files provided.'}), 400

        results = []
        for file in files[:20]:
            if not file or not file.filename:
                continue

            mime_type, err = validate_file(file)
            if err:
                results.append({'filename': file.filename, 'error': err})
                continue

            try:
                stored_name, file_size, has_thumbnail, taken_at = save_file(file, mime_type)
            except Exception as exc:
                results.append({'filename': file.filename, 'error': _store_failure_message(exc)})
                continue

            photo = Photo(
                album_id=album.id,
                uploader_id=current_user.id,
                uploader_name=current_user.username,
                stored_filename=stored_name,
                original_filename=secure_filename(file.filename)[:200],
                mime_type=mime_type,
                file_size=file_size,
                has_thumbnail=has_thumbnail,
                taken_at=taken_at,
            )
            db.session.add(photo)
            results.append({'filename': file.filename, 'success': True})

        if not db.session.query(AlbumAccess).filter_by(
            user_id=current_user.id, album_id=album.id
        ).first():
            db.session.add(AlbumAccess(user_id=current_user.id, album_id=album.id))
        db.session.commit()
        return jsonify({'results': results})

    @app.route('/share/<token>/upload/init', methods=['POST'])
    @login_required
    @limiter.limit("120 per hour")
    def upload_init(token):
        """
        Open — or recover — a chunked upload session for one large file.

        Files above the chunk threshold cannot cross Cloudflare in a single request,
        so the client declares the file here and then streams it in 8 MiB slices.
        Idempotent on the client's ``client_key``: re-picking a file already in flight
        returns that session and its true offset with ``resumed: true`` rather than
        restarting the transfer. Returns 201 for a fresh session, 200 for a recovered
        one. See docs/upload_protocol.md §6.1.
        """
        album = _album_for_upload(token)
        upload_dir = current_app.config['UPLOAD_FOLDER']

        # No scheduler in this stack, so reclaiming abandoned partials has to ride on
        # request traffic. init is the natural host: it is the only chunked endpoint
        # that is rare per upload. The helper throttles itself.
        maybe_sweep_expired_sessions(db.session, upload_dir)

        payload = request.get_json(silent=True) or {}
        client_key = payload.get('client_key')
        filename = (payload.get('filename') or '').strip()

        if not is_valid_client_key(client_key):
            return jsonify({'error': 'client_key must be 64 lowercase hex characters.'}), 400
        if not filename:
            return jsonify({'error': 'No filename provided.'}), 400
        try:
            total_size = int(payload.get('total_size'))
        except (TypeError, ValueError):
            return jsonify({'error': 'total_size must be an integer.'}), 400
        if total_size <= 0:
            return jsonify({'error': 'total_size must be greater than zero.'}), 400

        # Mandatory, not belt-and-braces: MAX_CONTENT_LENGTH is a per-request bound and
        # every request is now one chunk, so it no longer bounds an upload at all. Without
        # this check a client could declare any size it liked and stream to disk until
        # only the per-user quota stopped it.
        max_bytes = current_app.config['MAX_CONTENT_LENGTH']
        if total_size > max_bytes:
            return jsonify({
                'error': f'File exceeds the {max_bytes // (1024 * 1024)} MB upload limit.'
            }), 413

        try:
            upload_session, created = open_or_recover_session(
                db.session, upload_dir, current_user.id, album.id,
                client_key, filename[:200], total_size,
            )
        except UploadError as err:
            return _upload_error(err)

        return jsonify({
            'upload_id': upload_session.upload_id,
            'chunk_size': UPLOAD_CHUNK_SIZE,
            'received_bytes': upload_session.received_bytes,
            'total_size': upload_session.total_size,
            'resumed': not created,
        }), (201 if created else 200)

    @app.route('/share/<token>/upload/status/<upload_id>', methods=['GET'])
    @login_required
    @limiter.limit("300 per hour")
    def upload_status(token, upload_id):
        """
        Report where a chunked upload stands, so a returning client knows where to seek.

        The client probes this before sending anything when it finds a stored mapping
        for a re-selected file. A 404 means unknown, expired or already completed — all
        three tell the client the same thing: forget the mapping and init afresh.
        """
        album = _album_for_upload(token)
        upload_session = _session_for_upload(album, upload_id)
        return jsonify({
            'upload_id': upload_session.upload_id,
            'received_bytes': upload_session.received_bytes,
            'total_size': upload_session.total_size,
            'original_filename': upload_session.original_filename,
            'expires_at': upload_session.expires_at(UPLOAD_SESSION_TTL_HOURS)
                                        .strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

    @app.route('/share/<token>/upload/cancel/<upload_id>', methods=['DELETE'])
    @login_required
    @limiter.limit("120 per hour")
    def upload_cancel(token, upload_id):
        """
        Abandon a chunked upload, returning its reservation to the caller's quota now.

        Without this, a file the user removes from the queue keeps its full declared
        size charged against ``MAX_INFLIGHT_UPLOAD_MB_PER_USER`` until the TTL sweep
        collects it a day later — which is the ordinary way a user meets "upload would
        exceed your in-flight limit" while nothing is actually uploading.

        Deliberately not gated on ``allow_upload``: closing an album must not strand
        its guests' reservations, and handing quota back is the one upload operation
        that stays safe once uploads are off.

        Idempotent, and answers 200 whether or not there was anything to cancel. The
        client fires this without waiting for a reply, so a retry, a double click, or a
        handle the sweep already took must all read as "it is gone". A handle belonging
        to someone else is likewise reported as nothing-to-cancel: ``get_session`` is
        scoped to the caller and album, and a stranger learns nothing about whether the
        handle is real.

        A chunk racing the delete is safe — ``append_chunk`` holds a lock on the partial
        and re-reads the row under it, so it sees the deletion and reports 404.
        """
        album = _album_for_token(token)
        upload_dir = current_app.config['UPLOAD_FOLDER']
        upload_session = get_session(db.session, upload_id,
                                     user_id=current_user.id, album_id=album.id)
        if upload_session is None:
            return jsonify({'cancelled': False}), 200
        discard_session(db.session, upload_session, upload_dir)
        return jsonify({'cancelled': True}), 200

    # Charge the limiter for every chunk request that is not a 200, and for nothing
    # else. Two defects sat in the previous '422 only' rule, in opposite directions.
    #
    # Too little: every other status was free, so any refusal was an unmetered sink.
    # The cheapest was a session parked at a fixed offset — 8 MiB of body per request,
    # 409 each time, forever, with the counter still reading zero. 413 (understated
    # ``total_size``) and 400 (a first slice libmagic rejects) were the same trick with
    # a different status.
    #
    # Too much: the limit was still *checked* on every request even though it was only
    # deducted on 422s, so a user whose link corrupted 60 chunks — a bad-network day,
    # not an attack — was then 429'd on every chunk they sent for the rest of the hour.
    # Sixty checksum failures could deny that user the feature entirely.
    #
    # The budget is therefore sized for failures, not for traffic. A 500 MB file is ~63
    # chunks and spends nothing; a resume costs one 409; a flaky link costs a handful of
    # 422s. 600 is roughly an order of magnitude above the worst honest hour we can
    # construct, and an order of magnitude below what makes the endpoint worth abusing:
    # it caps a hostile client at 600 refusals an hour, and — since a 409 is now
    # answered before the body is read at all — the ones that cost a body read are only
    # those where the client really did send bytes. See docs/upload_protocol.md §8.
    @app.route('/share/<token>/upload/chunk/<upload_id>', methods=['POST'])
    @login_required
    @limiter.limit("600 per hour", deduct_when=lambda r: r.status_code != 200)
    def upload_chunk(token, upload_id):
        """
        Append one slice of a chunked upload at the offset the client declares.

        The body is raw bytes; ``X-Upload-Offset`` says where they belong and
        ``X-Chunk-SHA256`` attests to them. Every integrity rule — offset match,
        overrun, checksum, the truncate-then-append that makes a re-sent chunk
        idempotent — lives in ``pixelvault.uploads``; this route only translates.
        """
        # The global 500 MB cap is meaningless here: this endpoint has no business
        # reading more than a chunk, and the ceiling has to be low or a single lying
        # Content-Length undoes the point of slicing the file up.
        request.max_content_length = UPLOAD_CHUNK_SIZE + _CHUNK_BODY_SLACK

        album = _album_for_upload(token)
        upload_session = _session_for_upload(album, upload_id)

        try:
            offset = int(request.headers['X-Upload-Offset'])
        except (KeyError, ValueError):
            return jsonify({'error': 'X-Upload-Offset must be an integer byte offset.'}), 400

        try:
            # Before ``request.get_data()``, deliberately. A chunk aimed at the wrong
            # offset is refused on two header values and the row already in hand, so
            # the commonest refusal on this endpoint never buffers a body or sniffs it.
            ensure_offset_matches(upload_session, offset)

            data = request.get_data()
            if not data:
                return jsonify({'error': 'Chunk body is empty.'}), 400

            if offset == 0:
                # Cheap early rejection: sniffing the first slice fails a disallowed
                # type after 8 MiB instead of after 470 MB. Advisory only — the
                # authoritative check runs on the assembled file at complete, and a
                # client controlling its first slice can pass here and still be refused
                # there, which is exactly what should happen.
                _, err = validate_file_header(upload_session.original_filename, data[:2048])
                if err:
                    return jsonify({'error': err}), 400

            received_bytes = append_chunk(
                db.session, upload_session, current_app.config['UPLOAD_FOLDER'],
                data, offset, request.headers.get('X-Chunk-SHA256'),
            )
        except UploadError as err:
            return _upload_error(err)

        return jsonify({'received_bytes': received_bytes})

    @app.route('/share/<token>/upload/complete/<upload_id>', methods=['POST'])
    @login_required
    @limiter.limit("600 per hour")
    def upload_complete(token, upload_id):
        """
        Turn a fully-received partial into a Photo and close out the session.

        Nothing is concatenated here — the chunks were appended in place, so the
        ``.part`` file already *is* the finished file. This validates it, hands it to
        the same storage routine the single-request path uses, records the row and
        drops the session. Per-file validation failures come back inside ``results``
        with HTTP 200, matching do_upload so uploader.js can share one response handler.
        """
        album = _album_for_upload(token)
        upload_session = _session_for_upload(album, upload_id)
        upload_dir = current_app.config['UPLOAD_FOLDER']
        partial = upload_session.partial_path(upload_dir)
        filename = upload_session.original_filename

        if upload_session.received_bytes != upload_session.total_size:
            # Same answer as a mis-aimed chunk, and for the same reason: hand back the
            # true cursor so the client resumes from there instead of starting over.
            return _upload_error(OffsetMismatch(upload_session.received_bytes))

        if not partial.exists():
            return _upload_error(SessionUnusable('Partial file for this upload is missing'))

        # The security boundary. The first-chunk sniff only ever saw a slice; this is
        # the first look at the whole artefact, and it runs no matter what came before.
        mime_type, err = validate_stored_file(partial, filename)
        if err:
            discard_session(db.session, upload_session, upload_dir)
            return jsonify({'results': [{'filename': filename, 'error': err}]})

        try:
            stored_name, file_size, has_thumbnail, taken_at = store_upload(
                partial, filename, mime_type)
        except Exception as exc:
            # Discard on the way out, exactly as the validation branch above does.
            # Keeping the session would cost the user twice over: the row holds a
            # concurrency slot and its full declared size against the byte quota for
            # the rest of the TTL, and the partial holds the disk. It would buy nothing
            # in return — every retry replays the same conversion over the same bytes
            # and fails the same way, at 600 attempts an hour. A transient failure does
            # mean the user has to re-send the file; that is the deliberate trade, and
            # it is the same one a failed validation already makes.
            discard_session(db.session, upload_session, upload_dir)
            return jsonify({'results': [{'filename': filename,
                                        'error': _store_failure_message(exc)}]})

        photo = Photo(
            album_id=album.id,
            uploader_id=current_user.id,
            uploader_name=current_user.username,
            stored_filename=stored_name,
            original_filename=secure_filename(filename)[:200],
            mime_type=mime_type,
            file_size=file_size,
            has_thumbnail=has_thumbnail,
            taken_at=taken_at,
        )
        db.session.add(photo)

        if not db.session.query(AlbumAccess).filter_by(
            user_id=current_user.id, album_id=album.id
        ).first():
            db.session.add(AlbumAccess(user_id=current_user.id, album_id=album.id))

        # discard_session commits, which lands the Photo and AlbumAccess inserts in the
        # same transaction as the session delete — the row and the thing that replaces
        # it can never both exist, or both be missing.
        discard_session(db.session, upload_session, upload_dir)
        return jsonify({'results': [{'filename': filename, 'success': True}]})

    @app.route('/share/<token>/download')
    @login_required
    def download_album_share(token):
        """Stream a ZIP of all album photos to a user accessing the album via its upload share link."""
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        buf = build_album_zip(album)
        zip_name = secure_filename(album.name or 'album') + '.zip'
        return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)

    @app.route('/view/<view_token>/download')
    @login_required
    def download_album_view(view_token):
        """Stream a ZIP of all album photos to a user accessing the album via its view-only share link."""
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        buf = build_album_zip(album)
        zip_name = secure_filename(album.name or 'album') + '.zip'
        return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)
