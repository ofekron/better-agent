from __future__ import annotations

from collections.abc import Callable

from deep_link import DeepLinkError, parse_deep_link


def install_macos_url_handler(on_event: Callable[[dict[str, str]], None]) -> None:
    from webview.platforms.cocoa import BrowserView

    class BetterAgentAppDelegate(BrowserView.AppDelegate):
        def application_openURLs_(self, _application, urls):
            for url in urls:
                try:
                    event = parse_deep_link(str(url.absoluteString())).as_event()
                except (DeepLinkError, ValueError):
                    continue
                on_event(event)

    BrowserView._shared_app_delegate = BetterAgentAppDelegate.alloc().init()
