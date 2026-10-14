"""Tables and callouts for periodic reports within the current email layout."""
from html import escape

NAVY = '#eff6ff'
MUTED = '#93c5fd'
BLUE = '#3b82f6'


def paragraph(value: str, *, muted: bool = False) -> str:
    color = MUTED if muted else NAVY
    return f'<p style="margin:0 0 16px;color:{color};font-size:15px;line-height:24px;">{escape(value)}</p>'


def section(title: str, body: str) -> str:
    return f'<h2 style="margin:24px 0 12px;color:{NAVY};font-size:18px;">{escape(title)}</h2>{body}'


def data_table(rows) -> str:
    items = ''.join(
        f'<tr><td style="padding:10px;color:{MUTED};border-bottom:1px solid #1f2b46;">{escape(str(label))}</td>'
        f'<td style="padding:10px;color:{NAVY};text-align:right;border-bottom:1px solid #1f2b46;overflow-wrap:anywhere;">{escape(str(value))}</td></tr>'
        for label, value in rows
    )
    return f'<table role="presentation" width="100%" style="border-collapse:collapse;">{items}</table>'


def comparison_table(rows) -> str:
    return data_table([(label, f'{start} → {end}') for label, start, end in rows])


def metric_cards(metrics) -> str:
    items = ''.join(
        f'<td valign="top" style="padding:14px;border-top:3px solid {BLUE};background:#16223b;">'
        f'<div style="color:{MUTED};font-size:13px;">{escape(str(label))}</div>'
        f'<div style="color:{NAVY};font-size:26px;font-weight:700;">{escape(str(value))}</div>'
        f'<div style="color:{MUTED};font-size:12px;">{escape(str(caption))}</div></td>'
        for label, value, caption in metrics
    )
    return f'<table role="presentation" width="100%"><tr>{items}</tr></table>'


def note(title: str, items) -> str:
    points = ''.join(f'<li>{escape(str(item))}</li>' for item in items)
    return (f'<div style="margin:20px 0;padding:15px;border-left:3px solid {BLUE};background:#16223b;">'
            f'<strong style="color:{NAVY};">{escape(title)}</strong>'
            f'<ul style="color:{MUTED};line-height:21px;">{points}</ul></div>')
