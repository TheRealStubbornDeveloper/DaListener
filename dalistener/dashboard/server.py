from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from platformdirs import user_data_path
from pydantic import BaseModel, Field

from .contracts import (
    BootstrapResponse,
    CapturePreflightRequest,
    CapturePreflightResponse,
    CaptureWarningAcknowledgement,
    CaptureWarningPreferences,
    ExtensionAck,
    ExtensionHello,
)
from .events import EventHub
from .meetings import BrowserMeetingManager
from .pairing import ExtensionPairingStore
from .platform import open_folder, synchronize_browser_extension
from .preferences import CapturePreferenceStore
from .settings import OpenAISettingsStore
from .sources import CaptureCategory, classify_source, warning_message


class OpenAIKeyUpdate(BaseModel):
    api_key: str = Field(min_length=20, max_length=512)


class MeetingQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class DashboardContext:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.launch_token = secrets.token_urlsafe(32)
        self.extension_token = ExtensionPairingStore(data_dir / "extension-pairing.json").token()
        self.session_token = secrets.token_urlsafe(32)
        self.hub = EventHub()
        self.settings = OpenAISettingsStore()
        self.capture_preferences = CapturePreferenceStore(data_dir / "preferences.json")
        self.extension_dir = synchronize_browser_extension(data_dir / "BrowserExtension")
        self.meetings = BrowserMeetingManager(data_dir, self.hub, self.settings)
        self.port = 0


def _frontend_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "frontend"
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(data_dir: Path | None = None) -> FastAPI:
    root = Path(data_dir or os.environ.get("DALISTENER_DATA_DIR") or user_data_path("DaListener", "DaListener"))
    root.mkdir(parents=True, exist_ok=True)
    context = DashboardContext(root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        context.hub.bind()
        yield
        await asyncio.gather(
            *(context.meetings.stop(meeting.id) for meeting in context.meetings.summaries() if meeting.status != "ended"),
            return_exceptions=True,
        )

    app = FastAPI(title="DaListener Dashboard API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
        allow_credentials=False,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-DaListener-Extension-Token"],
    )
    app.state.context = context

    def require_session(
        dalistener_session: str | None = Cookie(default=None),
        x_dalistener_token: str | None = Header(default=None),
    ) -> None:
        if not secrets.compare_digest(dalistener_session or x_dalistener_token or "", context.session_token):
            raise HTTPException(status_code=401, detail="Dashboard session required")

    def require_extension(x_dalistener_extension_token: str | None = Header(default=None)) -> None:
        if not secrets.compare_digest(x_dalistener_extension_token or "", context.extension_token):
            raise HTTPException(status_code=401, detail="Pair the extension from the DaListener dashboard")

    @app.get("/auth/exchange", include_in_schema=False)
    async def exchange(token: str = Query(...)):
        if not secrets.compare_digest(token, context.launch_token):
            raise HTTPException(status_code=401, detail="Invalid launch token")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "dalistener_session",
            context.session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=12 * 60 * 60,
        )
        return response

    @app.get("/api/v1/bootstrap", response_model=BootstrapResponse, dependencies=[Depends(require_session)])
    async def bootstrap(request: Request):
        scheme = "wss" if request.url.scheme == "https" else "ws"
        return BootstrapResponse(
            meetings=context.meetings.summaries(),
            openai=context.meetings.openai_status(),
            extension_audio_url=f"{scheme}://{request.url.netloc}/api/v1/extension/audio",
        )

    @app.get("/api/v1/health", include_in_schema=False)
    async def health():
        return {"app": "DaListener", "status": "ready"}

    @app.post("/api/v1/extension/pairing", dependencies=[Depends(require_session)])
    async def extension_pairing(request: Request):
        scheme = "wss" if request.url.scheme == "https" else "ws"
        return {
            "audio_url": f"{scheme}://{request.url.netloc}/api/v1/extension/audio",
            "api_url": f"{request.url.scheme}://{request.url.netloc}",
            "token": context.extension_token,
        }

    @app.post(
        "/api/v1/extension/capture-preflight",
        response_model=CapturePreflightResponse,
        dependencies=[Depends(require_extension)],
    )
    async def capture_preflight(preflight: CapturePreflightRequest):
        source = classify_source(preflight.url)
        needs_warning = (
            source.supported
            and source.category != CaptureCategory.MEETING
            and not context.capture_preferences.is_suppressed(source.domain)
        )
        return CapturePreflightResponse(
            supported=source.supported,
            category=source.category,
            domain=source.domain,
            service_label=source.service_label,
            warning_required=needs_warning,
            warning_message=warning_message(source) if needs_warning else None,
        )

    @app.post(
        "/api/v1/extension/capture-warning/acknowledge",
        response_model=CaptureWarningPreferences,
        dependencies=[Depends(require_extension)],
    )
    async def acknowledge_capture_warning(acknowledgement: CaptureWarningAcknowledgement):
        domains = context.capture_preferences.suppressed_domains()
        if acknowledgement.suppress_for_domain:
            domains = context.capture_preferences.suppress(acknowledgement.domain)
        return CaptureWarningPreferences(suppressed_domains=domains)

    @app.get("/api/v1/meetings", dependencies=[Depends(require_session)])
    async def meetings():
        return context.meetings.summaries()

    @app.get("/api/v1/settings/openai", dependencies=[Depends(require_session)])
    async def openai_settings():
        return context.meetings.openai_status()

    @app.put("/api/v1/settings/openai", dependencies=[Depends(require_session)])
    async def update_openai_settings(update: OpenAIKeyUpdate):
        try:
            context.settings.save_api_key(update.api_key)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not store the API key securely: {exc}") from exc
        status = context.meetings.openai_status()
        context.hub.publish("openai.updated", None, status.model_dump(mode="json"))
        return status

    @app.post("/api/v1/transcripts/open-folder", dependencies=[Depends(require_session)])
    async def open_transcript_folder():
        path = context.data_dir / "Transcripts"
        open_folder(path)
        return {"ok": True, "path": str(path)}

    @app.post("/api/v1/extension/open-folder", dependencies=[Depends(require_session)])
    async def open_extension_folder():
        context.extension_dir = synchronize_browser_extension(context.extension_dir)
        open_folder(context.extension_dir)
        return {"ok": True, "path": str(context.extension_dir)}

    @app.get(
        "/api/v1/settings/capture-warnings",
        response_model=CaptureWarningPreferences,
        dependencies=[Depends(require_session)],
    )
    async def capture_warning_preferences():
        return CaptureWarningPreferences(suppressed_domains=context.capture_preferences.suppressed_domains())

    @app.delete(
        "/api/v1/settings/capture-warnings/{domain}",
        response_model=CaptureWarningPreferences,
        dependencies=[Depends(require_session)],
    )
    async def remove_capture_warning_preference(domain: str):
        return CaptureWarningPreferences(suppressed_domains=context.capture_preferences.remove(domain))

    @app.delete(
        "/api/v1/settings/capture-warnings",
        response_model=CaptureWarningPreferences,
        dependencies=[Depends(require_session)],
    )
    async def reset_capture_warning_preferences():
        return CaptureWarningPreferences(suppressed_domains=context.capture_preferences.reset())

    @app.get("/api/v1/meetings/{meeting_id}/transcript", dependencies=[Depends(require_session)])
    async def transcript(meeting_id: str):
        return context.meetings.transcript(meeting_id)

    @app.post("/api/v1/meetings/{meeting_id}/stop", dependencies=[Depends(require_session)])
    async def stop_meeting(meeting_id: str):
        await context.meetings.stop(meeting_id)
        return {"ok": True}

    @app.post("/api/v1/meetings/{meeting_id}/ask", dependencies=[Depends(require_session)])
    async def ask_meeting(meeting_id: str, request: MeetingQuestion):
        try:
            answer = await context.meetings.intelligence.answer(meeting_id, request.question)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"answer": answer}

    @app.websocket("/api/v1/events")
    async def events(websocket: WebSocket, since: int = 0):
        cookie = websocket.cookies.get("dalistener_session", "")
        token = websocket.query_params.get("token", "")
        if not secrets.compare_digest(cookie or token, context.session_token):
            await websocket.close(code=4401)
            return
        origin = websocket.headers.get("origin")
        if origin and origin not in {f"http://127.0.0.1:{context.port}", f"http://localhost:{context.port}"}:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        queue = context.hub.subscribe(since)
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            context.hub.unsubscribe(queue)

    @app.websocket("/api/v1/extension/audio")
    async def extension_audio(websocket: WebSocket):
        await websocket.accept()
        meeting_id: str | None = None
        try:
            hello = ExtensionHello.model_validate_json(await websocket.receive_text())
            if not secrets.compare_digest(hello.token, context.extension_token):
                await websocket.close(code=4401, reason="Pair the extension from the DaListener dashboard")
                return
            runtime = await context.meetings.start_browser_meeting(
                hello.title, hello.url, hello.tab_id, hello.browser, hello.sample_rate,
            )
            meeting_id = runtime.summary.id
            ack = ExtensionAck(
                meeting_id=meeting_id,
                transcription_model=runtime.summary.transcription_model,
            )
            await websocket.send_text(ack.model_dump_json())
            while True:
                message = await websocket.receive()
                if message.get("bytes") is not None:
                    context.meetings.accept_pcm(meeting_id, message["bytes"])
                elif message.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            try:
                await websocket.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass
        finally:
            if meeting_id:
                await context.meetings.stop(meeting_id)

    frontend = _frontend_dir()
    assets = frontend / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend_fallback(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        index = frontend / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse(
            {"detail": "Dashboard frontend is not built. Run npm.cmd install and npm.cmd run build in frontend/."},
            status_code=503,
        )

    return app


def main() -> None:
    app = create_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    configured_port = os.environ.get("DALISTENER_PORT")
    preferred_port = int(configured_port or "8765")
    try:
        sock.bind(("127.0.0.1", preferred_port))
    except OSError:
        if configured_port:
            raise
        print(
            "DaListener warning: port 8765 is unavailable; using a temporary port. "
            "Pair the extension again for this run.",
            flush=True,
        )
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    app.state.context.port = port
    url = f"http://127.0.0.1:{port}/auth/exchange?token={app.state.context.launch_token}"
    print(f"DaListener dashboard: {url}", flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = uvicorn.Server(config)

    async def serve() -> None:
        async def open_dashboard_when_ready() -> None:
            while not server.started and not server.should_exit:
                await asyncio.sleep(0.05)
            if server.started:
                await asyncio.to_thread(webbrowser.open, url)

        browser_task = asyncio.create_task(open_dashboard_when_ready())
        try:
            await server.serve(sockets=[sock])
        finally:
            if not browser_task.done():
                browser_task.cancel()
            await asyncio.gather(browser_task, return_exceptions=True)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
