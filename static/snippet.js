/**
 * openanalytics — tracking snippet
 * --------------------------------------------------------------------------
 * Privacy-first by design:
 *   - No cookies. No localStorage. No fingerprinting beyond what the page
 *     already reveals to the server (IP + User-Agent, hashed server-side).
 *   - No third-party requests, no image pixels, no external libraries.
 *   - Sends one POST per pageview / custom event. Prefers `fetch` with
 *     `keepalive` (CORS-aware, survives page unload); `sendBeacon` is the
 *     fallback for browsers without fetch.
 *   - Fails silently. A broken analytics endpoint must never break your app.
 *
 * Install:
 *   <script async src="https://analytics.example.com/snippet.js"></script>
 *
 * That's it — pageviews are tracked automatically. For custom events:
 *   analytics.track('signup', { plan: 'pro' });
 *
 * Configure (optional, before loading this script):
 *   window.OPENANALYTICS = {
 *     url: 'https://analytics.example.com/track', // full endpoint
 *     exclude: [/\/search\?/],                    // don't track these URLs
 *     debug: true                                 // log to console
 *   };
 * --------------------------------------------------------------------------
 */
(function () {
  if (typeof window === 'undefined' || window.__oa) return;
  window.__oa = true;

  var cfg = window.OPENANALYTICS || {};
  // The server substitutes the full base URL (scheme included), so this
  // snippet works against both `http://` dev installs and `https://` prod.
  var url = cfg.url || '__OPENANALYTICS_URL__/track';
  var exclude = cfg.exclude || [];
  var debug = !!cfg.debug;

  function excluded(u) {
    for (var i = 0; i < exclude.length; i++) {
      if (exclude[i].test(u)) return true;
    }
    return false;
  }

  function log() {
    if (!debug) return;
    var a = Array.prototype.slice.call(arguments);
    console.log.apply(console, ['[openanalytics]'].concat(a));
  }

  // Collect the page payload. Only what we can see without extra permissions.
  function payload(event, data, url, referrer) {
    return {
      event: event,
      data: data || null,
      url: url || location.href,
      referrer: referrer || document.referrer || '',
      timestamp: Date.now()
    };
  }

  function send(ev) {
    var body = JSON.stringify(payload(ev.event, ev.data, ev.url, ev.referrer));
    log('track', ev.event, ev.url || location.href);

    // Prefer fetch with keepalive: it honours CORS (so it works when the
    // analytics host differs from the page's origin) and `keepalive: true`
    // lets it outlive a page unload, which is the job sendBeacon was made
    // for. sendBeacon stays as a fallback because it cannot be blocked by
    // CORS — but it runs in no-cors mode, where browsers strip non-safelisted
    // Content-Type values, so the server must be tolerant of undeclared JSON.
    var did = false;
    try {
      fetch(url, {
        method: 'POST',
        keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: body,
        credentials: 'omit'
      }).catch(function () {});
      did = true;
    } catch (e) {
      if (debug) log('fetch failed', e && e.message);
    }
    if (!did && navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
    }
  }

  var queue = [];
  var started = false;

  function flush() {
    started = true;
    while (queue.length) {
      var ev = queue.shift();
      if (!excluded(ev.url || location.href)) send(ev);
    }
  }

  // Queue if we haven't flushed yet; otherwise send straight away. Without
  // this, any track() call made after the first pageview would sit in the
  // queue forever and silently disappear.
  function submit(ev) {
    if (started) {
      if (!excluded(ev.url || location.href)) send(ev);
    } else {
      queue.push(ev);
    }
  }

  // Auto-track the initial pageview. We queue it up front and let `flush`
  // send it once the document has loaded far enough to be meaningful.
  submit({ event: 'pageview', data: null, url: location.href, referrer: document.referrer || '' });

  // Pageviews can only fire once the document has loaded enough to be
  // meaningful. Queue early calls and flush on DOMContentLoaded/load.
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(flush, 0);
  } else {
    document.addEventListener('DOMContentLoaded', flush);
  }

  // Public API — expose immediately so it is available before flush.
  var api = {
    track: function (event, data) {
      if (!event || typeof event !== 'string') {
        log('track(): event name must be a non-empty string');
        return;
      }
      submit({ event: event, data: data || null, url: location.href, referrer: document.referrer || '' });
    },
    pageview: function (url, referrer) {
      submit({
        event: 'pageview',
        data: null,
        url: url || location.href,
        referrer: referrer || document.referrer || ''
      });
    },
    _debug: function (v) {
      debug = !!v;
    }
  };

  // `analytics` is the documented global. Fall back to `oa` if taken.
  try {
    Object.defineProperty(window, 'analytics', { value: api, writable: true, configurable: true });
  } catch (e) {
    window.oa = api;
  }
})();
