"""Escaping for the HTML messages this bot sends.

Every message goes out with parse_mode=HTML, so any value that came from outside the
code — a driver's name, a group title, a reverse-geocoded address, a unit somebody
typed — must have its `<`, `>` and `&` escaped before it is interpolated into one.
Telegram rejects a message whose entities don't parse, and a rejected alert is not
delivered late or in part: it is not delivered at all.

Only the interpolated values are escaped, never the surrounding template — the <b> and
<code> tags in the templates are ours and are meant to reach Telegram as markup.
"""

from html import escape


def esc(value) -> str:
    """`value` as text that is safe to interpolate into an HTML-parse-mode message.

    quote=False on purpose: Telegram's HTML subset only requires `<`, `>` and `&` to be
    escaped in text, and leaving quotes alone keeps addresses and names readable. Any
    value that ever lands inside a tag *attribute* (an href, say) needs escape(...,
    quote=True) at that call site instead.
    """
    return escape("" if value is None else str(value), quote=False)
