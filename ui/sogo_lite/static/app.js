// Small behaviours only. Every screen works as plain forms without this file.
(function () {
  "use strict";

  // Confirm before destructive submits.
  document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm") ||
      (event.submitter && event.submitter.getAttribute("data-confirm"));
    if (message && !window.confirm(message)) event.preventDefault();
  });

  // Selects that reload the page when changed (question pickers, filters).
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });

  // Gateway status pill in the header.
  var pill = document.getElementById("gateway-status");
  function refreshHealth() {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      var label = "gateway " + (h.reachable ? h.status + " · " + h.backend : "unreachable");
      var bad = !h.reachable || h.status !== "ok";
      if (h.token === "missing") { label += " · no token"; bad = true; }
      if (h.token === "rejected") { label = "gateway token rejected"; bad = true; }
      pill.textContent = label;
      pill.className = "pill " + (bad ? "warn" : "ok");
    }).catch(function () { pill.textContent = "gateway ?"; pill.className = "pill warn"; });
  }
  if (pill) { refreshHealth(); setInterval(refreshHealth, 30000); }

  // Inspector side panel, remembered across pages.
  var panel = document.getElementById("panel");
  var toggle = document.getElementById("panel-toggle");
  function showPanel(open) {
    if (!panel) return;
    var frame = panel.querySelector("iframe");
    panel.hidden = !open;
    if (open) frame.src = frame.getAttribute("data-src");
    try { localStorage.setItem("sogo-lite-panel", open ? "1" : "0"); } catch (e) { /* private mode */ }
  }
  if (toggle) {
    toggle.addEventListener("click", function () { showPanel(panel.hidden); });
    try { if (localStorage.getItem("sogo-lite-panel") === "1") showPanel(true); } catch (e) { /* private mode */ }
  }

  // Hints arrive just after a question is saved, so the editor checks a few times.
  var hints = document.getElementById("hints");
  if (hints) {
    [1200, 3000, 6000].forEach(function (delay) {
      setTimeout(function () {
        fetch(hints.getAttribute("data-poll")).then(function (r) { return r.text(); }).then(function (html) {
          var body = new DOMParser().parseFromString(html, "text/html").querySelectorAll(".hint");
          if (body.length !== hints.querySelectorAll(".hint").length) {
            hints.replaceChildren.apply(hints, Array.prototype.slice.call(body));
          }
        }).catch(function () { /* the editor works without hints */ });
      }, delay);
    });
  }

  // Scenario run progress.
  var progress = document.getElementById("run-progress");
  if (progress && progress.getAttribute("data-running") === "1") {
    var timer = setInterval(function () {
      fetch("/api/scenarios/status").then(function (r) { return r.json(); }).then(function (s) {
        progress.querySelector("progress").max = s.total || 1;
        progress.querySelector("progress").value = s.done;
        document.getElementById("run-count").textContent = s.done + " of " + s.total;
        document.getElementById("run-current").textContent = s.current;
        if (!s.running) { clearInterval(timer); window.location.reload(); }
      });
    }, 1500);
  }

  // Jobs polling test.
  var job = document.getElementById("job");
  if (job) {
    var jobTimer = setInterval(pollJob, 1000);
    pollJob();
  }
  function pollJob() {
    fetch("/api/jobs/" + job.getAttribute("data-job")).then(function (r) { return r.json(); }).then(function (s) {
      var status = document.getElementById("job-status");
      var facts = document.getElementById("job-facts");
      if (s.fallback) {
        status.textContent = "no answer: " + s.reason;
        status.className = "pill warn";
        clearInterval(jobTimer);
        return;
      }
      status.textContent = s.status;
      status.className = "pill " + (s.status === "done" ? "ok" : s.status === "failed" ? "warn" : "");
      document.getElementById("job-bar").value = s.total ? Math.round(100 * s.processed / s.total) : 0;
      var rows = [["Processed", s.processed + " of " + s.total], ["Failed items", s.failed_items],
                  ["Cache hits", s.cache_hits], ["Items per second", s.items_per_s === null ? "–" : s.items_per_s],
                  ["Output (on the gateway host)", s.output_path], ["Error", s.error || "–"]];
      facts.replaceChildren();
      rows.forEach(function (row) {
        var dt = document.createElement("dt"); dt.textContent = row[0];
        var dd = document.createElement("dd"); dd.textContent = row[1];
        facts.append(dt, dd);
      });
      if (s.status === "done" || s.status === "failed") clearInterval(jobTimer);
    });
  }
})();
