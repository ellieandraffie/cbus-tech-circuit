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

## To finish wiring
- **Analytics:** paste the Cloudflare Web Analytics beacon `<script>` into `index.html` (marked spot).
- **Feedback form:** set `FB_ENDPOINT` in the inline script to your Formspree form URL — hides the destination email.

## Weekly refresh
A scheduled agent re-scrapes the source calendars each Monday, regenerates the event
data + `sitemap.xml`, and commits — the page updates in place. A private companion
digest flags Lauren's personal picks into her daily brief (not shown on this public page).
