"""A local RSS feed for the outbound baggage tests.

NetBox's RSSFeedWidget fetches its feed with `requests` from the web process while the home page
renders, so a dashboard holding only that widget gives a real outbound call made inside a request.
"""

import http.server
import threading
import uuid

from extras.models import Dashboard

_RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>otel</title></channel></rss>'


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.received_headers.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(_RSS)))
        self.end_headers()
        self.wfile.write(_RSS)

    def log_message(self, *args):
        pass


class FeedServer:
    """Serves the feed on 127.0.0.1 and records the headers of every request it receives."""

    def __enter__(self):
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.received_headers = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        # A fresh URL per run: the widget caches a feed by URL.
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/feed?run={uuid.uuid4().hex}"
        return self

    @property
    def received_headers(self):
        return self._server.received_headers

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


def use_feed_dashboard(user, feed_url):
    """Give user a dashboard with only an RSS widget on feed_url (rolled back with the test)."""
    widget = str(uuid.uuid4())
    Dashboard.objects.update_or_create(
        user=user,
        defaults={
            "layout": [{"id": widget, "x": 0, "y": 0, "w": 4, "h": 3}],
            "config": {
                widget: {
                    "class": "extras.RSSFeedWidget",
                    "title": "otel",
                    "color": None,
                    "config": {
                        "feed_url": feed_url,
                        "requires_internet": False,
                        "max_entries": 1,
                        "cache_timeout": 600,
                        "request_timeout": 3,
                    },
                }
            },
        },
    )
