// Drives app/web/sw.js with fake service-worker globals and prints what it did as JSON.
const fs = require("fs");
const vm = require("vm");

const ORIGIN = "https://app.test";
const listeners = {};
const stores = new Map(); // cache name -> Map(url -> Response)
let online = true;

const absolute = (req) => (typeof req === "string" ? new URL(req, ORIGIN).href : req.url);

const caches = {
  async open(name) {
    if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return {
      async addAll(urls) {
        for (const u of urls) store.set(absolute(u), await fakeFetch(u));
      },
      async put(req, res) {
        store.set(absolute(req), res);
      },
    };
  },
  async match(req) {
    const url = absolute(req);
    for (const store of stores.values()) if (store.has(url)) return store.get(url).clone();
    return undefined;
  },
  async keys() {
    return [...stores.keys()];
  },
  async delete(name) {
    return stores.delete(name);
  },
};

async function fakeFetch(req) {
  const url = absolute(req);
  if (!online) throw new TypeError("network down");
  if (url.endsWith("/static/missing.js")) return new Response("missing " + url, { status: 404 });
  if (url.endsWith("/static/broken.js")) return new Response("broken " + url, { status: 500 });
  return new Response("body of " + url, { status: 200 });
}

const selfObj = {
  location: { origin: ORIGIN },
  clients: { claim: async () => {} },
  skipWaiting: async () => {},
  addEventListener: (name, fn) => {
    listeners[name] = fn;
  },
};

const code = fs.readFileSync(process.argv[2], "utf8");
vm.runInNewContext(code, { self: selfObj, caches, fetch: fakeFetch, URL, Promise, Response });

async function lifecycle(name) {
  let pending = Promise.resolve();
  listeners[name]({ waitUntil: (p) => (pending = p) });
  await pending;
}

async function dispatchFetch(req) {
  let responded = null;
  const waited = [];
  listeners.fetch({
    request: req,
    respondWith: (p) => (responded = Promise.resolve(p)),
    waitUntil: (p) => waited.push(Promise.resolve(p)),
  });
  if (responded === null) return { intercepted: false };
  const res = await responded;
  const result = { intercepted: true, body: res ? await res.text() : null };
  if (res && res.status !== 200) result.status = res.status;
  await Promise.all(waited);
  result.waited = waited.length;
  return result;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

const cachedUrls = async () => {
  await settle(); // the worker stores copies in the background
  return [...stores.values()]
    .flatMap((store) => [...store.keys()])
    .map((u) => {
      const parsed = new URL(u);
      return parsed.pathname + parsed.search;
    })
    .sort();
};

(async () => {
  const out = {};
  stores.set("old-cache-v0", new Map());
  await lifecycle("install");
  out.afterInstall = await cachedUrls();
  await lifecycle("activate");
  out.cachesAfterActivate = await caches.keys();

  const nav = (path) => ({ method: "GET", url: ORIGIN + path, mode: "navigate" });
  const sub = (path, method = "GET", origin = ORIGIN) => ({
    method,
    url: origin + path,
    mode: "cors",
  });

  out.navOnline = await dispatchFetch(nav("/questions?date=2026-10-01"));
  out.afterNavOnline = await cachedUrls();
  out.staticOnline = await dispatchFetch(sub("/static/new.js"));
  out.afterStaticOnline = await cachedUrls();
  out.staticNotOk = await dispatchFetch(sub("/static/missing.js"));
  out.afterStaticNotOk = await cachedUrls();
  out.staticServerError = await dispatchFetch(sub("/static/broken.js"));
  out.afterStaticServerError = await cachedUrls();
  out.post = await dispatchFetch(sub("/upload", "POST"));
  out.crossOrigin = await dispatchFetch(sub("/static/x.js", "GET", "https://cdn.other"));
  out.xhr = await dispatchFetch(sub("/api/data"));

  online = false;
  out.navOffline = await dispatchFetch(nav("/questions"));
  out.staticOffline = await dispatchFetch(sub("/static/new.js"));
  out.afterOffline = await cachedUrls();
  console.log(JSON.stringify(out));
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
