/*
 * Shared file uploader with per-file progress.
 *
 * Two transports live behind one interface:
 *
 *   - Small files (<= CHUNK_THRESHOLD) take the legacy path: one multipart
 *     POST to `url`, byte progress straight off XMLHttpRequest.upload. This
 *     path is untouched and is still the only path when crypto.subtle is
 *     unavailable.
 *   - Large files are sliced into CHUNK_SIZE pieces and pushed through the
 *     chunked protocol in docs/upload_protocol.md — init, N x chunk,
 *     complete. Cloudflare's free plan drops any request body over 100 MB at
 *     the edge, so a 470 MB video never reached the origin at all; slicing it
 *     keeps every request comfortably small, and makes the transfer resumable
 *     as a side effect.
 *
 * XHR rather than fetch throughout: fetch() exposes no upload progress events,
 * and progress is the entire point of this file.
 *
 * Per-file lifecycle: pending -> uploading -> processing -> done | error.
 * The 'processing' phase matters: the server converts HEIC and builds
 * thumbnails after the last byte lands, so 100% sent is not 100% complete. On
 * the chunked path 'processing' begins when `complete` is issued rather than
 * when the last chunk's bytes land — the bytes are merely on disk at that
 * point, and all the slow work happens inside that final request.
 *
 * Resumability: `client_key -> upload_id` is cached in localStorage, keyed by
 * album token. Re-picking a file that has a live session probes the server and
 * restarts from its authoritative offset. Browsers cannot re-open a File across
 * a reload, so the user must re-select the file; only the transfer resumes.
 * localStorage being absent or full degrades to non-resumable, never to broken.
 *
 * A watchdog covers the two ways an upload can look alive while being dead:
 * a connection that hangs mid-transfer (XHR fires no event for this, so idle
 * time between progress events is measured instead), and a 'processing' phase
 * that never returns. Note that xhr.timeout is deliberately NOT used — it caps
 * total request duration, which would kill legitimately slow large videos.
 *
 * Used by templates/album_upload.html and templates/album_view.html.
 */
(function (global) {
  'use strict';

  var CONCURRENCY = 3;          // parallel uploads
  var RATE_WINDOW_MS = 3000;    // rolling window for the transfer-rate estimate
  var RATE_MIN_ELAPSED_MS = 500;// don't show a rate before this much has elapsed
  var RATE_MIN_BYTES = 1048576; // ...or for files smaller than this (1 MB)
  var MAX_AUTO_RETRIES = 1;     // automatic retries on network failure (per chunk)
  var WATCHDOG_MS = 1000;       // how often in-flight items are re-checked
  var STALL_WARN_MS = 20000;    // no bytes moved this long -> warn, offer retry
  var STALL_FAIL_MS = 90000;    // ...still nothing -> abort so the row is retryable
  var PROCESS_WARN_MS = 60000;  // server-side processing running unusually long

  /* Chunked path. CHUNK_SIZE mirrors UPLOAD_CHUNK_SIZE in the protocol doc,
     but the server's `init` response is authoritative and overrides it. */
  var CHUNK_SIZE = 8388608;         // 8 MiB
  var CHUNK_THRESHOLD = 8388608;    // at or under this, use the legacy path
  var RETRY_DELAY_MS = 800;         // same backoff the legacy path uses
  var RATE_LIMIT_DELAY_MS = 5000;   // 429 deserves a longer pause than a blip
  var MAX_OFFSET_RESYNCS = 5;       // consecutive 409s before calling it a livelock
  var MAX_SESSION_RESTARTS = 1;     // 404 -> re-init from zero, at most this often
  var RESUME_TTL_MS = 86400000;     // 24h, mirroring UPLOAD_SESSION_TTL_HOURS
  var RESUME_KEY_PREFIX = 'pv.upload.resume.';

  function humanSize(bytes) {
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (bytes >= 1024 && i < 3) { bytes /= 1024; i++; }
    return bytes.toFixed(1) + ' ' + units[i];
  }

  function humanEta(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '';
    if (seconds < 60) return Math.max(1, Math.round(seconds)) + 's left';
    var m = Math.floor(seconds / 60);
    if (m < 60) return m + 'm ' + Math.round(seconds % 60) + 's left';
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm left';
  }

  function humanDuration(ms) {
    var s = Math.round(ms / 1000);
    if (s < 60) return s + 's';
    return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  }

  /* Map a transport-level failure to something the user can act on. The
     generic catch-all in the old code reported every one of these as
     "Failed", which is precisely the ambiguity issue #27 is about. */
  function errorForStatus(status, body) {
    if (body && body.error) return body.error;
    switch (status) {
      case 401: return 'Session expired — reload the page';
      case 403: return 'Uploads are disabled for this album';
      case 404: return 'Album not found';
      case 413: return 'File too large';
      case 429: return 'Too many uploads — wait a moment, then retry';
      default:  return status ? 'Upload failed (HTTP ' + status + ')' : 'Upload failed';
    }
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // ── Crypto helpers ────────────────────────────────────────────────────────
  //
  // crypto.subtle only exists in a secure context. Production is HTTPS and
  // http://localhost also qualifies, but a plain-HTTP deployment would find it
  // missing — so every caller here treats absence as "no chunking available"
  // rather than as an exception.

  function subtleCrypto() {
    try {
      return (global.crypto && global.crypto.subtle) || null;
    } catch (e) {
      return null;
    }
  }

  function toHex(buffer) {
    var view = new Uint8Array(buffer);
    var out = '';
    for (var i = 0; i < view.length; i++) {
      out += (view[i] < 16 ? '0' : '') + view[i].toString(16);
    }
    return out;
  }

  /* TextEncoder ships alongside crypto.subtle everywhere it matters, but the
     escape/encodeURIComponent trick is a two-line UTF-8 encoder and filenames
     are routinely non-ASCII — not worth the risk of a wrong digest. */
  function utf8Bytes(str) {
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(str);
    var binary = unescape(encodeURIComponent(str));
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i) & 0xff;
    return bytes;
  }

  /* Resolves to lowercase hex, or to null when digesting is impossible.
     Never rejects: callers treat a null digest as a capability gap. */
  function sha256Hex(data) {
    var subtle = subtleCrypto();
    if (!subtle) return Promise.resolve(null);
    var bytes = typeof data === 'string' ? utf8Bytes(data) : data;
    try {
      return Promise.resolve(subtle.digest('SHA-256', bytes)).then(toHex, function () {
        return null;
      });
    } catch (e) {
      return Promise.resolve(null);
    }
  }

  /* §7 of the protocol: sha256_hex(name \0 size \0 lastModified). The server
     never recomputes this — it is validated as 64 hex chars and used purely as
     an idempotency key — so the two sides cannot drift over hashing details.
     Including size and mtime means an edited file yields a different key and
     starts a new session instead of resuming into a mismatched prefix. */
  function fingerprintSource(file) {
    var mtime = file.lastModified;
    if (mtime == null) {
      mtime = file.lastModifiedDate ? file.lastModifiedDate.getTime() : 0;
    }
    return file.name + '\0' + file.size + '\0' + mtime;
  }

  /* Blob.arrayBuffer() would be one line but is missing from Safari < 14 and
     every pre-Chromium Edge; FileReader is universally available. The buffer is
     needed in memory anyway to compute the chunk digest, so reading it costs
     nothing extra over sending the Blob directly. */
  function readSlice(file, start, end) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };
      reader.onerror = function () { reject(reader.error || new Error('read failed')); };
      reader.readAsArrayBuffer(file.slice(start, end));
    });
  }

  // ── Resume store ──────────────────────────────────────────────────────────
  //
  // client_key -> { id: upload_id, at: timestamp }, one localStorage entry per
  // album so two albums holding the same file never hand each other a session
  // id the server will reject as not-theirs.

  function ResumeStore(namespace) {
    this.key = RESUME_KEY_PREFIX + namespace;
    this.enabled = false;
    try {
      /* Private browsing and some embedded webviews throw on the *first*
         access, not on construction, so probe with a real round trip. */
      var probe = this.key + '.probe';
      global.localStorage.setItem(probe, '1');
      global.localStorage.removeItem(probe);
      this.enabled = true;
    } catch (e) {
      this.enabled = false;
    }
    this.prune();
  }

  ResumeStore.prototype._read = function () {
    if (!this.enabled) return {};
    try {
      return JSON.parse(global.localStorage.getItem(this.key)) || {};
    } catch (e) {
      return {};
    }
  };

  ResumeStore.prototype._write = function (map) {
    if (!this.enabled) return;
    try {
      global.localStorage.setItem(this.key, JSON.stringify(map));
    } catch (e) {
      /* Quota exhausted. Our own entries are tiny, so the pressure is almost
         certainly someone else's — drop ours entirely and stop trying. Losing
         resumability is a far better outcome than breaking the upload. */
      try { global.localStorage.removeItem(this.key); } catch (e2) { /* ignore */ }
      this.enabled = false;
    }
  };

  /* Entries outlive their server sessions if the user never returns, so expire
     them on the same TTL the server uses rather than growing without bound. */
  ResumeStore.prototype.prune = function () {
    if (!this.enabled) return;
    var map = this._read();
    var cutoff = Date.now() - RESUME_TTL_MS;
    var changed = false;
    Object.keys(map).forEach(function (k) {
      var entry = map[k];
      if (!entry || !entry.id || !(entry.at > cutoff)) {
        delete map[k];
        changed = true;
      }
    });
    if (changed) this._write(map);
  };

  ResumeStore.prototype.get = function (clientKey) {
    if (!clientKey) return null;
    var entry = this._read()[clientKey];
    return entry && entry.id ? entry.id : null;
  };

  ResumeStore.prototype.set = function (clientKey, uploadId) {
    if (!clientKey || !uploadId || !this.enabled) return;
    var map = this._read();
    map[clientKey] = { id: uploadId, at: Date.now() };
    this._write(map);
  };

  ResumeStore.prototype.evict = function (clientKey) {
    if (!clientKey || !this.enabled) return;
    var map = this._read();
    if (!(clientKey in map)) return;
    delete map[clientKey];
    this._write(map);
  };

  /* The templates hand us only the legacy upload URL, and the chunked routes
     hang off it: /share/<token>/upload/{init,status,chunk,complete}. Deriving
     them here keeps the template contract unchanged. */
  function uploadBase(url) {
    return String(url || '').split('#')[0].split('?')[0].replace(/\/+$/, '');
  }

  function albumNamespace(url) {
    var m = /\/(?:share|view)\/([^/?#]+)/.exec(String(url || ''));
    return m ? m[1] : 'default';
  }

  function clamp(value, max) {
    if (typeof value !== 'number' || !isFinite(value)) return 0;
    return Math.max(0, Math.min(Math.floor(value), max));
  }

  function pctOf(loaded, total) {
    return total ? Math.floor((loaded / total) * 100) : 0;
  }

  function Uploader(options) {
    this.url = options.url;
    this.chunkBase = uploadBase(options.url);
    this.resumeStore = new ResumeStore(albumNamespace(options.url));
    this.dropZone = options.dropZone || null;
    this.fileInput = options.fileInput || null;
    this.listEl = options.listEl;
    this.totalWrap = options.totalWrap || null;
    this.totalBar = options.totalBar || null;
    this.submitBtn = options.submitBtn || null;
    this.onComplete = options.onComplete || function () {};
    this.onSelectionChange = options.onSelectionChange || function () {};

    this.items = [];
    this.running = false;
    this._watchdog = null;

    this._bindInputs();
    this._syncSubmit();
  }

  Uploader.prototype._bindInputs = function () {
    var self = this;

    if (this.fileInput) {
      this.fileInput.addEventListener('change', function () {
        self.addFiles(Array.prototype.slice.call(self.fileInput.files));
        self.fileInput.value = '';
      });
    }

    if (this.dropZone) {
      this.dropZone.addEventListener('dragover', function (e) {
        e.preventDefault();
        self.dropZone.classList.add('dragover');
      });
      this.dropZone.addEventListener('dragleave', function () {
        self.dropZone.classList.remove('dragover');
      });
      this.dropZone.addEventListener('drop', function (e) {
        e.preventDefault();
        self.dropZone.classList.remove('dragover');
        self.addFiles(Array.prototype.slice.call(e.dataTransfer.files));
      });
    }
  };

  Uploader.prototype.addFiles = function (files) {
    var self = this;
    files.forEach(function (file) {
      var dup = self.items.some(function (it) {
        return it.file.name === file.name && it.file.size === file.size;
      });
      if (dup) return;

      var item = {
        file: file,
        status: 'pending',
        loaded: 0,
        samples: [],
        retries: 0,
        error: null,
        xhr: null,
        lastProgressAt: 0,
        processingAt: 0,
        stalled: false,
        stallAbort: false,
        // Chunked-path state.
        cancelled: false,     // an abort must stop the chunk loop, not just the socket
        runId: 0,             // fences a superseded loop off from a fresh one
        clientKey: null,
        uploadId: null,
        completedBytes: 0,    // bytes the server has confirmed on disk
      };
      self._buildRow(item);
      self.items.push(item);
      self.listEl.appendChild(item.nodes.root);
      /* Probed on add rather than on start: the user should be told the file is
         resumable while deciding whether to upload it, not after committing. */
      self._prepareResume(item);
    });
    this._syncSubmit();
    this.onSelectionChange(this.items.length);
  };

  Uploader.prototype._buildRow = function (item) {
    var self = this;

    var root = el('div', 'pv-up-item is-pending');
    var row = el('div', 'pv-up-row');
    var name = el('span', 'pv-up-name', item.file.name);
    name.title = item.file.name;
    var size = el('span', 'pv-up-size', humanSize(item.file.size));
    var status = el('span', 'pv-up-status is-pending', 'Pending');

    var remove = el('button', 'pv-up-btn', '✕');
    remove.type = 'button';
    remove.setAttribute('aria-label', 'Remove ' + item.file.name);
    remove.addEventListener('click', function () { self.removeItem(item); });

    var retry = el('button', 'pv-up-btn pv-up-retry', 'Retry');
    retry.type = 'button';
    retry.style.display = 'none';
    retry.addEventListener('click', function () { self.retryItem(item); });

    row.appendChild(name);
    row.appendChild(size);
    row.appendChild(status);
    row.appendChild(retry);
    row.appendChild(remove);

    var barWrap = el('div', 'pv-up-bar-wrap');
    var bar = el('div', 'pv-up-bar');
    barWrap.appendChild(bar);

    var meta = el('div', 'pv-up-meta', '');

    root.appendChild(row);
    root.appendChild(barWrap);
    root.appendChild(meta);

    item.nodes = {
      root: root, status: status, bar: bar, meta: meta,
      remove: remove, retry: retry,
    };
  };

  Uploader.prototype.removeItem = function (item) {
    /* Set before the abort, and unconditionally: a pending row may still have
       a resume probe in flight, and the chunk loop must not queue another
       request after the socket it was watching goes away. */
    item.cancelled = true;
    if (item.status === 'uploading' || item.status === 'processing') {
      if (item.xhr) item.xhr.abort();
    }
    var i = this.items.indexOf(item);
    if (i !== -1) this.items.splice(i, 1);
    if (item.nodes.root.parentNode) {
      item.nodes.root.parentNode.removeChild(item.nodes.root);
    }
    this._syncSubmit();
    this._renderTotal();
    this.onSelectionChange(this.items.length);
  };

  Uploader.prototype.retryItem = function (item) {
    if (item.status !== 'error' && !item.stalled) return;
    // Restarting a stalled transfer: drop the dead socket first. The abort is
    // user-driven, so it must not be reported as a stall failure.
    if (item.stalled && item.xhr) {
      item.stallAbort = false;
      item.cancelled = true;
      item.xhr.abort();
    }
    item.retries = 0;
    item.error = null;
    this._setStatus(item, 'pending', 'Pending');
    var self = this;
    this._startWatchdog();
    this._upload(item).then(function () {
      self._renderTotal();
      self._maybeFinish();
    });
  };

  Uploader.prototype._setStatus = function (item, status, text) {
    item.status = status;
    item.stalled = false;
    item.nodes.root.className = 'pv-up-item is-' + status;
    item.nodes.status.className = 'pv-up-status is-' + status;
    item.nodes.status.textContent = text;
    item.nodes.retry.style.display = status === 'error' ? '' : 'none';
    if (status !== 'uploading') item.nodes.meta.textContent = '';
  };

  /* Rate from a rolling window rather than a cumulative average, so the
     readout tracks the current connection instead of lagging behind it. */
  Uploader.prototype._rate = function (item) {
    var s = item.samples;
    if (s.length < 2) return 0;
    var first = s[0], last = s[s.length - 1];
    var dt = (last.t - first.t) / 1000;
    if (dt <= 0) return 0;
    return (last.loaded - first.loaded) / dt;
  };

  /* The chunked path feeds this `completedBytes + e.loaded` for the chunk in
     flight, so `loaded` stays whole-file absolute and every consumer below —
     the window, the ETA, the byte-weighted total bar — works unchanged. */
  Uploader.prototype._onProgress = function (item, loaded, total) {
    item.loaded = loaded;
    var now = Date.now();
    item.lastProgressAt = now;
    if (item.stalled) this._clearStall(item);

    item.samples.push({ t: now, loaded: loaded });
    while (item.samples.length > 2 && now - item.samples[0].t > RATE_WINDOW_MS) {
      item.samples.shift();
    }

    var pct = total ? Math.floor((loaded / total) * 100) : 0;
    item.nodes.bar.style.width = pct + '%';
    item.nodes.status.textContent = pct + '%';

    // Suppress rate/ETA where it would be meaningless noise.
    var elapsed = now - item.startedAt;
    if (total >= RATE_MIN_BYTES && elapsed >= RATE_MIN_ELAPSED_MS) {
      var rate = this._rate(item);
      if (rate > 0) {
        var eta = humanEta((total - loaded) / rate);
        item.nodes.meta.textContent = humanSize(rate) + '/s' + (eta ? ' · ' + eta : '');
      }
    }

    this._renderTotal();
  };

  Uploader.prototype._markStall = function (item, idleMs) {
    item.stalled = true;
    item.nodes.root.classList.add('is-stalled');
    item.nodes.status.classList.add('is-stalled');
    item.nodes.status.textContent = 'Stalled';
    item.nodes.retry.style.display = '';
    item.nodes.meta.textContent =
      'No data sent for ' + humanDuration(idleMs) + ' — retry, or check your connection';
  };

  Uploader.prototype._clearStall = function (item) {
    item.stalled = false;
    item.nodes.root.classList.remove('is-stalled');
    item.nodes.status.classList.remove('is-stalled');
    item.nodes.retry.style.display = 'none';
  };

  /* Runs while anything is in flight. Uploading rows are judged on idle time
     since the last progress event; processing rows get a live elapsed counter
     so a long server-side conversion reads as working, not frozen.

     On the chunked path item.xhr is the chunk currently in flight, so the
     abort below still lands on a live socket — and _uploadChunked treats any
     abort as terminal for the whole loop rather than for one request. */
  Uploader.prototype._checkStalls = function () {
    var self = this;
    var now = Date.now();
    var busy = false;

    this.items.forEach(function (item) {
      if (item.status === 'uploading') {
        busy = true;
        var idle = now - item.lastProgressAt;
        if (idle >= STALL_FAIL_MS) {
          item.stallAbort = true;
          if (item.xhr) item.xhr.abort();
        } else if (idle >= STALL_WARN_MS) {
          self._markStall(item, idle);
        }
      } else if (item.status === 'processing') {
        busy = true;
        var waited = now - item.processingAt;
        item.nodes.meta.textContent = waited >= PROCESS_WARN_MS
          ? 'Still processing after ' + humanDuration(waited) + ' — large videos take a while'
          : 'Processing for ' + humanDuration(waited);
      }
    });

    if (!busy) this._stopWatchdog();
  };

  Uploader.prototype._startWatchdog = function () {
    if (this._watchdog) return;
    var self = this;
    this._watchdog = setInterval(function () { self._checkStalls(); }, WATCHDOG_MS);
  };

  Uploader.prototype._stopWatchdog = function () {
    if (!this._watchdog) return;
    clearInterval(this._watchdog);
    this._watchdog = null;
  };

  // ── Transport selection ───────────────────────────────────────────────────

  /* Chunking needs SHA-256 per chunk, which needs a secure context. Without it
     the only honest option is the legacy path — it may hit an edge body limit
     for very large files, but that is exactly today's behaviour, not a
     regression introduced here. */
  Uploader.prototype._canChunk = function (file) {
    return file.size > CHUNK_THRESHOLD && !!subtleCrypto();
  };

  /* Single entry point for starting (or restarting) an item.

     Every run gets a fresh id, and the chunk loop compares its captured id
     against the item's on each step. That closes a race an `item.cancelled`
     flag cannot close on its own: abort() settles its promise in a microtask,
     by which time a synchronous retryItem() has already cleared the flag and
     the superseded loop would happily fire the next chunk. */
  Uploader.prototype._upload = function (item) {
    item.runId = (item.runId || 0) + 1;
    item.cancelled = false;
    return this._canChunk(item.file) ? this._uploadChunked(item) : this._uploadOne(item);
  };

  /* One request in either protocol. Resolves — never rejects — with a uniform
     outcome so the chunk loop can branch on status without try/catch around
     every step. `item.xhr` is repointed here, which is what keeps removeItem,
     retryItem and the stall watchdog working per chunk with no changes.

     xhr.timeout is left unset for the same reason as the legacy path: it caps
     total request duration, and a chunk on a bad connection is slow, not dead.
     The watchdog decides what "dead" means. */
  Uploader.prototype._request = function (item, opts) {
    return new Promise(function (resolve) {
      var xhr = new XMLHttpRequest();
      item.xhr = xhr;

      xhr.open(opts.method, opts.url, true);
      xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
      if (opts.contentType) xhr.setRequestHeader('Content-Type', opts.contentType);
      if (opts.headers) {
        Object.keys(opts.headers).forEach(function (name) {
          xhr.setRequestHeader(name, opts.headers[name]);
        });
      }
      if (opts.onProgress) {
        xhr.upload.onprogress = function (e) {
          if (e.lengthComputable) opts.onProgress(e);
        };
      }

      var settled = false;
      function settle(outcome) {
        if (settled) return;
        settled = true;
        resolve(outcome);
      }

      xhr.onload = function () {
        var body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) { /* non-JSON */ }
        settle({ status: xhr.status, body: body });
      };
      xhr.onerror = function () { settle({ status: 0, body: null, network: true }); };
      xhr.ontimeout = function () { settle({ status: 0, body: null, network: true }); };
      xhr.onabort = function () { settle({ status: 0, body: null, aborted: true }); };

      xhr.send(opts.body == null ? null : opts.body);
    });
  };

  // ── Resume probe ──────────────────────────────────────────────────────────

  /* Best-effort, fire-and-forget: a failure here only costs resumability.
     Runs on file add so a recovered percentage is visible before the user
     commits to uploading. */
  Uploader.prototype._prepareResume = function (item) {
    var self = this;
    if (!this._canChunk(item.file)) return;

    // Still pending, still in the list, still this file.
    function stillRelevant() {
      return !item.cancelled && item.status === 'pending';
    }

    sha256Hex(fingerprintSource(item.file)).then(function (key) {
      if (!key || !stillRelevant()) return;
      item.clientKey = key;

      var uploadId = self.resumeStore.get(key);
      if (!uploadId) return;

      return self._request(item, {
        method: 'GET',
        url: self.chunkBase + '/status/' + encodeURIComponent(uploadId),
      }).then(function (res) {
        if (!stillRelevant()) return;
        var body = res.body;

        /* 404 means unknown, expired or already completed — the protocol is
           explicit that the client treats all three identically. A size
           mismatch means the key collided with a different file. Either way
           the mapping is dead; a network error is not, so leave it alone. */
        if (res.status === 404 || (res.status === 200 && body && body.total_size !== item.file.size)) {
          self.resumeStore.evict(key);
          return;
        }
        if (res.status !== 200 || !body) return;

        var received = clamp(body.received_bytes, item.file.size);
        if (received <= 0) return;

        item.uploadId = uploadId;
        item.completedBytes = received;
        item.loaded = received;

        var pct = pctOf(received, item.file.size);
        self._setStatus(item, 'pending', 'Resuming at ' + pct + '%');
        item.nodes.root.classList.add('is-resumable');
        item.nodes.bar.style.width = pct + '%';
        item.nodes.meta.textContent =
          humanSize(received) + ' of ' + humanSize(item.file.size) + ' already uploaded';
        self._renderTotal();
      });
    }).catch(function () { /* resumability is a bonus; never let it break an upload */ });
  };

  // ── Legacy single-request transport ───────────────────────────────────────

  Uploader.prototype._uploadOne = function (item) {
    var self = this;

    return new Promise(function (resolve) {
      // Started here rather than only in start(): the auto-retry path re-enters
      // _uploadOne after a gap in which the watchdog may have stopped itself.
      self._startWatchdog();

      var form = new FormData();
      form.append('files', item.file);

      var xhr = new XMLHttpRequest();
      item.xhr = xhr;
      item.loaded = 0;
      item.samples = [];
      item.startedAt = Date.now();
      item.lastProgressAt = item.startedAt;
      item.processingAt = 0;
      item.stallAbort = false;
      self._setStatus(item, 'uploading', '0%');
      item.nodes.bar.style.width = '0%';

      xhr.open('POST', self.url, true);
      xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');

      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) self._onProgress(item, e.loaded, e.total);
      };

      // Body fully sent; the server is now converting/thumbnailing.
      xhr.upload.onload = function () {
        item.loaded = item.file.size;
        item.processingAt = Date.now();
        self._setStatus(item, 'processing', 'Processing…');
        self._renderTotal();
      };

      function fail(message, retryable) {
        if (retryable && item.retries < MAX_AUTO_RETRIES) {
          item.retries++;
          self._setStatus(item, 'pending', 'Retrying…');
          setTimeout(function () { self._uploadOne(item).then(resolve); }, 800);
          return;
        }
        item.error = message;
        self._setStatus(item, 'error', message);
        self._renderTotal();
        resolve();
      }

      xhr.onload = function () {
        var body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) { /* non-JSON */ }

        if (xhr.status < 200 || xhr.status >= 300) {
          // 5xx may be transient; 4xx will fail identically on retry.
          fail(errorForStatus(xhr.status, body), xhr.status >= 500);
          return;
        }

        // One file per request, so a single result is expected back.
        var result = body && body.results && body.results[0];
        if (result && result.success) {
          item.loaded = item.file.size;
          self._setStatus(item, 'done', '✓ Done');
        } else {
          fail((result && result.error) || (body && body.error) || 'Upload failed', false);
          return;
        }
        self._renderTotal();
        resolve();
      };

      xhr.onerror = function () { fail('Network error', true); };
      xhr.ontimeout = function () { fail('Timed out', true); };
      xhr.onabort = function () {
        if (item.stallAbort) {
          item.stallAbort = false;
          fail('Stalled — no data for ' + humanDuration(STALL_FAIL_MS), false);
          return;
        }
        resolve();
      };

      xhr.send(form);
    });
  };

  // ── Chunked transport ─────────────────────────────────────────────────────

  /* init -> chunk* -> complete, per docs/upload_protocol.md. Written as a
     hand-rolled loop of mutually-tail-calling steps rather than a chain,
     because 409 (re-seek) and 404 (re-init) both jump backwards and a chain
     cannot express that without unwinding.

     Resolves — never rejects — exactly like _uploadOne, so start() and
     retryItem() cannot tell the two transports apart. */
  Uploader.prototype._uploadChunked = function (item) {
    var self = this;
    var runId = item.runId;
    var base = this.chunkBase;
    var chunkSize = CHUNK_SIZE;   // overridden by the server's init response
    var chunkRetries = 0;         // MAX_AUTO_RETRIES is per chunk, not per file
    var resyncs = 0;              // consecutive 409s
    var restarts = 0;             // 404 -> fresh session

    return new Promise(function (resolve) {
      self._startWatchdog();

      item.samples = [];
      item.startedAt = Date.now();
      item.lastProgressAt = item.startedAt;
      item.processingAt = 0;
      item.stallAbort = false;
      /* Not zero: a resumed row already has confirmed bytes on the server, and
         `loaded` is whole-file absolute throughout. */
      item.completedBytes = clamp(item.completedBytes, item.file.size);
      item.loaded = item.completedBytes;
      self._setStatus(item, 'uploading', pctOf(item.loaded, item.file.size) + '%');
      item.nodes.bar.style.width = pctOf(item.loaded, item.file.size) + '%';

      /* The loop terminator. `cancelled` covers removeItem and reset;
         the runId comparison covers a run superseded by retryItem. */
      function stopped() {
        return item.cancelled || item.runId !== runId;
      }

      // The row is gone or superseded — leave the DOM alone and stand down.
      function bail() { resolve(); }

      function giveUp(message) {
        item.error = message;
        self._setStatus(item, 'error', message);
        self._renderTotal();
        resolve();
      }

      /* An abort is the one outcome that always ends the loop. The watchdog's
         stall abort is a failure; every other abort is the user removing,
         retrying or resetting the row, and has already been reported. */
      function afterAbort() {
        if (item.stallAbort) {
          item.stallAbort = false;
          giveUp('Stalled — no data for ' + humanDuration(STALL_FAIL_MS));
          return;
        }
        resolve();
      }

      function retryStep(step, message, delayMs) {
        if (chunkRetries >= MAX_AUTO_RETRIES) {
          giveUp(message);
          return;
        }
        chunkRetries++;
        /* The window is about to see `loaded` jump backwards to the start of
           the chunk; keeping the old samples would produce a negative rate. */
        item.samples = [];
        self._setStatus(item, 'uploading', 'Retrying…');
        // A deliberate backoff is not a stall — don't let the watchdog count it.
        item.lastProgressAt = Date.now();
        setTimeout(function () {
          if (stopped()) return bail();
          item.lastProgressAt = Date.now();
          step();
        }, delayMs || RETRY_DELAY_MS);
      }

      /* _prepareResume computes the fingerprint on file add, but it is async and
         the user can hit Upload before it lands — and it can legitimately come
         back null if subtle fails at runtime. Without a client_key the chunked
         protocol has nothing to key its session on, so fall back to the legacy
         transport rather than sending a request `init` will reject. */
      function begin() {
        if (item.clientKey) return init();
        sha256Hex(fingerprintSource(item.file)).then(function (key) {
          if (stopped()) return bail();
          if (!key) return self._uploadOne(item).then(resolve);
          item.clientKey = key;
          init();
        });
      }

      function seekTo(offset) {
        item.completedBytes = clamp(offset, item.file.size);
        item.samples = [];
        self._onProgress(item, item.completedBytes, item.file.size);
      }

      // §6.1 — idempotent on client_key, so this doubles as the resume call.
      function init() {
        if (stopped()) return bail();
        self._request(item, {
          method: 'POST',
          url: base + '/init',
          contentType: 'application/json',
          body: JSON.stringify({
            client_key: item.clientKey,
            filename: item.file.name,
            total_size: item.file.size,
            content_type: item.file.type || 'application/octet-stream',
          }),
        }).then(function (res) {
          if (res.aborted) return afterAbort();
          if (stopped()) return bail();
          if (res.network) return retryStep(init, 'Network error');

          if (res.status === 200 || res.status === 201) {
            var body = res.body || {};
            if (!body.upload_id) return giveUp('Upload failed (no session id)');
            item.uploadId = body.upload_id;
            if (body.chunk_size > 0) chunkSize = body.chunk_size;
            self.resumeStore.set(item.clientKey, item.uploadId);
            /* The server's counter wins over anything the status probe told
               us earlier — it is the value the next X-Upload-Offset must
               match, and it may have moved in another tab. */
            seekTo(body.received_bytes);
            chunkRetries = 0;
            return sendChunk();
          }
          if (res.status >= 500) return retryStep(init, errorForStatus(res.status, res.body));
          if (res.status === 429) {
            return retryStep(init, errorForStatus(429, res.body), RATE_LIMIT_DELAY_MS);
          }
          giveUp(errorForStatus(res.status, res.body));
        });
      }

      // §6.3 — raw bytes, offset and digest in headers.
      function sendChunk() {
        if (stopped()) return bail();
        if (item.completedBytes >= item.file.size) return complete();

        var start = item.completedBytes;
        var end = Math.min(start + chunkSize, item.file.size);

        readSlice(item.file, start, end).then(function (buffer) {
          if (stopped()) return bail();
          return sha256Hex(buffer).then(function (digest) {
            if (stopped()) return bail();
            /* _canChunk already established subtle exists; a null here means
               it failed mid-flight, and sending an unverifiable chunk would
               just earn a 422 forever. */
            if (!digest) return giveUp('Could not checksum this file in your browser');

            return self._request(item, {
              method: 'POST',
              url: base + '/chunk/' + encodeURIComponent(item.uploadId),
              contentType: 'application/octet-stream',
              headers: {
                'X-Upload-Offset': String(start),
                'X-Chunk-SHA256': digest,
              },
              body: buffer,
              onProgress: function (e) {
                self._onProgress(item, start + e.loaded, item.file.size);
              },
            }).then(function (res) { onChunkResponse(res, end); });
          });
        }, function () {
          /* The File handle went stale — the file was moved, renamed or
             replaced on disk mid-upload. Retrying reads the same dead handle. */
          if (stopped()) return bail();
          giveUp('Could not read the file — it may have been moved or changed');
        }).catch(function () {
          // Belt and braces: never leave the promise unsettled on a surprise.
          if (!stopped()) giveUp('Upload failed');
          resolve();
        });
      }

      function onChunkResponse(res, end) {
        if (res.aborted) return afterAbort();
        if (stopped()) return bail();
        if (res.network) return retryStep(sendChunk, 'Network error');

        if (res.status === 200) {
          var received = res.body && res.body.received_bytes;
          item.completedBytes = typeof received === 'number'
            ? clamp(received, item.file.size)
            : end;
          chunkRetries = 0;
          resyncs = 0;
          self._onProgress(item, item.completedBytes, item.file.size);
          return sendChunk();
        }

        if (res.status === 409) {
          /* Not an error. This is how a resuming client discovers where to
             seek, and two tabs racing the same session produce it legitimately.
             It costs no retry budget — but a server that keeps answering 409
             without the offset advancing is a livelock, so cap it. */
          var truth = res.body && res.body.received_bytes;
          if (typeof truth !== 'number' || ++resyncs > MAX_OFFSET_RESYNCS) {
            return giveUp('Upload is out of sync — retry to start it over');
          }
          seekTo(truth);
          return sendChunk();
        }

        if (res.status === 422) {
          // Bytes arrived corrupt. Same offset, same chunk, fresh attempt.
          return retryStep(sendChunk, 'Upload corrupted in transit — retry');
        }

        if (res.status === 404) return restartSession();
        if (res.status === 429) {
          return retryStep(sendChunk, errorForStatus(429, res.body), RATE_LIMIT_DELAY_MS);
        }
        if (res.status >= 500) {
          return retryStep(sendChunk, errorForStatus(res.status, res.body));
        }
        giveUp(errorForStatus(res.status, res.body));
      }

      /* The session expired or was swept while we were mid-transfer. The
         partial file is gone with it, so there is nothing to seek to — evict
         the mapping and start over from zero, once. */
      function restartSession() {
        if (restarts >= MAX_SESSION_RESTARTS) {
          return giveUp('Upload session expired — retry to start it over');
        }
        restarts++;
        self.resumeStore.evict(item.clientKey);
        item.uploadId = null;
        chunkRetries = 0;
        resyncs = 0;
        // A 404 out of `complete` leaves the row reading 'processing'; it is
        // uploading again as of the next line.
        self._setStatus(item, 'uploading', '0%');
        seekTo(0);
        init();
      }

      // §6.4 — empty body, and the same {results:[…]} envelope as the legacy
      // endpoint, so success and per-file failure are read the same way here.
      function complete() {
        if (stopped()) return bail();

        /* 'processing' starts here, not when the last chunk landed. Those bytes
           only reached a .part file; validation, HEIC conversion and
           thumbnailing all happen inside this request. */
        item.loaded = item.file.size;
        item.processingAt = Date.now();
        self._setStatus(item, 'processing', 'Processing…');
        self._renderTotal();

        self._request(item, {
          method: 'POST',
          url: base + '/complete/' + encodeURIComponent(item.uploadId),
        }).then(function (res) {
          if (res.aborted) return afterAbort();
          if (stopped()) return bail();
          if (res.network) return retryStep(complete, 'Network error');
          if (res.status === 404) return restartSession();

          if (res.status === 409) {
            /* The server has fewer bytes than we think it does — a chunk we
               counted as accepted did not survive. Same re-seek as mid-stream,
               against the same livelock guard, and the row goes back to
               'uploading' because that is what it is doing again. */
            var truth = res.body && res.body.received_bytes;
            if (typeof truth !== 'number' || ++resyncs > MAX_OFFSET_RESYNCS) {
              return giveUp('Upload is out of sync — retry to start it over');
            }
            self._setStatus(item, 'uploading', '0%');
            seekTo(truth);
            return sendChunk();
          }

          if (res.status < 200 || res.status >= 300) {
            if (res.status >= 500) {
              return retryStep(complete, errorForStatus(res.status, res.body));
            }
            if (res.status === 429) {
              return retryStep(complete, errorForStatus(429, res.body), RATE_LIMIT_DELAY_MS);
            }
            return giveUp(errorForStatus(res.status, res.body));
          }

          /* Whatever comes back, the session is finished server-side — keep
             localStorage from accumulating dead mappings. */
          self.resumeStore.evict(item.clientKey);

          var result = res.body && res.body.results && res.body.results[0];
          if (result && result.success) {
            item.loaded = item.file.size;
            self._setStatus(item, 'done', '✓ Done');
            self._renderTotal();
            resolve();
            return;
          }
          /* A validation failure rides inside results with HTTP 200 — it is a
             verdict on the file, not a transport fault, and re-sending the
             same bytes would earn the same verdict. */
          giveUp((result && result.error) || (res.body && res.body.error) || 'Upload failed');
        });
      }

      begin();
    });
  };

  Uploader.prototype._renderTotal = function () {
    if (!this.totalBar) return;
    var sent = 0, total = 0;
    this.items.forEach(function (it) {
      total += it.file.size;
      sent += it.status === 'done' ? it.file.size : Math.min(it.loaded, it.file.size);
    });
    // Weight by bytes so the bar advances smoothly rather than in file-sized jumps.
    this.totalBar.style.width = (total ? (sent / total) * 100 : 0) + '%';
  };

  Uploader.prototype._syncSubmit = function () {
    if (!this.submitBtn) return;
    var pending = this.items.some(function (it) {
      return it.status === 'pending' || it.status === 'error';
    });
    this.submitBtn.disabled = this.running || !pending;
  };

  Uploader.prototype._maybeFinish = function () {
    var busy = this.items.some(function (it) {
      return it.status === 'uploading' || it.status === 'processing';
    });
    if (busy) return;

    var succeeded = this.items.filter(function (it) { return it.status === 'done'; }).length;
    var failed = this.items.filter(function (it) { return it.status === 'error'; }).length;
    this.onComplete({ succeeded: succeeded, failed: failed, total: this.items.length });
  };

  Uploader.prototype.start = function () {
    if (this.running) return Promise.resolve();

    var queue = this.items.filter(function (it) {
      return it.status === 'pending' || it.status === 'error';
    });
    if (!queue.length) return Promise.resolve();

    var self = this;
    this.running = true;
    this._syncSubmit();
    this._startWatchdog();
    if (this.totalWrap) this.totalWrap.style.display = 'block';

    var next = 0;
    function worker() {
      if (next >= queue.length) return Promise.resolve();
      var item = queue[next++];
      return self._upload(item).then(worker);
    }

    var workers = [];
    for (var i = 0; i < Math.min(CONCURRENCY, queue.length); i++) {
      workers.push(worker());
    }

    return Promise.all(workers).then(function () {
      self.running = false;
      self._syncSubmit();
      self._maybeFinish();
    });
  };

  Uploader.prototype.reset = function () {
    this._stopWatchdog();
    /* Flag before aborting: the chunk loop checks `cancelled` on the way out
       of every request, and an abort alone would only kill the chunk in flight.
       Sessions are deliberately left in localStorage — the user cleared the
       list, not the upload, and re-adding the file should still resume. */
    this.items.forEach(function (it) {
      it.cancelled = true;
      if (it.xhr) it.xhr.abort();
    });
    this.items = [];
    this.listEl.innerHTML = '';
    this.running = false;
    if (this.totalWrap) this.totalWrap.style.display = 'none';
    if (this.totalBar) this.totalBar.style.width = '0%';
    this._syncSubmit();
    this.onSelectionChange(0);
  };

  Uploader.prototype.count = function () { return this.items.length; };

  global.PixelVaultUploader = Uploader;
  global.pvHumanSize = humanSize;
})(window);
