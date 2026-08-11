"""Visual language for the UI.

Kept in one place so a verdict looks the same everywhere it appears — the
colour a user learns on the results table is the colour they see on the
detail panel and in the counts.

Restrained on purpose. This is a tool a consultant shows a client's Basis team,
and the two things that must stand out are the system role badge and a red
verdict. Everything else stays quiet so those read instantly.
"""

from __future__ import annotations

from cdcforge.model import Verdict
from cdcforge.triage import Bucket

# --- palette ---------------------------------------------------------------

INK = "#1c1f23"
MUTED = "#5b6570"
LINE = "#e2e5e9"
CANVAS = "#ffffff"
SURFACE = "#f7f8fa"

GREEN = "#1a7f4b"
AMBER = "#9a6a00"
PURPLE = "#6b46c1"
RED = "#c0392b"
SLATE = "#6b7280"
BLUE = "#1f5fa9"

VERDICT_STYLE: dict[Verdict, tuple[str, str, str]] = {
    #                     label              colour  background
    Verdict.PASS: ("Ready", GREEN, "#e8f5ee"),
    Verdict.FAIL_FIXABLE: ("Fixable", AMBER, "#fdf3e2"),
    Verdict.MANUAL_REVIEW: ("Needs review", PURPLE, "#f1ecfb"),
    Verdict.FAIL_HARD: ("Not possible", RED, "#fbebe9"),
    Verdict.UNPARSEABLE: ("Unreadable", SLATE, "#eef0f2"),
}

BUCKET_STYLE: dict[Bucket, tuple[str, str, str]] = {
    Bucket.READY: VERDICT_STYLE[Verdict.PASS],
    Bucket.FIXABLE: VERDICT_STYLE[Verdict.FAIL_FIXABLE],
    Bucket.REVIEW: VERDICT_STYLE[Verdict.MANUAL_REVIEW],
    Bucket.NOT_POSSIBLE: VERDICT_STYLE[Verdict.FAIL_HARD],
    Bucket.UNPARSEABLE: VERDICT_STYLE[Verdict.UNPARSEABLE],
}


def verdict_label(verdict: Verdict) -> str:
    """Plain English rather than the enum.

    'FAIL_HARD' is precise and unfriendly; 'Not possible' is what the reader
    actually needs to know.
    """
    return VERDICT_STYLE[verdict][0]


def chip(text: str, colour: str, background: str, *, bold: bool = True) -> str:
    weight = "600" if bold else "500"
    return (
        f'<span style="background:{background};color:{colour};font-weight:{weight};'
        f'padding:.16rem .55rem;border-radius:1rem;font-size:.78rem;'
        f'white-space:nowrap;">{text}</span>'
    )


def verdict_chip(verdict: Verdict) -> str:
    label, colour, background = VERDICT_STYLE[verdict]
    return chip(label, colour, background)


def role_badge(role_label: str, productive: bool) -> str:
    """F-03 — visible on every screen, impossible to miss.

    The specification asks for this explicitly, and it is the one piece of
    chrome allowed to shout.
    """
    colour, background = (RED, "#fbebe9") if productive else (GREEN, "#e8f5ee")
    return (
        f'<div style="border:1px solid {colour}33;background:{background};'
        f'color:{colour};border-radius:.5rem;padding:.5rem .7rem;text-align:center;'
        f'font-weight:700;letter-spacing:.02em;font-size:.85rem;">{role_label}</div>'
    )


#: The palette as CSS custom properties, defined once for light and once for
#: dark. Streamlit exposes the user's choice, and the first version of this
#: file hard-coded light colours — so anyone running the dark theme got dark
#: chrome around white panels and grey-on-grey text. A tool shown to a
#: client's Basis team cannot look broken because of someone's OS setting.
_TOKENS = f"""
  :root {{
    --ink: {INK}; --muted: {MUTED}; --line: {LINE};
    --canvas: {CANVAS}; --surface: {SURFACE};
    --green: {GREEN}; --amber: {AMBER}; --purple: {PURPLE};
    --red: {RED}; --slate: {SLATE}; --blue: {BLUE};
    --shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 1px 3px rgba(16, 24, 40, .06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --ink: #e6e9ee; --muted: #9aa4b2; --line: #2b3138;
      --canvas: #14171b; --surface: #1a1e24;
      --green: #4ade80; --amber: #fbbf24; --purple: #c4b5fd;
      --red: #f87171; --slate: #9aa4b2; --blue: #7dd3fc;
      --shadow: none;
    }}
  }}
"""

CSS = f"""
<style>
{_TOKENS}

  /* System fonts only. A webfont import would be an outbound request from a
     tool that runs inside a customer's network and is otherwise entirely
     local — blocked in plenty of them, and a needless thing to explain to a
     security reviewer. Every OS in the room already has a good UI face. */
  html, body, .stApp, [class*="css"] {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text",
                 "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif;
    font-feature-settings: "tnum";
  }}
  .stApp {{ background: var(--canvas); }}
  .block-container {{ padding-top: 1.6rem; max-width: 1180px; }}

  h1, h2, h3 {{ color: var(--ink); letter-spacing: -0.015em; }}
  h1 {{ font-size: 1.55rem !important; font-weight: 650 !important;
        margin-bottom: .2rem !important; }}
  h2 {{ font-size: 1.12rem !important; font-weight: 620 !important;
        margin-top: 1.7rem !important; }}
  h3 {{ font-size: .96rem !important; font-weight: 600 !important;
        margin-top: 1.3rem !important; }}
  p, li, label, .stMarkdown {{ color: var(--ink); }}

  .cdc-sub {{ color: var(--muted); font-size: .9rem; margin: -.15rem 0 1.2rem; }}

  /* masthead ------------------------------------------------------------- */
  .cdc-brand {{ display: flex; align-items: center; gap: .55rem;
                margin: 0 0 .15rem; }}
  .cdc-brand .mark {{ width: 1.65rem; height: 1.65rem; border-radius: .45rem;
                      background: linear-gradient(135deg, var(--blue), var(--purple));
                      display: flex; align-items: center; justify-content: center;
                      color: #fff; font-size: .82rem; font-weight: 700; }}
  .cdc-brand .name {{ font-size: 1.02rem; font-weight: 650; color: var(--ink);
                      letter-spacing: -.01em; }}

  /* stepper -------------------------------------------------------------- */
  .cdc-steps {{ display: flex; gap: .3rem; margin: .1rem 0 1.4rem;
                border-bottom: 1px solid var(--line); padding-bottom: .7rem; }}
  .cdc-step {{ font-size: .78rem; color: var(--muted); padding: .2rem .55rem;
               border-radius: 1rem; white-space: nowrap; }}
  .cdc-step.on {{ color: var(--blue); background: color-mix(in srgb, var(--blue) 12%, transparent);
                  font-weight: 600; }}
  .cdc-step.done {{ color: var(--green); }}

  /* count tiles ---------------------------------------------------------- */
  .cdc-tiles {{ display: flex; gap: .6rem; flex-wrap: wrap; margin: .4rem 0 1rem; }}
  .cdc-tile {{ border: 1px solid var(--line); border-radius: .7rem;
               padding: .7rem .95rem; min-width: 6.8rem; background: var(--canvas);
               box-shadow: var(--shadow); }}
  .cdc-tile .n {{ font-size: 1.6rem; font-weight: 680; line-height: 1.1;
                  font-variant-numeric: tabular-nums; }}
  .cdc-tile .l {{ font-size: .7rem; color: var(--muted); text-transform: uppercase;
                  letter-spacing: .06em; margin-top: .15rem; font-weight: 500; }}

  /* finding rows --------------------------------------------------------- */
  .cdc-finding {{ border-left: 3px solid var(--line); padding: .4rem 0 .4rem .75rem;
                  margin: .35rem 0; font-size: .87rem; }}
  .cdc-finding .rule {{ font-family: ui-monospace, "SF Mono", Consolas, monospace;
                        font-size: .76rem; color: var(--muted); }}
  .cdc-finding .fix {{ color: var(--muted); font-size: .82rem; margin-top: .2rem; }}
  .cdc-hard {{ border-left-color: var(--red); }}
  .cdc-fix  {{ border-left-color: var(--amber); }}
  .cdc-rev  {{ border-left-color: var(--purple); }}
  .cdc-warn {{ border-left-color: var(--slate); }}

  .cdc-note {{ background: var(--surface); border: 1px solid var(--line);
               border-radius: .6rem; padding: .75rem .95rem; font-size: .87rem;
               color: var(--ink); margin: .6rem 0; }}

  /* sidebar -------------------------------------------------------------- */
  section[data-testid="stSidebar"] {{ background: var(--surface);
                                      border-right: 1px solid var(--line); }}
  section[data-testid="stSidebar"] .block-container {{ padding-top: 1.3rem; }}
  section[data-testid="stSidebar"] hr {{ margin: .9rem 0; border-color: var(--line); }}

  /* controls ------------------------------------------------------------- */
  .stButton button {{ border-radius: .5rem; font-weight: 560;
                      transition: transform .04s ease, box-shadow .12s ease; }}
  .stButton button:hover {{ box-shadow: var(--shadow); }}
  .stButton button:active {{ transform: translateY(1px); }}
  .stButton button[kind="primary"] {{ background: var(--blue); border-color: var(--blue); }}

  div[data-testid="stDataFrame"] {{ border: 1px solid var(--line);
                                    border-radius: .6rem; overflow: hidden; }}
  div[data-testid="stExpander"] {{ border: 1px solid var(--line);
                                   border-radius: .6rem; }}
  code {{ font-size: .84em; }}

  /* Streamlit chrome we do not want in front of a client's Basis team */
  footer, #MainMenu, [data-testid="stToolbar"],
  [data-testid="stDecoration"] {{ visibility: hidden; }}
</style>
"""


def brand() -> str:
    """The masthead. One mark, one name, no tagline shouting."""
    return (
        '<div class="cdc-brand"><div class="mark">◆</div>'
        '<div class="name">CDC Forge</div></div>'
    )


def stepper(steps: list[str], current: str) -> str:
    """The four steps as a quiet progress rail rather than a radio list."""
    done = steps.index(current) if current in steps else 0
    cells = []
    for index, step in enumerate(steps):
        state = "on" if index == done else ("done" if index < done else "")
        cells.append(f'<div class="cdc-step {state}">{step}</div>')
    return f'<div class="cdc-steps">{"".join(cells)}</div>'


def tiles(items: list[tuple[str, object, str]]) -> str:
    """``[(label, value, colour)]`` → a row of count tiles."""
    cells = "".join(
        f'<div class="cdc-tile"><div class="n" style="color:{colour}">{value}</div>'
        f'<div class="l">{label}</div></div>'
        for label, value, colour in items
    )
    return f'<div class="cdc-tiles">{cells}</div>'
