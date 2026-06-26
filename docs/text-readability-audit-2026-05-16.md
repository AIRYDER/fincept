# Text Readability UI Audit

## Verdict
The UI is visually coherent, but text readability is being taxed by three things: very small chrome text, heavy uppercase/tracking in dense strips, and repeated truncation in content-heavy panels. The app still feels like Fincept, but several surfaces read more like operator telemetry than comfortably scannable UI.

## What Reads Well
- The mono font choice is consistent and fits the product.
- The cool-dark palette keeps text contrast workable.
- The login surface and hero copy are comparatively readable.
- The overview page remains understandable at desktop width even with dense chrome.

## Findings

### 1. Shell chrome is too small and too tightly packed on narrow widths
**Severity:** Medium  
**Locations:** `apps/dashboard/src/components/shell/title-bar.tsx:282-319`, `apps/dashboard/src/components/shell/safety-state-bar.tsx:132-193`, `apps/dashboard/src/components/shell/status-bar.tsx:56-100`, `apps/dashboard/src/components/shell/nav-tabs.tsx:60-90`

**Issue:** The authenticated shell relies on 10px labels, uppercase text, and wide tracking across several stacked bars. That is readable on desktop, but on 390px widths the chrome starts to compete with the actual page content. The bars are information-rich, but the small text and tight packing reduce quick scanning.

**Recommendation:** Keep the terminal feel, but raise the most important chrome text slightly, loosen tracking, and hide less important chips earlier on narrow screens.

**Pros:** Better mobile legibility, faster scanning, less visual fatigue.  
**Cons:** Slightly less authentic terminal density, a bit more vertical space or earlier collapse needed.

### 2. Long-form content panels are still a little too label-heavy
**Severity:** Medium  
**Locations:** `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx:98-356`, `apps/dashboard/src/features/portfolio-builder/PortfolioBuilderPage.tsx:137-151`, `apps/dashboard/src/components/ui/card.tsx:27-57`, `apps/dashboard/src/components/widgets/page-header.tsx:16-20`

**Issue:** The portfolio report and builder copy mix 10px section labels with 11px or 12px narrative text, then repeat uppercase headings throughout. The structure is clear, but the reading experience is dense enough that longer passages feel more like a dashboard log than an investment memo.

**Recommendation:** Preserve small chrome, but give narrative sections and explanatory copy a more forgiving size and line height. Reduce uppercase usage in body-adjacent labels where the text is meant to be read, not decoded.

**Pros:** Less eye strain, clearer hierarchy, easier to read multi-sentence explanations.  
**Cons:** Panels become taller, and the UI loses some of the “compact workstation” feel.

### 3. Important values are still hidden behind truncation in a few key places
**Severity:** Low to Medium  
**Locations:** `apps/dashboard/src/features/portfolio-builder/PortfolioReportView.tsx:320-345`, `apps/dashboard/src/app/system/page.tsx:284-490`, `apps/dashboard/src/app/research/page.tsx:423-445`

**Issue:** Some of the most important text is placed in `truncate` or fixed-width table cells. That avoids overflow, but it also means the user may not actually be able to read the full value without hovering or hunting for the tooltip. This is especially noticeable for technical values, command strings, and candidate themes.

**Recommendation:** Keep truncation only for genuinely secondary metadata. For primary text, allow wrapping or provide an explicit expand / copy affordance so the value can be read without guesswork.

**Pros:** Key content becomes visible instead of implied, and the interface feels more trustworthy.  
**Cons:** More wrap-induced height, and some tables will need responsive collapse or disclosure patterns.

## Best Next Changes
1. Increase the readable text size on content surfaces first, not the shell chrome.
2. Let only secondary metadata truncate.
3. Keep the top bars compact, but make them collapse earlier on mobile.
4. Use uppercase sparingly in content areas and keep it mostly for chrome and badges.

## Bottom Line
The app is structurally strong; the readability issue is mostly an emphasis problem. We do not need a redesign. We need a small hierarchy adjustment so the text that matters can be read quickly without the shell asking for too much attention.
