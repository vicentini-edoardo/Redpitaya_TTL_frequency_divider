# Product

## Register

product

## Users

Small lab groups who control a Red Pitaya TTL frequency generator from a desktop computer. Users are technical and comfortable with lab hardware, but the interface should still support repeated use by more than one person without relying on private memory or hidden workflows.

## Product Purpose

The application provides a single desktop control panel for connecting to a Red Pitaya over SSH, uploading and compiling the board helper, switching between pulse/frequency-shift and harmonic generation modes, applying output settings, reading live FPGA status, and controlling the independent DIO2 trigger output. Success means users can quickly confirm connection state, input/output frequency, active mode, and output state, then make precise changes without visual noise or ambiguity.

## Brand Personality

Calm, precise, practical. The product should feel like reliable desktop lab software: clear enough for shared use, restrained enough for long sessions, and confident about hardware state.

## Anti-references

Avoid marketing-style dashboards, decorative dark-mode panels, game-like neon controls, oversized cards, and designs that obscure the distinction between live board state and editable parameters. Avoid interfaces that feel like a developer terminal unless the user is reading the log.

## Design Principles

- Put hardware state first: connection, mode, input, output, and output override state must be visible at a glance.
- Separate live readings from pending edits so users know what is measured, what is configured, and what will be applied.
- Keep controls familiar: standard fields, tabs, buttons, and status indicators should behave like desktop software.
- Use restrained color for meaning: primary actions, selection, success, warning, and danger only.
- Preserve precision: values, units, and register-derived details remain available without dominating the main workflow.

## Accessibility & Inclusion

No special constraints requested. Use readable contrast, keyboard-accessible controls, color plus text for status, reduced-motion-safe feedback, and standard desktop widget affordances.
