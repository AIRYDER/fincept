# Fincept Terminal Design System

## 1. Atmosphere & Identity

Fincept Terminal feels like a quiet OLED research instrument: dense, exact, and tactile, with the restraint of Teenage Engineering hardware and the dotted, modular clarity of Nothing OS. The signature is a dark instrument panel where glass panes, soft inset pressure, LED state dots, and cobalt/orange measurement marks make the interface feel physical without becoming decorative.

## 2. Color

### Palette

| Role | Token | Light | Dark | Usage |
|------|-------|-------|------|-------|
| Surface/primary | --surface-primary | #F7F8FA | #030406 | OLED app background |
| Surface/secondary | --surface-secondary | #ECEFF3 | #090C12 | App shell and panel base |
| Surface/elevated | --surface-elevated | #FFFFFF | #101620 | Raised glass cards, popovers |
| Surface/inset | --surface-inset | #E3E6EA | #05070B | Neumorphic depressed controls |
| Text/primary | --text-primary | #101318 | #F3F7FF | Primary labels and values |
| Text/secondary | --text-secondary | #59606B | #96A0AF | Supporting copy |
| Text/tertiary | --text-tertiary | #7C8490 | #586171 | Disabled text and low-priority metadata |
| Border/default | --border-default | #CBD2DC | #1C2633 | Panel edges |
| Border/subtle | --border-subtle | #E3E8EF | #101722 | Hairlines and quiet separators |
| Accent/primary | --accent-primary | #2F6BFF | #2F6BFF | Tertiary cobalt focus, links, selected nav |
| Accent/complement | --accent-complement | #FF7A1A | #FF7A1A | Orange balance marks, warning-adjacent emphasis |
| Status/success | --status-success | #16A34A | #2FEF7D | Positive states |
| Status/warning | --status-warning | #D97706 | #FFB020 | Cautions |
| Status/error | --status-error | #DC2626 | #FF4E57 | Kill switch and destructive states |
| Status/info | --status-info | #0891B2 | #4FD8FF | Live telemetry |

### Rules

- Cobalt is the tertiary accent and should appear as focus, active navigation, and measuring light, not as a full-page wash.
- Orange is a complementary balancing color for paper-mode, warnings, and occasional instrument marks.
- Green, red, and cyan remain semantic trading/status colors; do not use them as brand decoration.
- OLED black is allowed only as the page floor. Interactive surfaces must use declared elevated or inset tokens so layers remain visible.

## 3. Typography

### Scale

| Level | Size | Weight | Line Height | Tracking | Usage |
|-------|------|--------|-------------|----------|-------|
| Display | 48px / 3rem | 700 | 1.05 | -0.03em | Login hero, major page identity |
| H1 | 32px / 2rem | 700 | 1.15 | -0.02em | Page headers |
| H2 | 24px / 1.5rem | 600 | 1.25 | -0.01em | Section headers |
| H3 | 18px / 1.125rem | 600 | 1.35 | 0 | Card titles |
| Body/lg | 16px / 1rem | 400 | 1.6 | 0 | Lead paragraphs |
| Body | 14px / 0.875rem | 400 | 1.55 | 0 | Default dashboard text |
| Body/sm | 12px / 0.75rem | 400 | 1.45 | 0.01em | Secondary info |
| Caption | 11px / 0.6875rem | 500 | 1.35 | 0.06em | Labels, metadata |
| Overline | 10px / 0.625rem | 700 | 1.2 | 0.12em | Instrument labels, uppercase tabs |

### Font Stack

- Primary: JetBrains Mono via `--font-mono`, then ui-monospace, Cascadia Mono, Consolas, monospace.
- Mono: same as primary. This product intentionally uses one mono family.
- Serif: none.

### Rules

- Use tabular figures everywhere data can shift.
- Labels may be uppercase; explanatory text should stay sentence case.
- Keep pages dense, but avoid text below 10px except secondary mnemonics.

## 4. Spacing & Layout

### Base Unit

All spacing derives from a base of **4px**.

| Token | Value | Usage |
|-------|-------|-------|
| --space-1 | 4px | Icon-to-label, tight strips |
| --space-2 | 8px | Compact controls |
| --space-3 | 12px | Default panel padding |
| --space-4 | 16px | Cards, horizontal page padding |
| --space-5 | 20px | Comfortable panel groups |
| --space-6 | 24px | Major card padding |
| --space-8 | 32px | Page section breaks |
| --space-10 | 40px | Hero and report spacing |
| --space-12 | 48px | Large visual separation |

### Grid

- Max content width: dashboards may fill the viewport; focused reports should use 1440px max.
- Column system: CSS grid with 8px gutters for dense cockpit pages and 16px gutters for report pages.
- Breakpoints: sm 640px, md 768px, lg 1024px, xl 1280px, 2xl 1536px.

### Rules

- Prefer `min-height: 100dvh` over fixed viewport height.
- Keep shell strips single-purpose and dense; let the main canvas breathe with 8-16px padding.
- Avoid nested framed cards unless the inner layer is an intentional inset control.

## 5. Components

### App Shell

- **Structure**: title bar, safety strip, horizontal nav, scrollable main canvas, status bar.
- **Variants**: authenticated shell, print shell.
- **Spacing**: 4px shell strips, 8-16px main canvas.
- **States**: active nav, hover nav, focus command trigger, disconnected status.
- **Accessibility**: semantic `header`, `nav`, `main`, `footer`; visible focus rings.
- **Motion**: transform/opacity only; no layout animation.

### Instrument Card

- **Structure**: `Card`, `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`, `CardFooter`.
- **Variants**: glass default, inset content, active/hover glow.
- **Spacing**: 12px content, 8px header/footer.
- **States**: default, hover, focus-within, disabled via opacity.
- **Accessibility**: preserves semantic children; no card-level click target unless explicitly interactive.
- **Motion**: 180-220ms transform, border, background, and shadow changes.

### Control Button

- **Structure**: inline-flex label with optional icon.
- **Variants**: cobalt primary, orange/destructive, secondary, outline, ghost, link.
- **Spacing**: 24-44px heights with compact uppercase labels.
- **States**: default, hover, active press, focus-visible, disabled.
- **Accessibility**: visible focus ring and native disabled state.
- **Motion**: pressed controls translate 1px and compress subtly.

### Status Badge

- **Structure**: compact inline indicator.
- **Variants**: cobalt default, secondary, destructive, long, short, warn, outline, muted.
- **Spacing**: 1px vertical, 6px horizontal.
- **States**: focus ring when interactive.
- **Accessibility**: text label carries the state; color is not the only signal.
- **Motion**: color transitions only.

## 6. Motion & Interaction

### Timing

| Type | Duration | Easing | Usage |
|------|----------|--------|-------|
| Micro | 100-150ms | ease-out | Button press, LED shift |
| Standard | 180-260ms | ease-in-out | Nav hover, panel elevation |
| Emphasis | 420ms | cubic-bezier(0.16, 1, 0.3, 1) | Dialog and command palette entry |
| Scroll-driven | tied to scroll | linear | Avoid unless route-specific |

### Rules

- Only animate `transform`, `opacity`, `filter`, color, background, border-color, and shadow.
- Every interactive element has hover, active, and focus-visible states.
- Respect `prefers-reduced-motion`; persistent pulsing LEDs must become static.

## 7. Depth & Surface

### Strategy

Mixed, but constrained: OLED floor, glass panels, and neumorphic inset/raised controls.

| Level | Value | Usage |
|-------|-------|-------|
| Inset | `inset 4px 4px 10px rgba(0,0,0,.52), inset -2px -2px 8px rgba(255,255,255,.035)` | Search inputs, command keys, depressed panels |
| Raised | `10px 14px 34px rgba(0,0,0,.38), -1px -1px 0 rgba(255,255,255,.035)` | Buttons and selected surfaces |
| Glass | translucent surface plus blur, inner white line, cobalt/orange hairline | Cards, bars, popovers |
| Glow | `0 0 24px rgba(47,107,255,.18)` or orange equivalent | Active regions only |

Depth must never reduce readability. If a glow competes with data, remove the glow.
