"""Shared native desktop design tokens; no user settings are written."""
import customtkinter as ctk

BG = '#090A0D'
SURFACE = '#121419'
BORDER = '#2B2F39'
TEXT = '#F3F4F6'
MUTED = '#A8B0C0'
PRIMARY = '#505ED8'
PRIMARY_HOVER = '#626FE8'
ACCENT = '#A5B4FC'
SELECTED = '#242A40'
ZEBRA = '#181B22'
SUCCESS = '#6EE7B7'
WARNING = '#F5CE7D'
WARNING_BG = '#352B17'
DANGER = '#FDA4AF'
DANGER_BG = '#351B23'


def font(size=13, bold=False):
    return ctk.CTkFont(family='Segoe UI', size=size, weight='bold' if bold else 'normal')


def button(parent, text, command, kind='secondary', **kwargs):
    styles = {
        'primary': dict(fg_color=PRIMARY, hover_color=PRIMARY_HOVER, text_color='white', border_width=0),
        'secondary': dict(fg_color=SURFACE, hover_color=SELECTED, text_color=TEXT, border_width=1, border_color=BORDER),
        'danger': dict(fg_color=SURFACE, hover_color=DANGER_BG, text_color=DANGER, border_width=1, border_color='#713546'),
        'nav': dict(fg_color=SURFACE, hover_color=ZEBRA, text_color=MUTED, border_width=0, anchor='w'),
    }
    options = dict(height=36, corner_radius=8, font=font(), text_color_disabled='#7B8497')
    options.update(styles[kind])
    options.update(kwargs)
    widget = ctk.CTkButton(parent, text=text, command=command, **options)
    widget._canvas.configure(takefocus=1)
    widget.bind('<Return>', lambda event: widget.invoke())
    widget.bind('<space>', lambda event: widget.invoke())
    widget.bind('<FocusIn>', lambda event: widget.configure(border_width=2, border_color=TEXT if kind == 'primary' else ACCENT))
    widget.bind('<FocusOut>', lambda event: widget.configure(border_width=options.get('border_width', 0), border_color=options.get('border_color', BORDER)))
    return widget
