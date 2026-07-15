import type { Bootstrap, DashboardEvent, TranscriptEvent } from "./types";

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
  stop: (meetingId: string) => json<{ ok: boolean }>(`/api/v1/meetings/${meetingId}/stop`, { method: "POST" }),
  pairExtension: () => json<{ audio_url: string; token: string }>("/api/v1/extension/pairing", { method: "POST" }),
  saveOpenAIKey: (apiKey: string) => json("/api/v1/settings/openai", { method: "PUT", body: JSON.stringify({ api_key: apiKey }) }),
  openTranscriptFolder: () => json("/api/v1/transcripts/open-folder", { method: "POST" })
  ,ask: (meetingId: string, question: string) => json<{ answer: string }>(`/api/v1/meetings/${meetingId}/ask`, { method: "POST", body: JSON.stringify({ question }) })
};

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
