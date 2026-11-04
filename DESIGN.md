# Design

Swiss annual-report grid for an operating tool: the verdict and reason come first, at report-page scale.

- **Colour:** the Atlas palette. Section headers: navy `#14315C` for invoices, teal `#0B6B6B` for Ask, graphite `#2F3B46` for the log. Cyan `#0E7490` (dark theme `#38BDD8`) for actions, links and selection only. Verdict colours own whole regions: reject `#C9251B`, review `#F2B705`, approve `#0B7A54`. Neutrals are cool, with light and dark sets. Tokens are in `frontend/src/styles.css`.
- **Type:** Hanken Grotesk only, tabular figures. Display weight 800 with tight tracking; UI text at 16px and 14px.
- **Structure:** heavy 4px rules open each section, 1px rules separate rows. No cards, and no shadows used as structure.
- **Components:** square buttons, verdict tags, the proportional verdict strip (also the filter), click-to-correct figures showing original and new values, check and cross marks on line items, fixed decision bar, fold-out sections.
- **Motion:** state only, 120 to 420ms ease-out. One authored moment: the circular theme wipe. Reduced motion is respected.
- **Copy:** recommendation and human decision are always separate. Errors name the problem and the recovery.
