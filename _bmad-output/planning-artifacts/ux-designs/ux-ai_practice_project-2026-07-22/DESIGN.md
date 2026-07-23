---
name: Ballast
status: final
created: 2026-07-22
updated: 2026-07-22
theme: ballast-terminal    # v1 default; theming is token-based, so a calm/market theme is a later swap
colors:
  bg: "#05060a"
  bg-2: "#080b10"
  surface: "#0a0d12"
  surface-2: "#0e121a"
  line: "#1c2733"
  line-red: "#4a1119"
  text: "#e9edf2"          # soft white — coach PROSE
  muted: "#7b8a90"
  phosphor: "#5dff8a"      # green terminal — interface, data, labels, up
  phosphor-dim: "#2f9f5c"
  brand-red: "#ff2b3a"     # HERO — brand/identity ONLY, rare, never loss
  brand-red-deep: "#c01522"
  accent-pink: "#ff5cae"   # secondary/interactive — links, focus, chips, cursor
  market-up: "#5dff8a"     # green (with ▲)
  market-down: "#6ad0ff"   # sky blue (with ▼) — NEVER red/pink
  uncertainty: "#c77dff"   # violet — the "what I can't know" callout
typography:
  display: "Benguiat, 'Bookman Old Style', Georgia, serif"   # red wordmark / hero only
  terminal: "'VT323', 'Space Mono', ui-monospace, monospace"  # data, labels, chrome, cursor
  body: "Inter, system-ui, sans-serif"                        # coach prose (readable)
  scale: { xs: 11, sm: 13, base: 15, lg: 19, xl: 26, hero: 42 }
rounded: { base: 6, pill: 999 }
spacing: { unit: 4, scale: [4, 8, 12, 16, 20, 24, 32, 44] }
components: [wordmark, terminal-bar, cursor, button-primary, button-ghost, coach-card, data-block, uncertainty-callout, cosign-block, chip, input, market-indicator, reauth-banner]
---

# DESIGN.md — Ballast

## Brand & Style

**An 80s green-phosphor terminal humming in a Stranger Things room.** A deep, near-black blue world lit by a soft CRT glow. The **green terminal is the interface** — data, labels, the `ballast:~$` prompt, a blinking cursor — calm, nostalgic, utilitarian. The **Stranger Things red** is rare brand punctuation (the wordmark, the single most important action) — the neon sign glowing on the wall behind the terminal. **Neon pink** is the interactive spark (links, focus, chips). Personality: a patient, honest guide; the energy is *atmosphere*, not busyness. Defining rule: the interface stays serene exactly when the user is most anxious.

## Colors

| Token | Hex | Use |
| --- | --- | --- |
| `bg` / `surface` | `#05060a` / `#0a0d12` | near-black blue world; cards |
| `text` / `muted` | `#e9edf2` / `#7b8a90` | coach prose / secondary |
| **`phosphor`** | `#5dff8a` | green terminal — data, labels, chrome, market-up |
| **`brand-red`** | `#ff2b3a` | brand/identity ONLY — wordmark + primary action. Rare. |
| **`accent-pink`** | `#ff5cae` | secondary/interactive — links, focus, chips, cursor |
| `market-down` | `#6ad0ff` | losses — sky blue |
| `uncertainty` | `#c77dff` | violet — the "what I can't know" callout |

**Hard color rules:**
- **Red is never loss, alert, or error** — it is brand only, and used sparingly so it stays potent.
- **Losses use `market-down` (sky blue), never red or pink.** Gains use `phosphor` (green). Both always pair color with a **sign/icon** (▲ ▼) — never color alone.
- Long coach *prose* is soft-white `body`, not green mono (readability). Green is for **data, labels, short "system" lines, and chrome.**

## Typography

- **Display** (Benguiat-style serif): the red `BALLAST` wordmark and rare hero moments only — glowing, gentle flicker (static under reduced-motion).
- **Terminal** (VT323 / Space Mono): all data, stats, labels, the `>` prompt, the cursor — the "friendly old computer" voice.
- **Body** (Inter): everything the coach *says* — calm, jargon-free, comfortable over long reads.
- Generous line-height (~1.6) for prose.

## Layout & Spacing

- 4px base unit (scale in frontmatter). Lean generous — whitespace is part of the calm.
- Reading measure ~640–720px for coach content; dashboard container ~900px. Comfortable one-column on mobile.

## Elevation & Depth

Depth = **glow + darkness**, not heavy shadows: soft green/red glows, and a fixed **edge vignette**. **No scanlines** (they hurt reading and accessibility). Optional faint top screen-glow for CRT mood. All ambient motion (cursor blink, wordmark flicker) respects `prefers-reduced-motion`.

## Shapes

- `rounded.base` 6px (cards, buttons, inputs); pill chips.
- Signature, load-bearing shapes: **3px red left-border** = a coach card; **dashed red-tinted divider** = the co-sign zone; **blinking green cursor block** = the terminal motif.

## Components

- **wordmark** — red serif, glowing, gentle flicker (static under reduced-motion).
- **terminal-bar** — a subtle top bar with a green `ballast:~$ …` prompt on coach surfaces.
- **cursor** — blinking green block; the terminal signature.
- **button-primary** — `brand-red-deep` fill + red glow; the single most important action per view (usually Approve & Co-sign).
- **button-ghost** — muted/green outline; secondary/decline ("not now").
- **coach-card** — surface, 3px red left-border; holds recommendation → why → precedent → uncertainty → co-sign.
- **data-block** — near-black green-phosphor mono panel for precedent (up green, down sky-blue, with signs).
- **uncertainty-callout** — violet; always present on a recommendation.
- **cosign-block** — dashed red divider + note + primary action.
- **chip** — pill, pink outline (e.g., "↺ if it dips, I'll replay this").
- **market-indicator** — green ▲ / sky-blue ▼.
- **reauth-banner** — calm, neutral/muted (never red) for the ~weekly Schwab re-login.

## Do's and Don'ts

- ✅ Green = interface, pink = interactive, red = rare brand. Keep scary-moment screens the most legible and calm.
- ✅ Coach prose in `body` (soft white); green mono for data/labels only.
- ✅ Honor `prefers-reduced-motion` (kill cursor blink + flicker). Pair color with sign/icon.
- ❌ Never use red or pink for loss/error/alarm.
- ❌ No scanlines, no flashing, no aggressive motion, no unprompted attention-grabbing (pull, not push).
- ❌ No dense jargon; no long paragraphs set in green mono.
