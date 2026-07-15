import type { Bootstrap, DashboardEvent, LocalModelStatus, Pricing, TranscriptEvent, Usage } from "./types";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init
  });
  if (!response.ok) throw new Error((await response.text()) || `${response.status}`);
  return response.json() as Promise<T>;
}

export const api = {
  bootstrap: () => json<Bootstrap>("/api/v1/bootstrap"),
  transcript: (meetingId: string) => json<TranscriptEvent[]>(`/api/v1/meetings/${meetingId}/transcript`),
  notes: (meetingId: string) => json<Record<string, unknown>>(`/api/v1/meetings/${meetingId}/notes`),
  stop: (meetingId: string) => json<{ ok: boolean }>(`/api/v1/meetings/${meetingId}/stop`, { method: "POST" }),
  saveOpenAIKey: (apiKey: string) => json("/api/v1/settings/openai", { method: "PUT", body: JSON.stringify({ api_key: apiKey }) }),
  saveOpenAIAdminKey: (adminKey: string) => json("/api/v1/settings/openai/admin", { method: "PUT", body: JSON.stringify({ admin_key: adminKey }) }),
  saveProviderMode: (mode: "auto" | "cloud" | "local") => json("/api/v1/settings/providers", { method: "PUT", body: JSON.stringify({ mode }) }),
  pricing: (refresh = true) => json<Pricing>(`/api/v1/pricing?refresh=${refresh}`),
  usage: (meetingId?: string, includeOrganization = false) => json<Usage>(`/api/v1/usage?include_organization=${includeOrganization}${meetingId ? `&meeting_id=${encodeURIComponent(meetingId)}` : ""}`),
  localModel: () => json<LocalModelStatus>("/api/v1/local-model/status"),
  prepareLocal: (acceptedLicense: boolean) => json<LocalModelStatus>("/api/v1/local-model/prepare", { method: "POST", body: JSON.stringify({ accepted_license: acceptedLicense }) }),
  cancelLocal: () => json<{ok: boolean}>("/api/v1/local-model/cancel", { method: "POST" }),
  openTranscriptFolder: () => json("/api/v1/transcripts/open-folder", { method: "POST" }),
  stopApplication: () => json<{ok: boolean}>("/api/v1/application/stop", { method: "POST" }),
  summarize: (meetingId: string) => json(`/api/v1/meetings/${meetingId}/summarize`, { method: "POST" }),
  ask: (meetingId: string, question: string) => json<{ answer: string }>(`/api/v1/meetings/${meetingId}/ask`, { method: "POST", body: JSON.stringify({ question }) })
};

export async function uploadMedia(file: File, watchedNames: string, provider: "auto" | "cloud" | "local"): Promise<{job_id: string; status: string; file_name: string; progress: number}> {
  const body = new FormData();
  body.append("media", file);
  body.append("watched_names", watchedNames);
  body.append("provider", provider);
  const response = await fetch("/api/v1/uploads/transcribe", { method: "POST", credentials: "same-origin", body });
  if (!response.ok) throw new Error((await response.text()) || `${response.status}`);
  return response.json();
}

export function connectEvents(since: number, onEvent: (event: DashboardEvent) => void, onState: (state: string) => void) {
  let stopped = false;
  let socket: WebSocket | null = null;
  let retry = 500;

  const open = () => {
    if (stopped) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/api/v1/events?since=${since}`);
    socket.onopen = () => { retry = 500; onState("connected"); };
    socket.onmessage = message => {
      const event = JSON.parse(message.data) as DashboardEvent;
      since = Math.max(since, event.sequence);
      onEvent(event);
    };
    socket.onerror = () => socket?.close();
    socket.onclose = () => {
      onState("reconnecting");
      if (!stopped) window.setTimeout(open, Math.min(retry *= 2, 10_000));
    };
  };
  open();
  return () => { stopped = true; socket?.close(); };
}
