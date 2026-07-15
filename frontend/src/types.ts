export type MeetingStatus = "preparing" | "live" | "paused" | "ended" | "error";
export interface Meeting {
  id: string;
  title: string;
  browser: string;
  tab_id: number | null;
  status: MeetingStatus;
  transcription_provider: "openai";
  transcription_model: string;
  started_at: string;
  ended_at: string | null;
  event_count: number;
  last_error: string | null;
}

export interface TranscriptEvent {
  utterance_id: string;
  source_id: string;
  text: string;
  start_ms: number;
  end_ms: number;
  revision: number;
  stability: "draft" | "final";
  detected_language?: string | null;
}

export interface OpenAIStatus {
  configured: boolean;
  active_streams: number;
  transcription_model: string;
  intelligence_model: string;
  status: "ready" | "missing-key" | "degraded";
  message: string;
}

export interface Bootstrap {
  meetings: Meeting[];
  openai: OpenAIStatus;
  extension_audio_url: string;
}

export interface DashboardEvent {
  sequence: number;
  event_type: string;
  meeting_id: string | null;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface IntelligenceNotes {
  summary: string;
  key_points: string[];
  decisions: string[];
  action_items: string[];
  technologies: { name: string; explanation: string }[];
  suggested_response: string | null;
  suggestion_confident: boolean;
}
