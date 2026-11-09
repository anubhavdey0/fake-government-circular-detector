/* circverify front end. Progressive enhancement over plain HTML forms:
   everything works without this file, it just gets quicker and nicer with it. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  /* ------------------------------------------------------------ viewers */
  function initViewers(root) {
    root.querySelectorAll(".viewer:not([data-ready])").forEach(function (viewer) {
      viewer.setAttribute("data-ready", "");
      var stage = viewer.querySelector(".viewer-stage");
      var range = viewer.querySelector(".v-range input");
      function setPos(pct) {
        pct = Math.max(0, Math.min(100, pct));
        viewer.style.setProperty("--pos", pct + "%");
        if (range && Number(range.value) !== Math.round(pct)) range.value = Math.round(pct);
      }
      viewer.querySelectorAll(".seg input").forEach(function (radio) {
        radio.addEventListener("change", function () { viewer.setAttribute("data-mode", radio.value); });
      });
      if (range) range.addEventListener("input", function () { setPos(Number(range.value)); });
      if (stage) {
        var dragging = false;
        function fromEvent(e) {
          var r = stage.getBoundingClientRect();
          setPos(((e.clientX - r.left) / r.width) * 100);
        }
        stage.addEventListener("pointerdown", function (e) {
          if (viewer.getAttribute("data-mode") !== "swipe") return;
          dragging = true;
          stage.setPointerCapture(e.pointerId);
          fromEvent(e);
        });
        stage.addEventListener("pointermove", function (e) { if (dragging) fromEvent(e); });
        ["pointerup", "pointercancel"].forEach(function (t) {
          stage.addEventListener(t, function () { dragging = false; });
        });
      }
    });
  }

  /* ------------------------------------------------------------ copy */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    var done = function () {
      var live = btn.parentElement.querySelector("[data-copy-status]");
      btn.classList.add("copied");
      if (live) live.textContent = "Copied";
      setTimeout(function () { btn.classList.remove("copied"); if (live) live.textContent = ""; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
    }
  });

  initViewers(document);

  /* ------------------------------------------------------------ upload flow */
  var form = document.getElementById("check-form");
  if (!form) return;
  var input = document.getElementById("file");
  var tray = form;
  var status = document.getElementById("tray-status");
  var err = document.getElementById("file-error");
  var out = document.getElementById("result");
  var MAX = Number(form.getAttribute("data-max-bytes") || 0);
  var INDEXED = form.getAttribute("data-indexed") || "the index";
  var OK_TYPES = /^(application\/pdf|image\/(png|jpe?g|webp|gif|tiff?))$/;
  var lastFile = null;

  function fail(msg) {
    err.textContent = msg;
    status.textContent = "";
    tray.classList.remove("is-busy");
  }

  function reset() {
    document.body.classList.remove("has-result");
    out.innerHTML = "";
    input.value = "";
    err.textContent = "";
    status.textContent = "";
    window.scrollTo({ top: 0 });
    var zone = form.querySelector(".tray-zone");
    if (zone) input.focus();
  }

  function check(file) {
    err.textContent = "";
    if (!file) return;
    if (file.type && !OK_TYPES.test(file.type)) {
      fail("That is a " + (file.type || "file") + ". Choose a PDF or an image (PNG, JPEG, WebP).");
      return;
    }
    if (MAX && file.size > MAX) {
      fail("That file is " + (file.size / 1048576).toFixed(1) + " MB. The limit is " + Math.round(MAX / 1048576) + " MB.");
      return;
    }
    lastFile = file;
    tray.classList.add("is-busy");
    status.textContent = "Checking " + (file.name || "the pasted image") + " against " + INDEXED + "…";
    document.body.classList.add("has-result");
    out.innerHTML = document.getElementById("skeleton-tpl").innerHTML;
    out.setAttribute("aria-busy", "true");
    var data = new FormData();
    data.append("file", file, file.name || "pasted-image.png");
    fetch("verify?fragment=1", { method: "POST", body: data })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        out.innerHTML = html;
        initViewers(out);
        status.textContent = "";
        var h = out.querySelector("#verdict-title");
        if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); }
        window.scrollTo({ top: 0 });
      })
      .catch(function () {
        document.body.classList.remove("has-result");
        out.innerHTML = "";
        fail("The check did not finish. Make sure the circverify server is still running, then try again.");
      })
      .finally(function () { tray.classList.remove("is-busy"); out.removeAttribute("aria-busy"); });
  }

  input.addEventListener("change", function () { check(input.files && input.files[0]); });
  form.addEventListener("submit", function (e) { e.preventDefault(); check(input.files && input.files[0]); });

  ["dragenter", "dragover"].forEach(function (t) {
    form.addEventListener(t, function (e) { e.preventDefault(); tray.classList.add("is-over"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    form.addEventListener(t, function (e) { e.preventDefault(); tray.classList.remove("is-over"); });
  });
  form.addEventListener("drop", function (e) {
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) check(f);
  });

  // Paste a screenshot straight from the clipboard (Ctrl+V / Cmd+V).
  document.addEventListener("paste", function (e) {
    if (document.body.classList.contains("has-result")) return;
    var items = (e.clipboardData && e.clipboardData.items) || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === "file") {
        var f = items[i].getAsFile();
        if (f) { e.preventDefault(); check(f); return; }
      }
    }
  });

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-action='reset']")) { e.preventDefault(); reset(); return; }
    var dl = e.target.closest("[data-action='report']");
    if (dl && lastFile) {
      e.preventDefault();
      var label = dl.querySelector(".btn-label");
      var before = label ? label.textContent : "";
      if (label) label.textContent = "Preparing report…";
      var data = new FormData();
      data.append("file", lastFile, lastFile.name || "pasted-image.png");
      fetch("report", { method: "POST", body: data })
        .then(function (r) { if (!r.ok) throw new Error(); return r.blob(); })
        .then(function (blob) {
          var a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = "circverify-report-" + (lastFile.name || "image").replace(/\.[^.]+$/, "") + ".html";
          document.body.appendChild(a);
          a.click();
          setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        })
        .catch(function () { if (label) label.textContent = "Report failed. Try again"; })
        .finally(function () { if (label && label.textContent === "Preparing report…") label.textContent = before; });
    }
  });
})();
