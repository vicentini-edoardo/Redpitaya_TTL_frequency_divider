# Product

## Register

product

## Users

Small lab team of 2-5 researchers or physicists sharing a single desktop workstation. Users are technically fluent (comfortable with SSH, FPGA, signal generators) but are not software UI professionals. They use this tool while running experiments — the GUI is a means, not an end. Secondary users may be colleagues unfamiliar with the exact hardware who need to understand readouts at a glance.

## Product Purpose

Desktop control panel for a custom Red Pitaya FPGA TTL signal generator. Two operating modes (Pulse/Freq-Shift and Harmonic Generator) share one SSH session and one FPGA bitfile. The tool lets users set frequency offsets, duty cycles, harmonic multipliers, and a free-running trigger square wave, then read back live hardware status from the board. Success is fast, confident parameter changes with immediate hardware feedback and no ambiguity about what the device is doing.

## Brand Personality

Sharp, precise, trustworthy. Like a well-designed terminal or oscilloscope software: dark, monospaced, functional. The interface should disappear into the task — no decoration that doesn't carry meaning.

## Anti-references

- Consumer audio apps with glow effects and skeuomorphic knobs.
- Generic "dark SaaS dashboard" aesthetics (glassmorphism, animated gradients, hero metrics with big drop shadows).
- Medical device UIs that over-sanitize: so sparse they communicate nothing.

## Design Principles

1. **Readouts before controls.** The live hardware state (input freq, output freq, status) is what the user reads first; controls are secondary.
2. **Every color earns its place.** Cyan = live/connected, green = active/output, amber = caution/acquiring, red = off/error. No decorative color.
3. **Density without clutter.** Lab tools are dense. Embrace it — but separate concerns clearly with spacing and grouping, not borders everywhere.
4. **Labels that survive a glance.** A colleague who didn't set this up should understand the current state in under 5 seconds.
5. **The tool disappears.** When it's working, the user should only see hardware state and parameters — not the software itself.

## Accessibility & Inclusion

WCAG AA contrast for all text. No color-only state communication (pair with text labels). Monospace fonts for numeric data throughout.
