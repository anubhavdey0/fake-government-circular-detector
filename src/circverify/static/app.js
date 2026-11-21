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

  /* ------------------------------------------------------------ motion */
  var REDUCE = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Sections arrive as they scroll into view. IntersectionObserver, never a scroll listener.
  var revealed = document.querySelectorAll("[data-reveal]");
  if ("IntersectionObserver" in window && !REDUCE) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });
    revealed.forEach(function (el) { io.observe(el); });
  } else {
    revealed.forEach(function (el) { el.classList.add("is-in"); });
  }

  // Receipt pages: a WhatsApp share link with the receipt's own address.
  document.querySelectorAll("[data-share-wa]").forEach(function (a) {
    a.href = "https://wa.me/?text=" + encodeURIComponent(a.getAttribute("data-text") + " " + location.href);
  });


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
  var lastText = null;

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

  var previewUrl = null;
  function showChecking(file, label) {
    out.innerHTML = document.getElementById("skeleton-tpl").innerHTML;
    out.setAttribute("aria-busy", "true");
    var name = out.querySelector(".checking-name");
    if (name) name.textContent = label || "";
    var img = out.querySelector(".checking-preview");
    if (img && file && /^image\//.test(file.type || "") && window.URL) {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      img.onload = function () { img.classList.add("has-src"); };
      img.src = previewUrl;
    }
    window.scrollTo({ top: 0, behavior: REDUCE ? "auto" : "smooth" });
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
    lastText = null;
    tray.classList.add("is-busy");
    status.textContent = "Checking " + (file.name || "the pasted image") + " against " + INDEXED + "…";
    document.body.classList.add("has-result");
    showChecking(file, file.name || "Pasted image");
    var data = new FormData();
    data.append("file", file, file.name || "pasted-image.png");
    submit("/verify?fragment=1", data);
  }

  function submit(url, data) {
    fetch(url, { method: "POST", body: data })
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
        fail("The check did not finish. Check your connection, then try again.");
      })
      .finally(function () {
        tray.classList.remove("is-busy");
        out.removeAttribute("aria-busy");
        if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
      });
  }

  function checkMany(files) {
    err.textContent = "";
    lastFile = null;
    lastText = null;
    tray.classList.add("is-busy");
    status.textContent = "Checking " + files.length + " files against " + INDEXED + "…";
    document.body.classList.add("has-result");
    showChecking(files[0], files.length + " files");
    var data = new FormData();
    for (var i = 0; i < files.length; i++) data.append("files", files[i], files[i].name || "file");
    submit("/verify-bulk", data);
  }

  function pick(files) {
    if (!files || !files.length) return;
    if (files.length > 1) checkMany(files);
    else check(files[0]);
  }

  input.addEventListener("change", function () { pick(input.files); });

  var textForm = document.getElementById("text-form");
  if (textForm) {
    textForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var box = document.getElementById("text");
      if (!box.value || box.value.trim().length < 60) {
        fail("Paste at least two or three full sentences of the circular.");
        box.focus();
        return;
      }
      lastFile = null;
      lastText = box.value;
      err.textContent = "";
      tray.classList.add("is-busy");
      status.textContent = "Checking the text against " + INDEXED + "\u2026";
      document.body.classList.add("has-result");
      showChecking(null, "Pasted text");
      submit("/verify-text?fragment=1", new FormData(textForm));
    });
  }
  var urlForm = document.getElementById("url-form");
  if (urlForm) {
    urlForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var box = document.getElementById("url");
      if (!box.value || !/^\s*(https?:\/\/)?[^\s.]+\.[^\s]+/i.test(box.value)) {
        fail("Paste the full link, starting with https://");
        box.focus();
        return;
      }
      lastFile = null;
      lastText = null;
      err.textContent = "";
      tray.classList.add("is-busy");
      status.textContent = "Fetching the document and checking it against " + INDEXED + "\u2026";
      document.body.classList.add("has-result");
      showChecking(null, box.value.trim());
      submit("/verify-url?fragment=1", new FormData(urlForm));
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (input.files && input.files[0]) pick(input.files);
    else fail("Choose a PDF or image first, or paste the text below.");
  });

  ["dragenter", "dragover"].forEach(function (t) {
    form.addEventListener(t, function (e) { e.preventDefault(); tray.classList.add("is-over"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    form.addEventListener(t, function (e) { e.preventDefault(); tray.classList.remove("is-over"); });
  });
  form.addEventListener("drop", function (e) {
    pick(e.dataTransfer && e.dataTransfer.files);
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

  // "Report it as fake": send the same file (or text) again, with the reporter's notes.
  document.addEventListener("submit", function (e) {
    var rf = e.target.closest("form[data-action='report-fake']");
    if (!rf) return;
    e.preventDefault();
    var outEl = rf.querySelector("[data-report-out]");
    var btn = rf.querySelector("button[type='submit']");
    var fd = new FormData(rf);
    if (lastFile) fd.append("file", lastFile, lastFile.name || "pasted-image.png");
    else if (lastText) fd.append("text", lastText);
    else { outEl.textContent = "Check the document again first, then report it."; return; }
    btn.disabled = true;
    outEl.textContent = "Sending…";
    fetch("/report-fake", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        outEl.textContent = j.message || (j.ok ? "Thank you." : "Could not send the report.");
        if (j.ok) rf.querySelectorAll("input, textarea, button").forEach(function (el) { el.disabled = true; });
        else btn.disabled = false;
      })
      .catch(function () { outEl.textContent = "Could not send the report. Try again."; btn.disabled = false; });
  });

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-action='reset']")) { e.preventDefault(); reset(); return; }
    var rb = e.target.closest("[data-action='receipt']");
    if (rb) {
      e.preventDefault();
      var outEl = rb.parentElement.querySelector("[data-receipt-out]");
      var fd = new FormData();
      fd.append("token", rb.getAttribute("data-token"));
      fetch("/receipt", { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.url) { if (outEl) outEl.textContent = j.error || "Could not create a receipt."; return; }
          var link = location.origin + j.url;
          if (outEl) {
            outEl.innerHTML = "";
            var a = document.createElement("a");
            a.href = j.url; a.textContent = link; a.target = "_blank"; a.rel = "noopener";
            outEl.appendChild(a);
          }
          if (navigator.clipboard) navigator.clipboard.writeText(link).catch(function () {});
          rb.querySelector(".btn-label").textContent = "Receipt link copied";
          if (outEl) {
            var verdict = document.querySelector("#verdict-title");
            var wa = document.createElement("a");
            wa.href = "https://wa.me/?text=" + encodeURIComponent((verdict ? verdict.textContent + ": " : "") +
              "checked against the official UGC documents. " + link);
            wa.textContent = "Share on WhatsApp"; wa.target = "_blank"; wa.rel = "noopener noreferrer";
            wa.className = "share-wa";
            outEl.appendChild(document.createTextNode(" · "));
            outEl.appendChild(wa);
          }
        })
        .catch(function () { if (outEl) outEl.textContent = "Could not create a receipt. Try again."; });
      return;
    }
    var dl = e.target.closest("[data-action='report']");
    if (dl && !lastFile) { e.preventDefault(); window.print(); return; }
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
