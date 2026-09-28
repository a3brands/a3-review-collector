/* A3 Review Collector dashboard.
   The API key lives in localStorage and is sent as a Bearer token, the same way
   the A3 Review Responder authenticates. It is never baked into this file. */
(function () {
  "use strict";

  var KEY_STORAGE = "a3_collector_api_key";
  var REFRESH_MS = 30000;
  var PAGE = 10;

  // "new" = waiting for a reply, "all" = everything collected.
  //
  // Opens on "all". Opening on "new" showed 36 rows out of 5,072 and read as a
  // collector that had barely found anything; the reviews already handled are
  // the evidence that collection is working. Same reason the Responder
  // dashboard was moved off its Unreplied landing on 2026-09-10.
  var view = "all";
  var shown = PAGE;
  var business = "";
  var sortBy = "urgent";
  var timer = null;
  var reloading = false;
  var expanded = {};       // review_id -> true when the reader opened it

  // Version of the JS this page loaded, read off our own <script> tag. Compared
  // against the server each poll so a tab left open across an update reloads
  // itself instead of silently running stale code.
  var LOADED_VERSION = (function () {
    var tag = document.querySelector('script[src*="app.js"]');
    var m = tag && /[?&]v=([a-f0-9]+)/.exec(tag.getAttribute("src") || "");
    return m ? m[1] : null;
  })();

  function $(id) { return document.getElementById(id); }
  function apiKey() { return localStorage.getItem(KEY_STORAGE) || ""; }

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign({ Authorization: "Bearer " + apiKey() }, options.headers || {});
    return fetch(path, options).then(function (res) {
      if (res.status === 401 || res.status === 403) {
        var e = new Error("The API key was rejected."); e.auth = true; throw e;
      }
      return res.json().then(function (body) {
        if (!res.ok) throw new Error(body.detail || ("HTTP " + res.status));
        return body;
      });
    });
  }

  // ---------- formatting ----------
  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function asDate(iso) {
    if (!iso) return null;
    var d = new Date(/Z|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
    return isNaN(d) ? null : d;
  }
  function ago(iso) {
    var d = asDate(iso);
    if (!d) return "never";
    var s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    var days = Math.floor(s / 86400);
    if (days < 60) return days + " days ago";
    return Math.floor(days / 30) + " months ago";
  }
  function full(iso) {
    var d = asDate(iso);
    return d ? d.toLocaleString() : "never";
  }
  function stars(n) {
    if (!n) return '<span class="r-when">no rating</span>';
    return '<span class="stars' + (n <= 2 ? " low" : "") + '">' +
      "★".repeat(n) + "☆".repeat(5 - n) + "</span>";
  }

  // ---------- needs attention ----------
  function renderNeeds(data) {
    var q = data.queue || {};
    var box = $("needs");
    var waiting = q.waiting || 0;

    if (!waiting) {
      box.className = "needs calm";
      box.innerHTML =
        '<div><div class="needs-count">0</div><div class="needs-label">waiting for a reply</div></div>' +
        '<div class="needs-calm-text">All caught up. Every collected review has been handled.</div>';
      return;
    }

    var flags = [];
    if (q.negative) {
      flags.push('<span class="flag bad">' + q.negative + " negative (" +
        (q.worst_rating || 1) + "★) — answer first</span>");
    }
    // A review left unanswered for months is the thing a count alone hides.
    if (q.oldest_posted_days >= 30) {
      var months = Math.floor(q.oldest_posted_days / 30);
      flags.push('<span class="flag aged">oldest posted ' +
        (months >= 12 ? "over a year" : months + " months") + " ago</span>");
    }
    if (!flags.length) flags.push('<span class="flag good">nothing urgent</span>');

    box.className = "needs" + (q.negative ? " alert" : "");
    box.innerHTML =
      '<div><div class="needs-count">' + waiting + '</div>' +
      '<div class="needs-label">waiting for a reply</div></div>' +
      '<div class="needs-flags">' + flags.join("") + "</div>";
  }

  // ---------- reviews ----------
  function severity(r) { return !r.rating ? "" : r.rating <= 2 ? "sev-bad" : r.rating === 3 ? "sev-mid" : ""; }

  function reviewCard(r) {
    var text = r.review_text || "";
    var isLong = text.length > 260;
    var open = expanded[r.review_id];
    var body = text
      ? '<div class="r-body' + (isLong && !open ? " clamped" : "") + '">' + esc(text) + "</div>" +
        (isLong ? '<button class="r-more" data-expand="' + esc(r.review_id) + '">' +
          (open ? "Show less" : "Show more") + "</button>" : "")
      : '<div class="r-body empty">Rating only — the customer left no comment.</div>';

    return '<article class="review ' + severity(r) + (r.processed ? " done" : "") + '">' +
      '<div class="r-top"><div>' + stars(r.rating) +
        ' <span class="r-who">' + esc(r.reviewer_name || "Anonymous") + "</span> " +
        '<span class="r-biz">' + esc(r.business_name || "") + "</span></div>" +
        '<div class="r-when" title="' + esc(full(r.review_date)) + '">' + esc(ago(r.review_date)) +
        (r.review_date_is_approximate ? " (approx.)" : "") + "</div></div>" +
      body +
      '<div class="r-foot">' +
        (r.processed ? '<span class="chip done">handled</span>'
                     : '<span class="chip wait">waiting for reply</span>') +
        (r.owner_replied ? '<span class="chip done">answered on Google</span>' : "") +
        '<span class="chip">found ' + esc(ago(r.detected_at)) + "</span>" +
        (r.review_url ? '<a href="' + esc(r.review_url) + '" target="_blank" rel="noopener">Reply on Google &rarr;</a>' : "") +
      "</div></article>";
  }

  function renderReviews(list, total) {
    var box = $("reviews");
    if (!list.length) {
      box.innerHTML = '<div class="empty-state">' +
        (view === "new" ? "Nothing waiting. Every collected review has been handled."
                        : "No reviews collected yet.") + "</div>";
      $("load-more").hidden = true;
      return;
    }
    box.innerHTML = list.map(reviewCard).join("");
    $("load-more").hidden = list.length >= total;
    $("load-more").textContent = "Show more (" + (total - list.length) + " more)";
  }

  function loadReviews() {
    var base = view === "new" ? "/api/reviews/new?" : "/api/reviews?";
    var url = base + "limit=" + shown + "&sort=" + sortBy +
      (business ? "&business=" + business : "");
    return api(url).then(function (body) {
      var total = body.total !== undefined ? body.total : body.count;   // whole queue, not this page
      renderReviews(body.reviews, total);
      $("reviews-heading").textContent =
        (view === "new" ? "Waiting for reply" : "All collected") + " (" + total + ")";
    });
  }

  // ---------- system details ----------
  function renderBusinesses(list) {
    $("businesses").innerHTML = list.map(function (b) {
      var last = b.last_check || {};
      return '<div class="card"><h4>' + esc(b.name) +
        ' <span class="pill ' + esc(b.status) + '">' + esc(b.status.replace(/_/g, " ")) + "</span></h4>" +
        '<div class="kv"><span class="k">Last check</span><span class="v">' + esc(ago(last.started_at)) + "</span></div>" +
        '<div class="kv"><span class="k">Waiting for reply</span><span class="v">' + esc(b.unprocessed_reviews) + "</span></div>" +
        '<div class="kv"><span class="k">Total collected</span><span class="v">' + esc(b.total_reviews) + "</span></div>" +
        (b.status === "error" && b.last_error
          ? '<div class="err-box">' + esc(b.last_error.error_type) + ": " + esc(b.last_error.error_message) + "</div>"
          : "") +
        "</div>";
    }).join("");
  }

  function renderBackends(list) {
    $("backends").innerHTML = list.map(function (x) {
      return '<div class="card"><h4>' + esc(x.name) +
        ' <span class="pill ' + (x.available ? "available" : "unavailable") + '">' +
        (x.available ? "available" : "unavailable") + "</span>" +
        (x.selected ? ' <span class="pill inuse">in use</span>' : "") + "</h4>" +
        '<p class="reason">' + esc(x.reason) + "</p></div>";
    }).join("");
  }

  function renderChecks(rows) {
    $("checks").innerHTML = rows.map(function (c) {
      var detail = c.status === "failed"
        ? (c.error_type || "") + ": " + (c.error_message || "")
        : (c.duration_ms !== null ? Math.round(c.duration_ms / 1000) + "s" : "");
      return '<div class="check-row">' +
        "<span>" + esc(c.business_name) + "</span>" +
        '<span><span class="lbl">when</span>' + esc(ago(c.started_at)) + "</span>" +
        '<span><span class="lbl">trigger</span>' + esc(c.trigger) + "</span>" +
        '<span><span class="lbl">found</span>' + esc(c.reviews_found) + "</span>" +
        '<span><span class="lbl">new</span>' + esc(c.new_reviews) + "</span>" +
        '<span class="s-' + esc(c.status) + '"><span class="lbl">status</span>' +
          esc(c.status) + " " + esc(detail) + "</span>" +
        "</div>";
    }).join("") || '<div class="empty-state">No checks recorded yet.</div>';
  }

  // ---------- load ----------
  function load() {
    loadReviews();
    return Promise.all([api("/api/admin/status"), api("/api/admin/checks?limit=12")])
      .then(function (r) {
        var data = r[0];
        if (LOADED_VERSION && data.asset_version &&
            data.asset_version !== LOADED_VERSION && !reloading) {
          reloading = true;
          setBanner("The dashboard was updated. Reloading…", "");
          setTimeout(function () { location.reload(true); }, 800);
          return;
        }

        renderNeeds(data);
        renderBusinesses(data.businesses);
        renderBackends(data.backends);
        renderChecks(r[1].checks);

        var s = data.scheduler;
        $("system-line").textContent =
          (s.running ? "Checking both dealerships every " + s.interval_minutes + " min"
                     : "Scheduler STOPPED") +
          " · next " + ago(s.next_check).replace(" ago", " from now") +
          " · " + data.totals.total_reviews_collected + " reviews collected";

        // Only alarm about a failure that is STILL happening.
        var failing = (data.businesses || []).filter(function (b) { return b.status === "error"; });
        if (failing.length) {
          setBanner("Collection is currently failing:\n" + failing.map(function (b) {
            var e = b.last_error || {};
            return b.name + " — " + (e.error_type || "error") + ": " + (e.error_message || "");
          }).join("\n\n"), "err");
        } else {
          $("banner").hidden = true;
        }

        $("footer").textContent = "Backend: " + data.config.collector_backend +
          " · " + data.config.reviews_per_check + " reviews per check · updated " +
          new Date().toLocaleTimeString();
      })
      .catch(function (err) {
        if (err.auth) { showGate("The stored API key was rejected."); return; }
        setBanner("Dashboard could not load: " + err.message, "err");
      });
  }

  function setBanner(text, kind) {
    var b = $("banner");
    b.hidden = false;
    b.className = "banner" + (kind ? " " + kind : "");
    b.textContent = text;
  }

  // ---------- gate ----------
  function showGate(message) {
    if (timer) { clearInterval(timer); timer = null; }
    localStorage.removeItem(KEY_STORAGE);
    $("app").hidden = true;
    $("gate").style.display = "grid";
    $("gate-error").textContent = message || "";
  }
  function showApp() {
    $("gate").style.display = "none";
    $("app").hidden = false;
    load();
    if (timer) clearInterval(timer);
    timer = setInterval(load, REFRESH_MS);
  }

  // ---------- events ----------
  $("gate-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var value = $("gate-key").value.trim();
    if (!value) { $("gate-error").textContent = "Enter the API key."; return; }
    localStorage.setItem(KEY_STORAGE, value);
    api("/api/admin/status").then(showApp).catch(function (err) {
      showGate(err.auth ? "That API key was rejected." : err.message);
    });
  });

  function setView(next) {
    view = next; shown = PAGE;
    $("tab-new").classList.toggle("active", view === "new");
    $("tab-all").classList.toggle("active", view === "all");
    loadReviews();
  }
  $("tab-new").addEventListener("click", function () { setView("new"); });
  $("tab-all").addEventListener("click", function () { setView("all"); });
  $("filter-business").addEventListener("change", function () {
    business = this.value; shown = PAGE; loadReviews();
  });
  $("sort-by").addEventListener("change", function () { sortBy = this.value; loadReviews(); });
  $("load-more").addEventListener("click", function () { shown += PAGE; loadReviews(); });

  // Expanding a long review is delegated, so it survives every re-render.
  $("reviews").addEventListener("click", function (e) {
    var btn = e.target.closest("[data-expand]");
    if (!btn) return;
    var id = btn.getAttribute("data-expand");
    expanded[id] = !expanded[id];
    loadReviews();
  });

  $("check-now").addEventListener("click", function () {
    var btn = this;
    btn.disabled = true; btn.textContent = "Checking…";
    setBanner("Checking both dealerships now. This can take up to a minute.", "");
    api("/api/admin/check-now", { method: "POST" })
      .then(function (res) {
        if (res.skipped) { setBanner(res.reason, ""); return load(); }
        var lines = (res.results || []).map(function (r) {
          return r.status === "success"
            ? r.business_name + ": " + r.reviews_found + " found, " + r.new_reviews + " new"
            : r.business_name + ": FAILED — " + r.error_type + ": " + r.error_message;
        });
        var bad = (res.results || []).some(function (r) { return r.status === "failed"; });
        setBanner("Check finished.\n" + lines.join("\n"), bad ? "err" : "ok");
        return load();
      })
      .catch(function (err) { setBanner("Check failed: " + err.message, "err"); })
      .finally(function () { btn.disabled = false; btn.textContent = "Check Now"; });
  });

  $("refresh").addEventListener("click", function () {
    // Manual click only. The 30-second auto-refresh calls load() directly and
    // deliberately never notifies.
    api("/api/admin/notify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: "refresh" })
    }).then(function (res) {
      // Say what actually happened. Silence used to be ambiguous: you could not
      // tell whether an email went out or the queue was simply unchanged.
      if (res && res.sent && res.new_reviews) {
        setBanner("Emailed " + res.new_reviews + " newly detected review" +
          (res.new_reviews === 1 ? "" : "s") + ".", "ok");
      }
    }).catch(function () { /* a failed notification must not block the refresh */ });
    load();
  });

  $("logout").addEventListener("click", function () { showGate(""); });

  if (apiKey()) {
    api("/api/admin/status").then(showApp).catch(function () { showGate(""); });
  } else {
    showGate("");
  }
})();
