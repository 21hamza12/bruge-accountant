# Vendored front-end assets

| File | Version | Source | SHA-384 (base64) |
|---|---|---|---|
| `htmx.min.js` | 2.0.4 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js | `HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+` |
| `fonts/geist-latin-400-normal.woff2` | @fontsource/geist 5.3.0 | https://cdn.jsdelivr.net/npm/@fontsource/geist@5.3.0/files/geist-latin-400-normal.woff2 | `uc3s6uG0khEMWge66YWpY/GdBOC8vDbY9Bnbg7etmcAKzqkkY+fU5cbTikq8Ya9L` |
| `fonts/geist-latin-500-normal.woff2` | @fontsource/geist 5.3.0 | https://cdn.jsdelivr.net/npm/@fontsource/geist@5.3.0/files/geist-latin-500-normal.woff2 | `k54QIv/oj/i2qZdUpQtOlH+Hv70UNlmc3QWAIZ/nZJMF8ixcOD40x3KIhR8BAGW7` |
| `fonts/geist-latin-600-normal.woff2` | @fontsource/geist 5.3.0 | https://cdn.jsdelivr.net/npm/@fontsource/geist@5.3.0/files/geist-latin-600-normal.woff2 | `0mWZZhIKltUkp64k21QxvRgryNxpracQr5bqMq713FKtarRPM4OrwUjY46z5ZQLO` |
| `fonts/geist-mono-latin-400-normal.woff2` | @fontsource/geist-mono 5.3.0 | https://cdn.jsdelivr.net/npm/@fontsource/geist-mono@5.3.0/files/geist-mono-latin-400-normal.woff2 | `+hSrm3seYioO4dd6rTJCXGeoFZs47FGgfpJZC7uTQ+5EO1ehodpbwHLXWpsY8BDy` |
| `fonts/geist-mono-latin-500-normal.woff2` | @fontsource/geist-mono 5.3.0 | https://cdn.jsdelivr.net/npm/@fontsource/geist-mono@5.3.0/files/geist-mono-latin-500-normal.woff2 | `7lA063272U7jZ/sh9Qr+6srnAxSX7a284uveSh7Qhip3AOgjlUPWCSuZiV1sSLkj` |

`tests/test_pwa_assets.py` recomputes the hash on every run. To upgrade: download the new file,
compute `openssl dgst -sha384 -binary htmx.min.js | openssl base64 -A`, update this table and the
test, and re-check that nothing needs `hx-on`, inline scripts or eval (the CSP forbids them and
`<meta name="htmx-config">` sets `allowEval: false`).

`.gitattributes` marks the file `-text` so a Windows checkout cannot change its bytes.

## Fonts (Geist and Geist Mono)

The five `fonts/*.woff2` files are the `latin` subset (French needs nothing more) of the Geist and
Geist Mono families, taken unmodified from the npm packages `@fontsource/geist` and
`@fontsource/geist-mono`, pinned at 5.3.0 and fetched through jsDelivr (URLs in the table above).
They replace a Google Fonts link, which the CSP (`font-src 'self'`, `style-src 'self'`) forbids and
which would leak every visitor's address to a third party.

* Licence: SIL Open Font License 1.1, copyright The Geist Project Authors
  (https://github.com/vercel/geist-font). The full text is in `static/fonts/OFL-geist.txt` and
  `static/fonts/OFL-geist-mono.txt` (copied from the packages' `LICENSE` files) and travels with
  the fonts, as the licence requires. The fonts are not renamed or modified.
* `static/app.css` declares them with `@font-face` (`font-display: swap`).
* `.gitattributes` marks `*.woff2` as `-text`; `tests/test_pwa_assets.py` recomputes every hash.
* To upgrade: fetch the new files, recompute with
  `openssl dgst -sha384 -binary <file> | openssl base64 -A`, then update this table and the test.
