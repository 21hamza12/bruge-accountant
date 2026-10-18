# Vendored front-end assets

| File | Version | Source | SHA-384 (base64) |
|---|---|---|---|
| `htmx.min.js` | 2.0.4 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js | `HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+` |

`tests/test_pwa_assets.py` recomputes the hash on every run. To upgrade: download the new file,
compute `openssl dgst -sha384 -binary htmx.min.js | openssl base64 -A`, update this table and the
test, and re-check that nothing needs `hx-on`, inline scripts or eval (the CSP forbids them and
`<meta name="htmx-config">` sets `allowEval: false`).

`.gitattributes` marks the file `-text` so a Windows checkout cannot change its bytes.
