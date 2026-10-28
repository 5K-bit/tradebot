# LATHE terminal identity

LATHE is the Forex trading engine in the Blackfong project family.

- Maker's mark: `BLACKFONG / PROJECTS`.
- Display name: `LATHE`; repository and existing commands stay `tradebot`.
- Descriptor: `FOREX TRADING ENGINE`.
- Tagline: `Observe. Evaluate. Execute.`
- Footer: `LOCAL SYSTEMS. CLEAR PURPOSE. / 5K`.
- Accent: terminal bright yellow/gold (ANSI 93); terminal background is preserved.
- Layout: large wordmark, purpose, tagline and context, following Copycat and Sentinel's terminal banners.

`branding.py` owns the identity and uses only the Python standard library.
Run `python branding.py` to preview it without MT5 or a broker connection.
The trader shows the effective mode from the existing configuration schema;
preflight identifies itself as checks only. The banner does not change modes,
place orders, or claim a broker connection succeeded.

Runtime text remains ASCII to respect the project's Windows encoding checks.
Below 48 columns the large wordmark becomes a compact LATHE heading.
Redirected output, `NO_COLOR`, `TERM=dumb`, and legacy Windows consoles use
plain text. Windows Terminal and supported Unix terminals use the gold accent.
No terminal clearing, animation, extra dependency, or shell profile change.
