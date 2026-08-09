# CSS linting

## Setup

``` ini
npm install
npm run lint:css        # check
npm run lint:css:fix    # auto-fix what's fixable (mostly formatting; the
                         # duplicate/token rules below need a human decision)
```

## What this catches

- **`no-duplicate-selectors` / `declaration-block-no-duplicate-properties`**
  — the exact failure mode this project kept hitting: the same rule (or
  same property inside a rule) defined more than once, usually because a
  block got copy-pasted into a new page instead of reused.
- **`color-no-hex` + a `declaration-property-value-disallowed-list` rule**
  — flags a literal `#hex` or `rgb(...)` color on any color-ish property
  (background, color, border, outline, box-shadow, fill, stroke). The
  fix is almost always `rgb(var(--mpg-N))` or `rgb(var(--color-X))` from
  the `:root` token block at the top of `mpg.css`.

## Two intentional exceptions

`mpg.css` has two `/* stylelint-disable ... */` … `/* stylelint-enable ... */`
blocks, around:

1. **View Log colorization** (log severity / HTTP status colors) —
   deliberately independent of the light/dark theme system.
2. **The Pygments "codehilite" syntax-highlighting palette** — applied to
   server-rendered markdown, unrelated to the app's own color tokens.

Both are explained in the section comments right above them in `mpg.css`.
Don't remove the disable/enable pair without reading why first.

## First run

Running this against the current `mpg.css` will surface a handful of
pre-existing one-off literals outside those two blocks (a couple of hex
codes, plus `rgb(0 0 0)` / `rgb(255 255 255)` overlay colors and a few
single-use component colors like `.create-add-btn`'s gray and
`.badge-bridge`'s violet). That's expected for a rule added after the
fact — each one is a real judgment call: promote it to a token if it's
likely to recur, or leave it with an inline
`/* stylelint-disable-next-line */` if it's genuinely one-off. Neither
was pre-decided for you.

## CI

`lint-css.yml` is a standalone GitHub Actions workflow (move it to
`.github/workflows/lint-css.yml`) that installs Node and runs
`npm run lint:css` on any push/PR touching a `.css` file. If your CI is
actually orchestrated by a Python-based runner (nox, tox, a custom
`invoke`/`fabric` task, etc.), share that file and this can be wired in
as a step there instead — stylelint itself is an npm package regardless
of how it's invoked.
