"""Email-safe blue layout matching the EngageMate website."""
from html import escape
from urllib.parse import urlsplit

from engagemate.web_origin import support_email, web_origin


def email_layout(title: str, body: str, *, button_label: str = "Open dashboard",
                 button_url: str | None = None,
                 footer: str = "Your EngageMate activity update",
                 unsubscribe_url: str | None = None,
                 unsubscribe_description: str = "these emails") -> str:
    # Links come from ENGAGEMATE_WEB_ORIGIN; rendering fails when it is unset.
    origin = web_origin()
    if button_url is None:
        button_url = origin
    origin_label = escape(urlsplit(origin).netloc)
    settings_url = escape(f"{origin}/settings", quote=True)
    support = support_email()
    support_html = (
        f'<br><a href="mailto:{escape(support, quote=True)}" style="color:#bfdbfe;">{escape(support)}</a>'
        if support else ''
    )
    origin_href = escape(origin, quote=True)
    title = escape(title)
    button_label, button_url, footer = escape(button_label), escape(button_url, quote=True), escape(footer)
    # Every email a user can opt out of carries its own signed link. Only the
    # outer footer text changes, so the rest of the layout stays byte-stable.
    if unsubscribe_url:
        link_style = 'style="color:#bfdbfe;text-decoration:underline;"'
        footer += (
            f'<br><span style="color:#93c5fd;">You get {escape(unsubscribe_description)} because you have an EngageMate account.</span><br>'
            f'<a href="{escape(unsubscribe_url, quote=True)}" {link_style}>Unsubscribe</a>'
            f' &middot; <a href="{settings_url}" {link_style}>Manage email settings</a>'
        )
    return f'''<!doctype html><html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>@media screen and (max-width:600px){{.container{{width:100%!important}}.p-24{{padding:16px!important}}.h1{{font-size:22px!important;line-height:28px!important}}.btn{{display:block!important;width:100%!important}}}}</style></head>
<body style="margin:0;padding:0;background:#0b0c10;"><center style="width:100%;background:#0b0c10;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" class="container" cellspacing="0" cellpadding="0" border="0" style="width:600px;max-width:600px;background:#0f1424;border-radius:12px;overflow:hidden;border:1px solid #1f2b46;">
<tr><td style="background:#0b1b3a;padding:16px 24px;border-bottom:1px solid #1d4ed8;font:12px Arial,Helvetica,sans-serif;letter-spacing:1px;color:#bfdbfe;text-transform:uppercase;">EngageMate</td></tr>
<tr><td class="p-24" style="padding:24px;font:15px/22px Arial,Helvetica,sans-serif;color:#dbeafe;">
<h1 class="h1" style="margin:0 0 16px;font-size:24px;line-height:30px;color:#eff6ff;">{title}</h1>
<!--email-content-->
{body}
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td style="padding:16px;border:1px solid #1f2b46;border-radius:10px;">
<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="{button_url}" style="height:44px;v-text-anchor:middle;width:220px;" arcsize="12%" strokecolor="#3b82f6" fillcolor="#3b82f6"><w:anchorlock xmlns:w="urn:schemas-microsoft-com:office:word"/><center style="color:#ffffff;font:700 14px Arial;">{button_label}</center></v:roundrect><![endif]-->
<!--[if !mso]><!--><a href="{button_url}" class="btn" style="background:#3b82f6;border:1px solid #3b82f6;border-radius:8px;color:#ffffff;display:inline-block;font:700 14px/44px Arial;text-align:center;text-decoration:none;width:220px;">{button_label}</a><!--<![endif]-->
<p style="font-size:12px;line-height:18px;color:#93c5fd;">Review activity and manage your settings at <a href="{origin_href}" style="color:#bfdbfe;">{origin_label}</a>.</p></td></tr></table>
<p style="margin:18px 0 0;font-size:13px;color:#a5b4fc;">EngageMate{support_html}</p>
</td></tr><tr><td style="background:#0b1b3a;padding:16px 24px;text-align:center;font:12px Arial;color:#bfdbfe;">{footer}</td></tr></table></td></tr></table></center></body></html>'''
