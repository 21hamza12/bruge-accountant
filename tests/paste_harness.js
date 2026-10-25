// Drives app/web/static/screenshot-paste.js with a fake page and prints what it did as JSON.
const fs = require("fs");
const vm = require("vm");

class FakeFile {
  constructor(name, type) {
    this.name = name;
    this.type = type;
  }
}

class FakeDataTransfer {
  constructor() {
    const files = [];
    this.files = files;
    this.items = { add: (f) => files.push(f) };
  }
}

function element(id) {
  const listeners = {};
  return {
    id,
    files: [],
    textContent: "",
    dataset: {},
    classes: new Set(),
    classList: {
      add(c) { this._el.classes.add(c); },
      remove(c) { this._el.classes.delete(c); },
    },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    fire(type, event) { (listeners[type] || []).forEach((fn) => fn(event)); return event; },
  };
}

function page({ withInput = true } = {}) {
  const docListeners = {};
  const els = {};
  if (withInput) {
    els.shot_files = element("shot_files");
    els["shot-zone"] = element("shot-zone");
    els["shot-pasted"] = element("shot-pasted");
    els["shot-pasted"].dataset = {
      one: "1 image prête.",
      many: "{n} images prêtes.",
      full: "4 images au plus : les suivantes sont ignorées.",
    };
    for (const e of Object.values(els)) e.classList._el = e;
  }
  const document = {
    getElementById: (id) => els[id] || null,
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    fire(type, event) { (docListeners[type] || []).forEach((fn) => fn(event)); return event; },
  };
  return { document, els };
}

function event(extra) {
  return { prevented: false, preventDefault() { this.prevented = true; }, ...extra };
}

function clipboard(...items) {
  return {
    items: items.map((it) => ({
      kind: it.kind,
      type: it.type,
      getAsFile: () => (it.kind === "file" ? new FakeFile(it.name || "image.png", it.type) : null),
    })),
  };
}

function run(src, opts) {
  const p = page(opts);
  const sandbox = { document: p.document, DataTransfer: FakeDataTransfer, globalThis: {} };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(src, sandbox);
  return p;
}

const src = fs.readFileSync(process.argv[2], "utf8");
const out = {};
const names = (p) => p.els.shot_files.files.map((f) => f.name);

// One screenshot pasted with Ctrl+V.
let p = run(src);
let e = p.document.fire("paste", event({ clipboardData: clipboard({ kind: "file", type: "image/png" }) }));
out.pasteOne = { prevented: e.prevented, files: names(p), status: p.els["shot-pasted"].textContent };

// A second paste adds to the first.
e = p.document.fire("paste", event({ clipboardData: clipboard({ kind: "file", type: "image/jpeg", name: "b.jpg" }) }));
out.pasteSecond = { files: names(p).length, status: p.els["shot-pasted"].textContent };

// Text only: the page's own paste (into the Amadeus text box) is left alone.
p = run(src);
e = p.document.fire("paste", event({ clipboardData: clipboard({ kind: "string", type: "text/plain" }) }));
out.pasteText = { prevented: e.prevented, files: names(p).length, status: p.els["shot-pasted"].textContent };

// A pasted file that is not an image is ignored.
e = p.document.fire("paste", event({ clipboardData: clipboard({ kind: "file", type: "application/pdf", name: "x.pdf" }) }));
out.pastePdf = { prevented: e.prevented, files: names(p).length };

// Five images: four are kept.
p = run(src);
const five = Array.from({ length: 5 }, (_, i) => ({ kind: "file", type: "image/png", name: `p${i}.png` }));
p.document.fire("paste", event({ clipboardData: clipboard(...five) }));
out.pasteFive = { files: names(p), status: p.els["shot-pasted"].textContent };

// Dropped on the page (anywhere: the photo box is highlighted while dragging).
p = run(src);
const zone = p.els["shot-zone"];
const over = p.document.fire("dragover", event({}));
out.dragoverPrevented = over.prevented;
out.dragoverClass = zone.classes.has("is-dragover");
const drop = p.document.fire("drop", event({ dataTransfer: { files: [new FakeFile("d1.png", "image/png"), new FakeFile("n.txt", "text/plain")] } }));
out.drop = { prevented: drop.prevented, files: names(p), cls: zone.classes.has("is-dragover") };

// The photo box replaced after setup (htmx swaps the chat panel): the new box is used.
p = run(src);
p.els.shot_files = Object.assign(p.els.shot_files, { files: [] });
p.document.fire("paste", event({ clipboardData: clipboard({ kind: "file", type: "image/png" }) }));
out.afterSwap = names(p).length;

// A page without the photo box: a paste or a drop does nothing.
p = run(src, { withInput: false });
e = p.document.fire("paste", event({ clipboardData: clipboard({ kind: "file", type: "image/png" }) }));
const dropNoBox = p.document.fire("drop", event({ dataTransfer: { files: [] } }));
out.noBox = { prevented: e.prevented, dropPrevented: dropNoBox.prevented };

process.stdout.write(JSON.stringify(out));
