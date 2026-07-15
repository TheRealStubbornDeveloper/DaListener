from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
import webbrowser
import json
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from platformdirs import user_data_path
from pydantic import BaseModel, Field

from .contracts import (
    BrowserCaptureAck,
    BrowserCaptureHello,
    BootstrapResponse,
)
from .events import EventHub
from .meetings import BrowserMeetingManager
from .auth import DashboardLaunchStore
from .platform import open_folder
from .provider_mode import ProviderModeStore
from .settings import OpenAISettingsStore
from .usage import OpenAIOrganizationUsage
from .uploads import MediaUploadService
from .sources import classify_shared_label


class OpenAIKeyUpdate(BaseModel):
    api_key: str = Field(min_length=20, max_length=512)


class OpenAIAdminKeyUpdate(BaseModel):
    admin_key: str = Field(min_length=20, max_length=512)


class LocalPrepareRequest(BaseModel):
    accepted_license: bool = False


class ProviderSettingsUpdate(BaseModel):
    api_key: str | None = Field(default=None, min_length=20, max_length=512)
    admin_key: str | None = Field(default=None, min_length=20, max_length=512)
    mode: str | None = Field(default=None, pattern="^(auto|cloud|local)$")


class MeetingQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class DashboardContext:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.launch_token = DashboardLaunchStore(data_dir / "dashboard-auth.json").token()
        self.session_token = secrets.token_urlsafe(32)
        self.hub = EventHub()
        self.settings = OpenAISettingsStore()
        self.provider_mode = ProviderModeStore(data_dir / "provider-mode.json")
        self.meetings = BrowserMeetingManager(data_dir, self.hub, self.settings, provider_mode_store=self.provider_mode)
        self.uploads = MediaUploadService(data_dir, self.settings, self.meetings.local_models, self.provider_mode.load)
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
        await context.meetings.intelligence.close()

    app = FastAPI(title="DaListener Dashboard API", version="1.0.0", lifespan=lifespan)
    app.state.context = context

    def require_session(
        dalistener_session: str | None = Cookie(default=None),
        x_dalistener_token: str | None = Header(default=None),
    ) -> None:
        if not secrets.compare_digest(dalistener_session or x_dalistener_token or "", context.session_token):
            raise HTTPException(status_code=401, detail="Dashboard session required")

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
    async def bootstrap():
        return BootstrapResponse(
            meetings=context.meetings.summaries(),
            openai=context.meetings.openai_status(),
            browser_audio_token=context.session_token,
            provider_mode=context.provider_mode.load(),
            pricing=context.meetings.pricing.snapshot(refresh=False).to_dict(),
            usage=context.meetings.usage.totals(context.meetings.pricing.snapshot(refresh=False).rate_per_minute_usd),
            local_model=context.meetings.local_models.public_status(),
        )

    @app.get("/api/v1/health", include_in_schema=False)
    async def health():
        return {"app": "DaListener", "status": "ready"}

    @app.post("/api/v1/application/stop", dependencies=[Depends(require_session)])
    async def stop_application():
        server = getattr(app.state, "uvicorn_server", None)
        if server is None:
            raise HTTPException(status_code=503, detail="Application server is not ready")
        server.should_exit = True
        return {"ok": True}

    @app.post("/api/v1/uploads/transcribe", dependencies=[Depends(require_session)])
    async def transcribe_upload(
        media: UploadFile = File(...),
        watched_names: str = Form("Vlad,Vladimir"),
        provider: str = Form("auto", pattern="^(auto|cloud|local)$"),
    ):
        suffix = Path(media.filename or "upload.bin").suffix[:16]
        context.uploads.temp_dir.mkdir(parents=True, exist_ok=True)
        temporary = context.uploads.temp_dir / f"{secrets.token_hex(16)}{suffix}"
        total = 0
        try:
            with temporary.open("wb") as output:
                while chunk := await media.read(1024 * 1024):
                    total += len(chunk)
                    if total > 4 * 1024**3:
                        raise HTTPException(status_code=413, detail="Upload exceeds the 4 GB local limit")
                    output.write(chunk)
            names = [name.strip() for name in watched_names.split(",") if name.strip()]
            return await context.uploads.process(temporary, media.filename or "upload", names, provider)
        finally:
            await media.close()
            temporary.unlink(missing_ok=True)

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

    @app.put("/api/v1/settings/openai/admin", dependencies=[Depends(require_session)])
    async def update_openai_admin_settings(update: OpenAIAdminKeyUpdate):
        try:
            context.settings.save_admin_key(update.admin_key)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not store the Admin key securely: {exc}") from exc
        return {"configured": True, "message": "OpenAI Admin key stored in the operating-system credential store."}

    @app.get("/api/v1/settings/providers", dependencies=[Depends(require_session)])
    async def provider_settings():
        settings = context.settings.load()
        return {
            "policy": context.provider_mode.load(),
            "openai_configured": bool(settings.api_key),
            "admin_key_configured": bool(settings.admin_key),
            "local": context.meetings.local_models.public_status(),
        }

    @app.put("/api/v1/settings/providers", dependencies=[Depends(require_session)])
    async def update_provider_settings(update: ProviderSettingsUpdate):
        try:
            if update.api_key:
                context.settings.save_api_key(update.api_key)
            if update.admin_key:
                context.settings.save_admin_key(update.admin_key)
            if update.mode:
                context.provider_mode.save(update.mode)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not store provider credentials securely: {exc}") from exc
        return await provider_settings()

    @app.get("/api/v1/pricing", dependencies=[Depends(require_session)])
    async def pricing(refresh: bool = True):
        return (await asyncio.to_thread(context.meetings.pricing.snapshot, refresh)).to_dict()

    @app.get("/api/v1/usage", dependencies=[Depends(require_session)])
    async def usage(meeting_id: str | None = None, include_organization: bool = False):
        price = context.meetings.pricing.snapshot(refresh=False)
        result = context.meetings.usage.totals(price.rate_per_minute_usd, meeting_id)
        result["estimate_only"] = not bool(context.settings.load().admin_key)
        if include_organization:
            result["organization"] = await asyncio.to_thread(OpenAIOrganizationUsage().month, context.settings.load().admin_key)
        return result

    @app.get("/api/v1/capability", dependencies=[Depends(require_session)])
    async def capability():
        return context.meetings.local_models.public_status()

    @app.get("/api/v1/local-model/status", dependencies=[Depends(require_session)])
    async def local_model_status():
        return context.meetings.local_models.public_status()

    @app.post("/api/v1/local-model/prepare", dependencies=[Depends(require_session)])
    async def local_model_prepare(request: LocalPrepareRequest):
        try:
            return context.meetings.local_models.start_prepare(request.accepted_license)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/local-model/cancel", dependencies=[Depends(require_session)])
    async def local_model_cancel():
        context.meetings.local_models.cancel()
        return {"ok": True}

    @app.post("/api/v1/transcripts/open-folder", dependencies=[Depends(require_session)])
    async def open_transcript_folder():
        path = context.data_dir / "Transcripts"
        open_folder(path)
        return {"ok": True, "path": str(path)}

    @app.get("/api/v1/meetings/{meeting_id}/transcript", dependencies=[Depends(require_session)])
    async def transcript(meeting_id: str):
        return context.meetings.transcript(meeting_id)

    @app.get("/api/v1/meetings/{meeting_id}/notes", dependencies=[Depends(require_session)])
    async def meeting_notes(meeting_id: str):
        return context.meetings.store.notes(meeting_id) or {}

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

    @app.post("/api/v1/meetings/{meeting_id}/summarize", dependencies=[Depends(require_session)])
    async def summarize_meeting(meeting_id: str):
        notes = await context.meetings.intelligence.summarize(meeting_id)
        if notes is None:
            raise HTTPException(
                status_code=503,
                detail="No notes were generated. Confirm finalized transcript exists and OpenAI or the local LFM runtime is ready.",
            )
        return notes

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

    @app.websocket("/api/v1/browser/audio")
    async def browser_audio(websocket: WebSocket):
        cookie = websocket.cookies.get("dalistener_session", "")
        token = websocket.query_params.get("token", "")
        if not secrets.compare_digest(cookie or token, context.session_token):
            await websocket.close(code=4401, reason="Dashboard session required")
            return
        await websocket.accept()
        meeting_id: str | None = None
        try:
            hello = BrowserCaptureHello.model_validate_json(await websocket.receive_text())
            source = classify_shared_label(hello.title)
            runtime = await context.meetings.start_browser_meeting(
                hello.title, "", None, hello.browser, hello.sample_rate, source,
            )
            meeting_id = runtime.summary.id
            ack = BrowserCaptureAck(
                meeting_id=meeting_id,
                title=runtime.summary.title,
                transcription_provider=runtime.summary.transcription_provider,
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
    # Windows permits multiple live listeners to share a port when
    # SO_REUSEADDR is enabled, which can route authenticated requests to
    # different DaListener processes. Unix uses the option only for quick
    # restart after TIME_WAIT.
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    configured_port = os.environ.get("DALISTENER_PORT")
    preferred_port = int(configured_port or "8765")
    try:
        sock.bind(("127.0.0.1", preferred_port))
    except OSError:
        if configured_port:
            raise
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/api/v1/health", timeout=2) as response:
                health = json.loads(response.read().decode("utf-8"))
            if health == {"app": "DaListener", "status": "ready"}:
                url = f"http://127.0.0.1:8765/auth/exchange?token={app.state.context.launch_token}"
                print(f"DaListener already running: {url}", flush=True)
                webbrowser.open(url)
                sock.close()
                return
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        print(
            "DaListener warning: port 8765 is unavailable; using a temporary port. "
            "The dashboard will use the temporary local address for this run.",
            flush=True,
        )
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    app.state.context.port = port
    runtime_path = app.state.context.data_dir / "dashboard-runtime.json"
    runtime_path.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    url = f"http://127.0.0.1:{port}/auth/exchange?token={app.state.context.launch_token}"
    print(f"DaListener dashboard: {url}", flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server

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
            try:
                current = json.loads(runtime_path.read_text(encoding="utf-8"))
                if current.get("pid") == os.getpid():
                    runtime_path.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                pass

    asyncio.run(serve())


if __name__ == "__main__":
    main()
