export type MeetingStatus = "preparing" | "live" | "paused" | "ended" | "error";
export interface Meeting {
  id: string;
  title: string;
  browser: string;
  tab_id: number | null;
  status: MeetingStatus;
  transcription_provider: "openai" | "local";
  provider_reason: string | null;
  fallback_active: boolean;
  compute_device: "cloud" | "cuda" | "metal" | "cpu";
  openai_audio_seconds: number;
  estimated_cost_usd: number;
  transcription_delay_seconds: number[] | null;
  measured_transcription_lag_seconds: number | null;
  intelligence_delay_seconds: number[] | null;
  transcription_model: string;
  capture_category: "meeting" | "media" | "other" | "unsupported";
  site_domain: string;
  service_label: string;
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
  browser_audio_token: string;
  provider_mode: "auto" | "cloud" | "local";
  pricing: Pricing;
  usage: Usage;
  local_model: LocalModelStatus;
}

export interface Pricing { model: string; rate_per_minute_usd: number; rate_per_hour_usd: number; source_url: string; checked_at: string; stale: boolean; }
export interface Usage { meeting_id?: string | null; session_seconds: number; today_seconds: number; month_seconds: number; session_cost_usd: number; today_cost_usd: number; month_cost_usd: number; estimate_only?: boolean; organization?: Record<string, unknown>; }
export interface LocalModelStatus { state: string; progress: number; message: string; model_path?: string | null; runtime_path?: string | null; license_url: string; compute_device: string; transcription_ready: boolean; intelligence_ready: boolean; error?: string | null; checksum_sha256?: string | null; recommended_max_tabs?: number; transcription_delay_seconds?: number[]; capability?: Record<string, unknown>; }

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

export interface UploadResult {
  file_name: string;
  provider: "cloud" | "local";
  fallback_reason: string | null;
  watched_names: string[];
  transcript: string;
  segments: {start_seconds: number; end_seconds: number; text: string}[];
  mentions: {start_seconds: number; end_seconds: number; text: string}[];
  notes: IntelligenceNotes;
  saved_path: string;
}
