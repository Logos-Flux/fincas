# Fincas — Grid Website Design System

**Version:** 1.0  
**Selected direction:** Grid  
**Brand line:** One system. Built your way.

## 1. Brand idea

Fincas is a modular business software platform: capable without feeling complicated, structured without feeling rigid. The Grid identity turns six simple tiles into an unmistakable **F**. Five violet tiles represent the connected system; the single lime tile represents the customer's ability to make it their own.

The website should feel:

- **Clear** — direct hierarchy, short copy, generous whitespace.
- **Modular** — repeatable layouts, cards, and product building blocks.
- **Capable** — precise typography and restrained detail rather than decorative excess.
- **Distinctive** — the lime accent is used deliberately, never as general decoration.

Avoid generic SaaS gradients, stock-photo handshakes, glowing 3D abstractions, chart-arrow clichés, and excessive purple backgrounds.

## 2. Logo suite

### 2.1 Primary files

All SVG logo wordmarks are outlined vector paths; no font installation is required to display them correctly. SVG and PNG logo files have transparent backgrounds unless noted otherwise.

| Need | File | Usage |
|---|---|---|
| Horizontal logo on light backgrounds | `assets/svg/fincas-grid-logo-light.svg` | Color mark with dark FINCAS wordmark |
| Horizontal logo on dark backgrounds | `assets/svg/fincas-grid-logo-dark.svg` | Color mark with white FINCAS wordmark |
| Standalone color mark | `assets/svg/fincas-grid-mark-color.svg` | Avatars, loading states, compact navigation |
| Black horizontal logo | `assets/svg/fincas-grid-logo-black.svg` | Single-color print and documents |
| White horizontal logo | `assets/svg/fincas-grid-logo-white.svg` | Reversed single-color applications |
| Black standalone mark | `assets/svg/fincas-grid-mark-black.svg` | Single-color light-background use |
| White standalone mark | `assets/svg/fincas-grid-mark-white.svg` | Single-color dark-background use |
| Adaptive standalone mark | `assets/svg/fincas-grid-mark-current.svg` | Inherits CSS `currentColor` |

The names **light** and **dark** describe the background the file is designed for.

PNG equivalents are in `assets/png/`. Horizontal lockups are exported at widths of 512, 1024, and 2048 px. The color mark is supplied at 128, 256, 512, and 1024 px. Use SVG by default; use PNG only when SVG is unsupported.

### 2.2 Construction

The mark uses a 3–2–1 tile arrangement:

```text
■ ■ ■
■ ■
■
```

- Tile: 42 × 42 units.
- Gap: 8 units.
- Tile corner radius: 8 units.
- Overall color-mark view box: 142 × 142 units.
- Accent tile: upper-right only.
- Horizontal lockup: 48-unit gap between mark and wordmark.

Do not redraw the mark with different gaps or substitute a typeset F.

### 2.3 Clear space and minimum size

- **Clear space:** keep at least one module — one tile plus one gap, equal to 50 construction units — clear on every side. No text, borders, imagery, or other logos should enter this area.
- **Horizontal logo:** minimum digital width 120 px; minimum print width 30 mm.
- **Standalone mark:** minimum digital size 24 × 24 px.
- **Below 24 px:** use the padded favicon artwork, not the horizontal lockup.

### 2.4 Correct use

- Use the color mark with the dark wordmark on white, pale lavender, and other light neutral surfaces.
- Use the color mark with the white wordmark on deep ink or similarly dark surfaces.
- Use the standalone mark when space is limited or the name is already clear from context.
- Use monochrome files only when color reproduction is unavailable or a single-color treatment is required.

Do not:

- Stretch, rotate, skew, or rearrange the tiles.
- Move the lime accent to another tile.
- Add shadows, glows, bevels, outlines, or gradients to the logo.
- Place the logo over busy imagery without a solid protective surface.
- Change the wordmark typeface or recreate it with live text.
- Use lime for the wordmark or as a small-text color on light backgrounds.

## 3. Color system

### 3.1 Core brand colors

| Token | Hex | Role |
|---|---|---|
| Brand primary | `#6C4CF1` | Logo tiles, primary actions, selected states |
| Brand primary hover | `#593BD9` | Hover and pressed states on light backgrounds |
| Brand accent | `#C7F36B` | One accent tile, highlights, rare high-emphasis moments |
| Brand ink | `#211C35` | Wordmark, headings, primary text |

### 3.2 Light mode

| Token | Hex | Role |
|---|---|---|
| Background | `#FCFCFE` | Main page background |
| Surface | `#FFFFFF` | Cards, navigation, forms |
| Surface tint | `#F7F5FF` | Hero sections, subtle brand panels |
| Text | `#211C35` | Primary copy |
| Text secondary | `#6F687E` | Supporting copy |
| Border | `#E2DDEA` | Dividers and card outlines |

### 3.3 Dark mode

| Token | Hex | Role |
|---|---|---|
| Background | `#121017` | Main page background |
| Surface | `#1A1722` | Cards and navigation |
| Surface raised | `#231E2E` | Popovers and elevated panels |
| Text | `#F7F5FF` | Primary copy |
| Text secondary | `#B4ADC2` | Supporting copy |
| Border | `#383143` | Dividers and card outlines |
| Primary interactive | `#9A86F7` | Links, focus rings, and interactive accents |

The dark-background logo keeps the original violet and lime mark with a white wordmark. The lighter interactive violet is for UI controls, not a recolor of the logo.

### 3.4 CSS tokens

```css
:root {
  --fincas-primary: #6c4cf1;
  --fincas-primary-hover: #593bd9;
  --fincas-accent: #c7f36b;
  --fincas-ink: #211c35;

  --fincas-bg: #fcfcfe;
  --fincas-surface: #ffffff;
  --fincas-surface-tint: #f7f5ff;
  --fincas-text: #211c35;
  --fincas-text-secondary: #6f687e;
  --fincas-border: #e2ddea;
  --fincas-focus: #6c4cf1;
}

@media (prefers-color-scheme: dark) {
  :root {
    --fincas-bg: #121017;
    --fincas-surface: #1a1722;
    --fincas-surface-tint: #231e2e;
    --fincas-text: #f7f5ff;
    --fincas-text-secondary: #b4adc2;
    --fincas-border: #383143;
    --fincas-focus: #9a86f7;
  }
}
```

Machine-readable tokens are also supplied in `assets/tokens/fincas-grid-tokens.json`.

### 3.5 Contrast guidance

Verified contrast ratios for the core combinations:

- White on brand primary `#6C4CF1`: **5.33:1**.
- Brand ink on brand accent `#C7F36B`: **12.84:1**.
- Secondary light-mode text on `#FCFCFE`: **5.18:1**.
- Secondary dark-mode text on `#121017`: **8.72:1**.
- Dark-mode interactive violet on `#121017`: **6.41:1**.

Maintain WCAG AA: 4.5:1 for normal text and 3:1 for large text and meaningful UI boundaries. Use dark ink text on lime buttons. Never use white text on lime.

## 4. Typography

Use two families:

- **Space Grotesk** — headlines, navigation, numbers, product labels, and calls to action.
- **Manrope** — body copy, descriptions, forms, and longer interface text.

Load only the weights needed. Recommended: Space Grotesk 500/600/700 and Manrope 400/500/600/700.

| Style | Desktop | Mobile | Family / weight |
|---|---:|---:|---|
| Display | 64 / 68 px | 42 / 46 px | Space Grotesk 600 |
| H1 | 56 / 60 px | 38 / 43 px | Space Grotesk 600 |
| H2 | 40 / 46 px | 31 / 37 px | Space Grotesk 600 |
| H3 | 28 / 34 px | 24 / 30 px | Space Grotesk 600 |
| Body large | 20 / 32 px | 18 / 29 px | Manrope 400 |
| Body | 16 / 26 px | 16 / 26 px | Manrope 400 |
| UI label | 14 / 20 px | 14 / 20 px | Manrope 600 |
| Eyebrow | 12 / 16 px | 12 / 16 px | Space Grotesk 700, +0.14em tracking |

Headlines may use `-0.025em` letter spacing. Body copy should remain at normal tracking. Keep paragraph measure between 55 and 75 characters.

## 5. Layout and spacing

### 5.1 Grid

- Maximum content width: **1200 px**.
- Desktop: 12 columns, 24 px gutters, 64 px page margins.
- Tablet: 8 columns, 20 px gutters, 32 px page margins.
- Mobile: 4 columns, 16 px gutters, 20 px page margins.
- Section spacing: 96–128 px desktop; 64–80 px mobile.

### 5.2 Spacing scale

Use a 4 px base unit:

```text
4, 8, 12, 16, 24, 32, 48, 64, 96, 128
```

Prefer fewer, more deliberate spaces over many arbitrary values.

### 5.3 Shape and elevation

| Element | Radius | Treatment |
|---|---|---|
| Logo tile motif | 8 units | Flat, no shadow |
| Inputs and buttons | 10 px | 1 px border where needed |
| Cards | 16 px | 1 px border; shadow only for elevation |
| Modal / large panel | 20–24 px | Restrained shadow |
| Pills and tags | 999 px | Compact labels only |

Light-mode card shadow: `0 12px 32px rgb(33 28 53 / 0.08)`. Prefer borders in dark mode; avoid large black shadows.

## 6. Website components

### 6.1 Header

- Use the horizontal color logo, approximately 32–36 px tall.
- Header height: 72–80 px desktop; 64 px mobile.
- Keep navigation simple: Product, Solutions, Pricing, Resources, Sign in, primary CTA.
- Use a white or dark surface with a subtle bottom border; avoid a permanently heavy shadow.

```html
<picture>
  <source
    srcset="/assets/svg/fincas-grid-logo-dark.svg"
    media="(prefers-color-scheme: dark)"
  />
  <img
    src="/assets/svg/fincas-grid-logo-light.svg"
    alt="Fincas"
    width="156"
    height="48"
    style="height: 34px; width: auto;"
  />
</picture>
```

### 6.2 Buttons

**Primary**

- Violet background, white label, 10 px radius.
- Height: 44 px default, 36 px compact, 52 px hero.
- Horizontal padding: 20 px default.
- Hover: `#593BD9`; pressed: slightly darker.
- Focus: visible 2–3 px ring with offset.

**Secondary**

- Transparent or surface background.
- 1 px border using the border token.
- Ink label in light mode; pale label in dark mode.

**Accent**

- Lime background with ink text.
- Reserve for one special action or highlighted conversion moment per view.

### 6.3 Cards and product modules

Cards should express the modular brand idea without turning every section into a grid of boxes.

- Use 16 px radius, 24–32 px padding, and a 1 px border.
- Keep headings short and align content to a consistent internal grid.
- Use one small tile motif or lime status detail only when meaningful.
- Hover movement, if any: maximum `translateY(-2px)` over 180 ms.

### 6.4 Hero

Recommended first-screen structure:

1. Eyebrow or concise product category.
2. One clear outcome-led headline, ideally 6–12 words.
3. Supporting copy of no more than two lines at desktop width.
4. Primary CTA and lower-emphasis secondary action.
5. Product UI or a crisp modular workflow visual.

Use pale lavender selectively as a hero surface. The main website background should remain mostly neutral.

### 6.5 Forms

- Minimum input height: 44 px.
- Persistent visible labels; do not rely on placeholder text as labels.
- Use violet for focus and lime only for confirmed positive emphasis when appropriate.
- Error states need text and iconography, not color alone.

### 6.6 Footer

- Dark ink footer is preferred for a strong final frame.
- Use the white monochrome horizontal logo or the color mark with white wordmark.
- Keep link groups aligned to the same modular grid as the page.

## 7. Iconography and imagery

- Use simple geometric icons with rounded joins and consistent stroke weight.
- Default UI icon sizes: 16, 20, and 24 px.
- Build illustrations from tiles, frames, connectors, and product UI fragments.
- Use lime as a single focal point rather than a repeated decorative wash.
- Product screenshots should be real, legible, and cropped cleanly; avoid floating them over complex 3D scenes.

## 8. Motion

- Micro-interactions: 120–180 ms.
- Component transitions: 180–240 ms.
- Section reveals, if used: 240–360 ms with minimal travel.
- Suggested easing: `cubic-bezier(0.2, 0.8, 0.2, 1)`.
- Do not animate the logo tiles continuously.
- Respect `prefers-reduced-motion`; remove nonessential movement and smooth scrolling.

## 9. Responsive behavior

- Keep the horizontal logo until the header becomes constrained; then use the standalone color mark.
- Stack hero content and product visual below 900 px.
- Collapse multi-column card grids to two columns on tablet and one on narrow mobile.
- Keep body text at 16 px or larger and touch targets at least 44 × 44 px.
- Test at 320, 375, 768, 1024, 1440, and 1920 px widths.

## 10. Voice and copy

The voice is direct, calm, and useful.

- Say what the product does before describing how advanced it is.
- Prefer “Bring your workflows into one system” over “Revolutionize your business.”
- Use short verbs: connect, automate, track, decide, grow.
- Avoid jargon unless the audience already uses it.

## 11. Favicons, app icons, and sharing

### 11.1 Included assets

| Asset | Location |
|---|---|
| SVG favicon | `assets/favicon/favicon.svg` |
| Multi-size ICO, 16/32/48 px | `assets/favicon/favicon.ico` |
| PNG favicons | `assets/favicon/favicon-16x16.png`, `favicon-32x32.png`, `favicon-48x48.png` |
| Safari pinned tab | `assets/favicon/safari-pinned-tab.svg` |
| Apple touch icon, 180 px | `assets/app/apple-touch-icon.png` |
| Android icons, 192 and 512 px | `assets/app/android-chrome-192x192.png`, `android-chrome-512x512.png` |
| Web app manifest | `assets/app/site.webmanifest` |
| Social avatar, 512 px | `assets/social/fincas-grid-avatar-512.png` |
| Open Graph image, 1200 × 630 px | `assets/social/fincas-grid-og-image.png` |

App icons and social artwork have intentional pale-lavender backgrounds and safe-area padding; they are not transparent logo exports.

### 11.2 HTML head snippet

```html
<link rel="icon" href="/assets/favicon/favicon.ico" sizes="any" />
<link rel="icon" type="image/svg+xml" href="/assets/favicon/favicon.svg" />
<link rel="apple-touch-icon" href="/assets/app/apple-touch-icon.png" />
<link rel="manifest" href="/assets/app/site.webmanifest" />
<link rel="mask-icon" href="/assets/favicon/safari-pinned-tab.svg" color="#6c4cf1" />

<meta name="theme-color" content="#f7f5ff" media="(prefers-color-scheme: light)" />
<meta name="theme-color" content="#121017" media="(prefers-color-scheme: dark)" />

<meta property="og:title" content="Fincas — One system. Built your way." />
<meta property="og:image" content="https://www.example.com/assets/social/fincas-grid-og-image.png" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta name="twitter:card" content="summary_large_image" />
```

Replace `https://www.example.com` with the production origin; social sharing images should use absolute URLs.

## 12. Delivery checklist

Before launch, confirm:

- [ ] Header switches to the white-wordmark logo in dark mode.
- [ ] SVG logos are used wherever supported.
- [ ] Logo clear space and minimum sizes are respected.
- [ ] Browser, Apple, Android, and manifest icons resolve without 404s.
- [ ] Social sharing image uses an absolute production URL.
- [ ] Text and control contrast meets WCAG AA.
- [ ] Keyboard focus is visible on every interactive element.
- [ ] Reduced-motion behavior is implemented.
- [ ] The lime accent remains selective and recognizable.
