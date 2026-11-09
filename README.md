# CBus Tech Circuit

A curated, weekly-updated public calendar of tech, startup & professional networking
events across Columbus, Dublin & Central Ohio. Presented by Pink Hippo Strategy.

## Deploy (GitHub Pages)
1. Create a **public** repo named `cbus-tech-circuit`.
2. Push these files to the `main` branch.
3. Settings → Pages → Source: `Deploy from a branch`, Branch: `main` / `/ (root)`.
4. Live at `https://<username>.github.io/cbus-tech-circuit/`.
5. Replace `YOUR-USERNAME.github.io/cbus-tech-circuit` in `index.html`, `robots.txt`,
   and `sitemap.xml` with the real Pages URL (or a custom domain).

## Wiring
- **Analytics:** Google Analytics 4, its own property "CBus Tech Calendar" (`G-0NMC1PYXFP`),
  set in the `<head>` of `index.html`. Before 2026-11-07 it shared the Pink Hippo Strategy
  property (`G-3Y16FP5YM5`).
- **Feedback form:** Web3Forms. The access key is in the inline script in `index.html`
  (public by design); the recipient email is set on the Web3Forms account, not in code.

## Weekly refresh
A scheduled agent re-scrapes the source calendars each Monday, regenerates the event
data + `sitemap.xml`, and commits — the page updates in place. A private companion
digest flags Lauren's personal picks into her daily brief (not shown on this public page).
