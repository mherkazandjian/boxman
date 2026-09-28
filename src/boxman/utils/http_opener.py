"""
urllib openers that keep proxy credentials off redirects (#216).

urllib's ProxyHandler adds ``Proxy-Authorization`` (from a proxy URL such as
``http://user:secret@proxy:3128`` in ``http_proxy``) as an ordinary request
header, and keeps it off the wire only when it tunnels. The stock redirect
handler copies every header onto the redirected request, so when a proxied
http mirror redirects, the password goes along: to a host reached directly
(an https CDN with no https proxy set, or a ``no_proxy`` host), or in the
CONNECT to another proxy.

Standard library only: scripts/check_box_images.py imports it too.
"""

import urllib.request


class StripProxyAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Follow redirects without carrying ``Proxy-Authorization`` across them.

    The header is dropped from every redirected request, wherever it goes;
    ProxyHandler adds it back when the new destination goes through an
    authenticated proxy too, so proxied downloads keep working. The probe
    handler of scripts/check_box_images.py (#211) builds on this one.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            for hdrs in (new.headers, new.unredirected_hdrs):
                for key in [k for k in hdrs if k.lower() == "proxy-authorization"]:
                    del hdrs[key]
        return new


def build_opener() -> urllib.request.OpenerDirector:
    """
    urllib's default opener, except that a redirect never carries proxy credentials.

    Its ProxyHandler takes the proxies from the environment when the opener
    is built, so build one per download rather than keeping one around.
    """
    return urllib.request.build_opener(StripProxyAuthRedirectHandler)
