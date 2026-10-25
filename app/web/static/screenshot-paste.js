// A screenshot pasted (Ctrl+V) or dropped anywhere on a page that has a photo box (#shot-zone with
// the file input #shot_files) is put into that input, as if it had been chosen: the person then
// presses "Lire la photo". Text pastes are left alone (the Amadeus text box keeps working). The
// box is looked up at each event, because htmx replaces the chat panel after every message. The
// status messages come from data attributes, so they are translated with the page.
(function () {
  "use strict";
  var MAX_IMAGES = 4;

  function isImage(file) {
    return Boolean(file && typeof file.type === "string" && file.type.indexOf("image/") === 0);
  }

  function setup(doc, DataTransferCtor) {
    if (!doc || !DataTransferCtor) return false;

    function box() {
      var input = doc.getElementById("shot_files");
      if (!input) return null;
      return { input: input, zone: doc.getElementById("shot-zone"), status: doc.getElementById("shot-pasted") };
    }

    function say(status, count, dropped) {
      if (!status) return;
      var data = status.dataset || {};
      if (dropped) status.textContent = data.full || "";
      else if (count === 1) status.textContent = data.one || "";
      else status.textContent = (data.many || "").replace("{n}", String(count));
    }

    function add(files) {
      var b = box();
      var images = [];
      for (var i = 0; i < files.length; i++) if (isImage(files[i])) images.push(files[i]);
      if (!b || images.length === 0) return false;
      var current = [];
      var existing = b.input.files || [];
      for (var j = 0; j < existing.length; j++) current.push(existing[j]);
      var all = current.concat(images);
      var kept = all.slice(0, MAX_IMAGES);
      var transfer = new DataTransferCtor();
      for (var k = 0; k < kept.length; k++) transfer.items.add(kept[k]);
      b.input.files = transfer.files;
      say(b.status, kept.length, all.length > MAX_IMAGES);
      return true;
    }

    doc.addEventListener("paste", function (event) {
      var data = event.clipboardData;
      if (!data || !data.items) return;
      var files = [];
      for (var i = 0; i < data.items.length; i++) {
        var item = data.items[i];
        if (item.kind === "file") {
          var file = item.getAsFile();
          if (file) files.push(file);
        }
      }
      if (add(files)) event.preventDefault();
    });

    doc.addEventListener("dragover", function (event) {
      var b = box();
      if (!b) return;
      event.preventDefault();
      if (b.zone) b.zone.classList.add("is-dragover");
    });

    doc.addEventListener("dragleave", function () {
      var b = box();
      if (b && b.zone) b.zone.classList.remove("is-dragover");
    });

    doc.addEventListener("drop", function (event) {
      var b = box();
      if (!b) return;
      event.preventDefault();
      if (b.zone) b.zone.classList.remove("is-dragover");
      add((event.dataTransfer && event.dataTransfer.files) || []);
    });
    return true;
  }

  if (typeof document !== "undefined") {
    setup(document, typeof DataTransfer !== "undefined" ? DataTransfer : null);
  }
})();
