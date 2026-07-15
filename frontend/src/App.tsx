import { useEffect, useMemo, useState } from "react";
import { api, connectEvents, uploadMedia } from "./api";
import { startBrowserCapture, stopBrowserCapture } from "./capture";
import type { DashboardEvent, IntelligenceNotes, LocalModelStatus, Meeting, OpenAIStatus, Pricing, TranscriptEvent, UploadResult, Usage } from "./types";

const WATCH_NAMES = ["vladimir", "vlad"];
const exactMention = (text: string) => WATCH_NAMES.some(name => new RegExp(`(^|[^\\p{L}\\p{N}_])${name}([^\\p{L}\\p{N}_]|$)`, "iu").test(text));
const time = (ms: number) => `${String(Math.floor(ms / 60000)).padStart(2, "0")}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;

export default function App() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [transcripts, setTranscripts] = useState<Record<string, Record<string, TranscriptEvent>>>({});
  const [openai, setOpenAI] = useState<OpenAIStatus | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [adminKey, setAdminKey] = useState("");
  const [pricing, setPricing] = useState<Pricing | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [localModel, setLocalModel] = useState<LocalModelStatus | null>(null);
  const [licenseAccepted, setLicenseAccepted] = useState(false);
  const [connection, setConnection] = useState("connecting");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [notes, setNotes] = useState<Record<string, IntelligenceNotes>>({});
  const [intelligenceStatus, setIntelligenceStatus] = useState<Record<string, string>>({});
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [addingSource, setAddingSource] = useState(false);
  const [browserAudioToken, setBrowserAudioToken] = useState("");
  const [providerMode, setProviderMode] = useState<"auto" | "cloud" | "local">("auto");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadNames, setUploadNames] = useState("Vlad, Vladimir");
  const [uploadProvider, setUploadProvider] = useState<"auto" | "cloud" | "local">("auto");
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);
  const selected = meetings.find(meeting => meeting.id === selectedId) || meetings[0];
  const events = useMemo(() => selected ? Object.values(transcripts[selected.id] || {}).sort((a, b) => a.start_ms - b.start_ms) : [], [selected, transcripts]);
  const latestMention = [...events].reverse().find(event => event.stability === "final" && exactMention(event.text));

  useEffect(() => {
    let disconnect = () => {};
    api.bootstrap().then(snapshot => {
      setMeetings(snapshot.meetings); setOpenAI(snapshot.openai); setPricing(snapshot.pricing); setUsage(snapshot.usage); setLocalModel(snapshot.local_model);
      setBrowserAudioToken(snapshot.browser_audio_token);
      setProviderMode(snapshot.provider_mode);
      api.pricing(true).then(setPricing).catch(() => {});
      setSelectedId(snapshot.meetings[0]?.id || null);
      disconnect = connectEvents(0, handleEvent, setConnection);
    }).catch(reason => setError(String(reason)));
    return () => disconnect();
  }, []);
  useEffect(() => {
    const reportCaptureError = (event: Event) => setError((event as CustomEvent<string>).detail);
    window.addEventListener("dalistener:capture-error", reportCaptureError);
    return () => window.removeEventListener("dalistener:capture-error", reportCaptureError);
  }, []);
  useEffect(() => {
    if (!selectedId || transcripts[selectedId]) return;
    api.transcript(selectedId).then(rows => setTranscripts(previous => ({...previous, [selectedId]: Object.fromEntries(rows.map(row => [row.utterance_id, row]))}))).catch(reason => setError(String(reason)));
    api.notes(selectedId).then(saved => {
      if (Object.keys(saved).length) setNotes(previous => ({...previous, [selectedId]: saved as unknown as IntelligenceNotes}));
    }).catch(reason => setError(String(reason)));
  }, [selectedId, transcripts]);

  function handleEvent(event: DashboardEvent) {
    if (event.event_type === "meeting.updated") {
      const meeting = event.payload as unknown as Meeting;
      setMeetings(previous => [meeting, ...previous.filter(item => item.id !== meeting.id)].sort((a, b) => b.started_at.localeCompare(a.started_at)));
      setSelectedId(current => current || meeting.id);
    } else if (event.event_type === "transcript.upserted" && event.meeting_id) {
      const transcript = event.payload as unknown as TranscriptEvent;
      setTranscripts(previous => ({...previous, [event.meeting_id!]: {...(previous[event.meeting_id!] || {}), [transcript.utterance_id]: transcript}}));
    } else if (event.event_type === "openai.updated") setOpenAI(event.payload as unknown as OpenAIStatus);
    else if (event.event_type === "intelligence.updated" && event.meeting_id) setNotes(previous => ({...previous, [event.meeting_id!]: event.payload as unknown as IntelligenceNotes}));
    else if (event.event_type === "intelligence.status" && event.meeting_id) setIntelligenceStatus(previous => ({...previous, [event.meeting_id!]: String(event.payload.message || event.payload.status)}));
    else if (event.event_type === "local-model.updated") setLocalModel(event.payload as unknown as LocalModelStatus);
    else if (event.event_type === "intelligence.error") setError(String(event.payload.message));
    else if (event.event_type === "meeting.saved") setNotice(`Transcript saved: ${String(event.payload.transcript_path)}`);
  }
  async function saveKey() {
    try { setOpenAI(await api.saveOpenAIKey(apiKey) as OpenAIStatus); setApiKey(""); setNotice("OpenAI key stored in the operating-system credential store."); }
    catch (reason) { setError(String(reason)); }
  }
  async function saveAdminKey() {
    try { await api.saveOpenAIAdminKey(adminKey); setAdminKey(""); setNotice("OpenAI Admin key stored securely."); setUsage(await api.usage(selectedId || undefined, true)); }
    catch (reason) { setError(String(reason)); }
  }
  async function prepareLocal() {
    try { setLocalModel(await api.prepareLocal(licenseAccepted)); setNotice("Local fallback preparation started."); }
    catch (reason) { setError(String(reason)); }
  }
  async function chooseProviderMode(mode: "auto" | "cloud" | "local") {
    try {
      await api.saveProviderMode(mode);
      setProviderMode(mode);
      setNotice(`${mode[0].toUpperCase()}${mode.slice(1)} mode selected. New captures will use this policy.`);
    } catch (reason) { setError(String(reason)); }
  }
  useEffect(() => {
    const timer = window.setInterval(() => {
      api.usage(selectedId || undefined).then(setUsage).catch(() => {});
      if (localModel?.state === "preparing") api.localModel().then(setLocalModel).catch(() => {});
    }, 5000);
    return () => window.clearInterval(timer);
  }, [selectedId, localModel?.state]);
  async function addSource() {
    setError(""); setAddingSource(true);
    try {
      if (!browserAudioToken) throw new Error("The local audio bridge is not authenticated. Reopen DaListener using run.bat.");
      const capture = await startBrowserCapture(browserAudioToken);
      setSelectedId(capture.meetingId);
      setNotice(`Capturing “${capture.title}”. Keep Chromium's sharing indicator active.`);
    } catch (reason) {
      if (!String(reason).includes("Capture cancelled")) setError(String(reason));
    } finally { setAddingSource(false); }
  }
  async function stopCapture(meetingId: string) {
    if (!stopBrowserCapture(meetingId)) await api.stop(meetingId);
  }
  async function ask() {
    if (!selected || !question.trim()) return;
    try { setAnswer((await api.ask(selected.id, question)).answer); } catch (reason) { setError(String(reason)); }
  }
  async function summarizeNow() {
    if (!selected) return;
    try {
      setIntelligenceStatus(previous => ({...previous, [selected.id]: "Generating grounded summary and action items…"}));
      const generated = await api.summarize(selected.id) as IntelligenceNotes;
      setNotes(previous => ({...previous, [selected.id]: generated}));
    } catch (reason) { setError(String(reason)); }
  }
  async function transcribeUpload() {
    if (!uploadFile) return;
    setError(""); setUploading(true); setUploadResult(null);
    try {
      const result = await uploadMedia(uploadFile, uploadNames, uploadProvider);
      setUploadResult(result); setNotice(`Upload transcript saved: ${result.saved_path}`);
    } catch (reason) { setError(String(reason)); }
    finally { setUploading(false); }
  }
  return <>
    <header className="topbar"><div><h1>DaListener Live Copilot</h1><p>Share independent Chromium tabs directly—no extension required.</p></div><div className="header-actions"><span className={`connection ${connection}`}>{connection}</span><button onClick={() => api.openTranscriptFolder()}>Open transcripts</button><button onClick={async () => { if (window.confirm("Stop DaListener and all active captures?")) { await api.stopApplication(); window.close(); } }}>Stop DaListener</button><button className="primary-action" onClick={addSource} disabled={addingSource}>{addingSource ? "Waiting for picker…" : "+ Add audio source"}</button></div></header>
    <main className="page">
      {error && <div className="error-banner">{error}</div>}{notice && <div className="notice-banner">{notice}</div>}
      {openai && !openai.configured && <section className="panel setup"><span className="eyebrow">ONE-TIME SETUP</span><h2>Connect OpenAI</h2><p>The key stays in the local Python bridge and is stored by Windows Credential Manager or macOS Keychain. It is never exposed to dashboard JavaScript.</p><div className="key-row"><input type="password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder="sk-…" /><button onClick={saveKey} disabled={apiKey.length < 20}>Save securely</button></div></section>}
      <section className="disclosure-grid">
        <article className="panel"><span className="eyebrow">PROVIDER MODE</span><h2>{providerMode === "local" ? "Local only" : providerMode === "cloud" ? "Cloud only" : "Automatic failover"}</h2><div className="mode-toggle">{(["local", "cloud", "auto"] as const).map(mode => <button key={mode} className={providerMode === mode ? "active" : ""} onClick={() => chooseProviderMode(mode)}>{mode === "auto" ? "Auto" : mode === "cloud" ? "Cloud" : "Local"}</button>)}</div><p className="muted">{providerMode === "local" ? "Bypasses OpenAI for transcription, notes, and Q&A." : providerMode === "cloud" ? "Uses OpenAI only and never starts local fallback." : "Attempts OpenAI first and switches failed work to prepared local models."}</p></article>
        <article className="panel"><span className="eyebrow">OPENAI BILLING</span><h2>{pricing ? `$${pricing.rate_per_minute_usd.toFixed(3)}/minute` : "Loading price…"}</h2><p>{pricing && `$${pricing.rate_per_hour_usd.toFixed(2)}/hour for each active tab`}</p><p className="muted">Today: ${usage?.today_cost_usd.toFixed(4) || "0.0000"} · Month: ${usage?.month_cost_usd.toFixed(4) || "0.0000"}</p>{pricing && <a href={pricing.source_url} target="_blank" rel="noreferrer">Official model price{pricing.stale ? " · cached/stale" : ""}</a>}<div className="key-row"><input type="password" value={adminKey} onChange={event => setAdminKey(event.target.value)} placeholder="Optional organization Admin key" /><button onClick={saveAdminKey} disabled={adminKey.length < 20}>Account totals</button></div></article>
        <article className="panel"><span className="eyebrow">FREE LOCAL FALLBACK</span><h2>{localModel?.state || "checking"}</h2><p>{localModel?.message}</p><p className="muted">{localModel?.compute_device?.toUpperCase()} · recommended maximum {localModel?.recommended_max_tabs || 1} local tab(s) · English only</p>{localModel?.state === "preparing" && <progress max={1} value={localModel.progress} />}{!localModel?.intelligence_ready && localModel?.state !== "preparing" && <><label className="license-row"><input type="checkbox" checked={licenseAccepted} onChange={event => setLicenseAccepted(event.target.checked)} /> I accept the <a href={localModel?.license_url} target="_blank" rel="noreferrer">LFM license</a></label><button onClick={prepareLocal} disabled={!licenseAccepted}>{localModel?.transcription_ready ? "Finish local intelligence setup" : "Prepare local fallback"}</button></>}{localModel?.state === "preparing" && <button className="secondary-action" onClick={() => api.cancelLocal()}>Cancel download</button>}</article>
      </section>
      <section className="panel upload-panel"><div className="section-heading"><div><span className="eyebrow">FILE TRANSCRIPTION</span><h2>Upload audio or video</h2><p className="muted">Decode locally, transcribe with the selected provider, highlight watched-name mentions, and create grounded notes.</p></div></div><div className="upload-controls"><input type="file" accept="audio/*,video/*" onChange={event => setUploadFile(event.target.files?.[0] || null)} /><input value={uploadNames} onChange={event => setUploadNames(event.target.value)} placeholder="Watched names, comma separated" aria-label="Watched names" /><select value={uploadProvider} onChange={event => setUploadProvider(event.target.value as "auto" | "cloud" | "local")}><option value="auto">Auto provider</option><option value="cloud">Cloud</option><option value="local">Local</option></select><button className="primary-action" onClick={transcribeUpload} disabled={!uploadFile || uploading}>{uploading ? "Transcribing and summarizing…" : "Transcribe file"}</button></div>{uploadResult && <div className="upload-results"><div className="name-alert"><strong>{uploadResult.mentions.length} watched-name mention(s)</strong><span>{uploadResult.watched_names.join(", ")} · {uploadResult.provider}</span></div><div className="dashboard-grid"><article><h3>Summary</h3><p>{uploadResult.notes.summary}</p><h3>Action items</h3>{uploadResult.notes.action_items.length ? <ul>{uploadResult.notes.action_items.map(item => <li key={item}>{item}</li>)}</ul> : <p className="muted">None detected.</p>}</article><article><h3>Mentions</h3>{uploadResult.mentions.map((item, index) => <p key={`${item.start_seconds}-${index}`}><b>{item.start_seconds.toFixed(1)}s:</b> {item.text}</p>)}<details><summary>Full timestamped transcript</summary><pre>{uploadResult.transcript}</pre></details></article></div></div>}</section>
      <section><div className="section-heading"><div><span className="eyebrow">NATIVE TAB SHARING</span><h2>Captured browser tabs</h2><p className="muted">Choose <b>Add audio source</b>, select a Chromium tab, and enable <b>Share tab audio</b>. Repeat for every meeting.</p></div>{openai && <div className={openai.configured ? "capacity" : "capacity warning"}>{openai.active_streams} active · {openai.transcription_model} · {openai.status}</div>}</div>
        <div className="meeting-grid">{meetings.map(meeting => <button className={`meeting-card ${selected?.id === meeting.id ? "active" : ""}`} key={meeting.id} onClick={() => setSelectedId(meeting.id)}><div><span className="eyebrow">{meeting.service_label} · {meeting.capture_category}</span><span className={`badge ${meeting.status}`}>{meeting.status}</span></div><h3>{meeting.title}</h3><p>{meeting.site_domain} · {meeting.transcription_provider === "local" ? `Local ${meeting.compute_device.toUpperCase()}` : "OpenAI"} · {meeting.transcription_model}</p><p className="muted">{meeting.transcription_provider === "openai" ? `$${meeting.estimated_cost_usd.toFixed(4)} estimated` : "No API transcription charge"}{meeting.measured_transcription_lag_seconds != null ? ` · ${meeting.measured_transcription_lag_seconds.toFixed(1)}s measured backlog` : ""}{meeting.provider_reason ? ` · ${meeting.provider_reason}` : ""}</p>{meeting.last_error && <p className="error-text">{meeting.last_error}</p>}</button>)}{!meetings.length && <div className="empty-card"><h3>No shared tabs</h3><p>Select <b>Add audio source</b>, choose a Zoom, Meet, Teams, Webex, YouTube, or other Chromium tab, and check <b>Share tab audio</b>.</p></div>}</div>
      </section>
      {selected && <section className="workspace"><div className="workspace-header"><div><span className="eyebrow">NOW VIEWING</span><h2>{selected.title}</h2><p>{selected.status} · {selected.event_count} finalized utterances</p></div>{selected.status !== "ended" && <button className="danger" onClick={() => stopCapture(selected.id)}>Stop capture</button>}</div>
        {latestMention && <div className="name-alert"><strong>⚠ Your name was mentioned</strong><span>{latestMention.text}</span></div>}
        <div className="dashboard-grid"><article className="panel transcript-panel"><div className="panel-header"><div><span className="eyebrow">LIVE</span><h3>Current discussion</h3></div><span className={`badge ${selected.status}`}>● {selected.status}</span></div><div className="transcript-list">{events.map(event => <div className={`transcript-item ${exactMention(event.text) ? "mention" : ""} ${event.stability}`} key={event.utterance_id}><div className="avatar">S</div><div><div className="transcript-meta"><b>{selected.service_label}</b><span>{time(event.start_ms)}</span><span>{event.stability}</span></div><p>{event.text}</p></div></div>)}{!events.length && <p className="muted">Waiting for speech from this tab…</p>}</div></article>
          <div className="right-column"><article className="panel"><span className="eyebrow">{selected.transcription_provider === "local" ? "LOCAL LFM NOTES" : "OPENAI NOTES"}</span><h3>30-second summary</h3><p className={notes[selected.id] ? "" : "muted"}>{notes[selected.id]?.summary || intelligenceStatus[selected.id] || "Waiting for finalized speech."}</p><button className="secondary-action" onClick={summarizeNow}>Generate now</button>{notes[selected.id]?.technologies.map(item => <p key={item.name}><b>{item.name}:</b> {item.explanation}</p>)}</article><article className="panel"><span className="eyebrow">READY TO SAY</span><h3>Grounded response</h3><textarea rows={7} readOnly value={notes[selected.id]?.suggestion_confident ? notes[selected.id]?.suggested_response || "" : ""} placeholder="A response appears only when the transcript provides enough context." /></article><article className="panel"><span className="eyebrow">FOLLOW-UP</span><h3>Action items</h3>{notes[selected.id]?.action_items.length ? <ul>{notes[selected.id].action_items.map(item => <li key={item}>{item}</li>)}</ul> : <p className="muted">{intelligenceStatus[selected.id] || "No action items detected yet."}</p>}</article><article className="panel"><span className="eyebrow">ASK</span><h3>Question this meeting</h3><textarea rows={3} value={question} onChange={event => setQuestion(event.target.value)} placeholder="What did Arjun just say?" /><button onClick={ask}>Ask copilot</button>{answer && <p>{answer}</p>}</article></div>
        </div></section>}
    </main>
  </>;
}
