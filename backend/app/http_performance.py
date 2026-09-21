"""Single compression layer and redacted, wire-byte access measurements."""
import gzip
import logging
import time
from urllib.parse import parse_qs
from starlette.datastructures import MutableHeaders

logger = logging.getLogger("uvicorn.error.performance")


class HttpPerformanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        size = 0
        status = 500
        start = None
        chunks = []
        compressible = False
        accept = dict(scope["headers"]).get(b"accept-encoding", b"").decode()
        accepts_gzip = any(part.split(";")[0].strip() == "gzip" and not any(
            field.strip() in {"q=0", "q=0.0", "q=0.00", "q=0.000"} for field in part.split(";")[1:]) for part in accept.split(","))

        async def measured_send(message):
            nonlocal size, status, start, compressible
            if message["type"] == "http.response.start":
                status = message["status"]
                start = message
                headers = MutableHeaders(scope=message)
                content_type = headers.get("content-type", "").split(";")[0]
                compressible = content_type == "application/json" or (content_type.startswith("text/") and content_type != "text/event-stream")
                compressible = compressible and "content-encoding" not in headers
                if not compressible:
                    await send(message)
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if compressible:
                    chunks.append(body)
                    if message.get("more_body", False):
                        return
                    body = b"".join(chunks)
                    headers = MutableHeaders(scope=start)
                    if len(body) >= 1024:
                        headers.add_vary_header("Accept-Encoding")
                        if accepts_gzip:
                            body = gzip.compress(body, compresslevel=5)
                            headers["Content-Encoding"] = "gzip"
                    headers["Content-Length"] = str(len(body))
                    await send(start)
                    message = {**message, "body": body}
                size += len(body)
                await send(message)
            else:
                await send(message)
        try:
            await self.app(scope, receive, measured_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            query = parse_qs(scope.get("query_string", b"").decode(errors="replace")) if route == "/api/media" else {}
            variant = query.get("variant", ["original"])[0] if query else "-"
            variant = variant if variant in {"thumb", "preview", "original", "-"} else "invalid"
            logger.info("route=%s method=%s status=%d duration_ms=%.1f response_bytes=%d media_variant=%s download=%s",
                        route, scope["method"], status, (time.perf_counter() - started) * 1000, size, variant,
                        query.get("download", ["0"])[0] in {"1", "true"})
