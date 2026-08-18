/*
 * Shared file uploader with per-file progress.
 *
 * Uploads one file per request through a small concurrency pool so each file
 * gets its own byte-level progress stream (XMLHttpRequest.upload.onprogress —
 * fetch() exposes no such events, which is why this is not written with fetch).
 *
 * Per-file lifecycle: pending -> uploading -> processing -> done | error.
 * The 'processing' phase matters: the server converts HEIC and builds
 * thumbnails after the last byte lands, so 100% sent is not 100% complete.
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
  var MAX_AUTO_RETRIES = 1;     // automatic retries on network failure
  var WATCHDOG_MS = 1000;       // how often in-flight items are re-checked
  var STALL_WARN_MS = 20000;    // no bytes moved this long -> warn, offer retry
  var STALL_FAIL_MS = 90000;    // ...still nothing -> abort so the row is retryable
  var PROCESS_WARN_MS = 60000;  // server-side processing running unusually long

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

  function Uploader(options) {
    this.url = options.url;
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
      };
      self._buildRow(item);
      self.items.push(item);
      self.listEl.appendChild(item.nodes.root);
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
      item.xhr.abort();
    }
    item.retries = 0;
    item.error = null;
    this._setStatus(item, 'pending', 'Pending');
    var self = this;
    this._startWatchdog();
    this._uploadOne(item).then(function () {
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
     so a long server-side conversion reads as working, not frozen. */
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
      return self._uploadOne(item).then(worker);
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
    this.items.forEach(function (it) { if (it.xhr) it.xhr.abort(); });
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
