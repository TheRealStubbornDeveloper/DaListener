import { useEffect, useMemo, useState } from "react";
import { api, connectEvents } from "./api";
import type { CaptureWarningPreferences, DashboardEvent, IntelligenceNotes, Meeting, OpenAIStatus, TranscriptEvent } from "./types";

const WATCH_NAMES = ["vladimir", "vlad"];
const exactMention = (text: string) => WATCH_NAMES.some(name => new RegExp(`(^|[^\\p{L}\\p{N}_])${name}([^\\p{L}\\p{N}_]|$)`, "iu").test(text));
const time = (ms: number) => `${String(Math.floor(ms / 60000)).padStart(2, "0")}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;

export default function App() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [transcripts, setTranscripts] = useState<Record<string, Record<string, TranscriptEvent>>>({});
  const [openai, setOpenAI] = useState<OpenAIStatus | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [connection, setConnection] = useState("connecting");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [notes, setNotes] = useState<Record<string, IntelligenceNotes>>({});
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [captureWarnings, setCaptureWarnings] = useState<CaptureWarningPreferences>({suppressed_domains: []});
  const selected = meetings.find(meeting => meeting.id === selectedId) || meetings[0];
  const events = useMemo(() => selected ? Object.values(transcripts[selected.id] || {}).sort((a, b) => a.start_ms - b.start_ms) : [], [selected, transcripts]);
  const latestMention = [...events].reverse().find(event => event.stability === "final" && exactMention(event.text));

  useEffect(() => {
    let disconnect = () => {};
    api.bootstrap().then(snapshot => {
      setMeetings(snapshot.meetings); setOpenAI(snapshot.openai);
      setSelectedId(snapshot.meetings[0]?.id || null);
      api.captureWarnings().then(setCaptureWarnings).catch(reason => setError(String(reason)));
      disconnect = connectEvents(0, handleEvent, setConnection);
    }).catch(reason => setError(String(reason)));
    return () => disconnect();
  }, []);
  useEffect(() => {
    if (!selectedId || transcripts[selectedId]) return;
    api.transcript(selectedId).then(rows => setTranscripts(previous => ({...previous, [selectedId]: Object.fromEntries(rows.map(row => [row.utterance_id, row]))}))).catch(reason => setError(String(reason)));
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
    else if (event.event_type === "intelligence.error") setError(String(event.payload.message));
    else if (event.event_type === "meeting.saved") setNotice(`Transcript saved: ${String(event.payload.transcript_path)}`);
  }
  async function saveKey() {
    try { setOpenAI(await api.saveOpenAIKey(apiKey) as OpenAIStatus); setApiKey(""); setNotice("OpenAI key stored in the operating-system credential store."); }
    catch (reason) { setError(String(reason)); }
  }
  async function copyPairing() {
    const value = await api.pairExtension(); await navigator.clipboard.writeText(JSON.stringify(value));
    setNotice("Extension pairing copied. Paste it into the DaListener extension options.");
  }
  async function ask() {
    if (!selected || !question.trim()) return;
    try { setAnswer((await api.ask(selected.id, question)).answer); } catch (reason) { setError(String(reason)); }
  }
  async function openExtensionFolder() {
    try { const result = await api.openExtensionFolder(); setNotice(`Extension folder opened: ${result.path}`); }
    catch (reason) { setError(String(reason)); }
  }

  return <>
    <header className="topbar"><div><h1>DaListener Live Copilot</h1><p>Independent OpenAI transcription for meetings, media, and other Chromium tabs.</p></div><div className="header-actions"><span className={`connection ${connection}`}>{connection}</span><button onClick={() => api.openTranscriptFolder()}>Open transcripts</button><button onClick={openExtensionFolder}>Open extension</button><button onClick={copyPairing} disabled={!openai?.configured}>Pair extension</button></div></header>
    <main className="page">
      {error && <div className="error-banner">{error}</div>}{notice && <div className="pairing">{notice}</div>}
      {openai && !openai.configured && <section className="panel setup"><span className="eyebrow">ONE-TIME SETUP</span><h2>Connect OpenAI</h2><p>The key stays in the local Python bridge and is stored by Windows Credential Manager or macOS Keychain. It is never sent to this dashboard or the extension again.</p><div className="key-row"><input type="password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder="sk-…" /><button onClick={saveKey} disabled={apiKey.length < 20}>Save securely</button></div></section>}
      <section><div className="section-heading"><div><span className="eyebrow">CAPTURES</span><h2>Captured browser tabs</h2></div>{openai && <div className={openai.configured ? "capacity" : "capacity warning"}>{openai.active_streams} active · {openai.transcription_model} · {openai.status}</div>}</div>
        <div className="meeting-grid">{meetings.map(meeting => <button className={`meeting-card ${selected?.id === meeting.id ? "active" : ""}`} key={meeting.id} onClick={() => setSelectedId(meeting.id)}><div><span className="eyebrow">{meeting.service_label} · {meeting.capture_category}</span><span className={`badge ${meeting.status}`}>{meeting.status}</span></div><h3>{meeting.title}</h3><p>{meeting.site_domain} · OpenAI · {meeting.transcription_model}</p>{meeting.last_error && <p className="error-text">{meeting.last_error}</p>}</button>)}{!meetings.length && <div className="empty-card"><h3>No captured tabs</h3><p>Configure OpenAI, pair the extension, then click its icon in a meeting, YouTube, or other audio tab.</p></div>}</div>
      </section>
      {selected && <section className="workspace"><div className="workspace-header"><div><span className="eyebrow">NOW VIEWING</span><h2>{selected.title}</h2><p>{selected.status} · {selected.event_count} finalized utterances</p></div>{selected.status !== "ended" && <button className="danger" onClick={() => api.stop(selected.id)}>Stop capture</button>}</div>
        {latestMention && <div className="name-alert"><strong>⚠ Your name was mentioned</strong><span>{latestMention.text}</span></div>}
        <div className="dashboard-grid"><article className="panel transcript-panel"><div className="panel-header"><div><span className="eyebrow">LIVE</span><h3>Current discussion</h3></div><span className={`badge ${selected.status}`}>● {selected.status}</span></div><div className="transcript-list">{events.map(event => <div className={`transcript-item ${exactMention(event.text) ? "mention" : ""} ${event.stability}`} key={event.utterance_id}><div className="avatar">S</div><div><div className="transcript-meta"><b>{selected.service_label}</b><span>{time(event.start_ms)}</span><span>{event.stability}</span></div><p>{event.text}</p></div></div>)}{!events.length && <p className="muted">Waiting for speech from this tab…</p>}</div></article>
          <div className="right-column"><article className="panel"><span className="eyebrow">OPENAI NOTES</span><h3>30-second summary</h3><p className={notes[selected.id] ? "" : "muted"}>{notes[selected.id]?.summary || "Notes appear after finalized speech and refresh every 30 seconds."}</p>{notes[selected.id]?.technologies.map(item => <p key={item.name}><b>{item.name}:</b> {item.explanation}</p>)}</article><article className="panel"><span className="eyebrow">READY TO SAY</span><h3>Grounded response</h3><textarea rows={7} readOnly value={notes[selected.id]?.suggestion_confident ? notes[selected.id]?.suggested_response || "" : ""} placeholder="A response appears only when the transcript provides enough context." /></article><article className="panel"><span className="eyebrow">FOLLOW-UP</span><h3>Action items</h3>{notes[selected.id]?.action_items.length ? <ul>{notes[selected.id].action_items.map(item => <li key={item}>{item}</li>)}</ul> : <p className="muted">No action items detected yet.</p>}</article><article className="panel"><span className="eyebrow">ASK</span><h3>Question this meeting</h3><textarea rows={3} value={question} onChange={event => setQuestion(event.target.value)} placeholder="What did Arjun just say?" /><button onClick={ask}>Ask OpenAI</button>{answer && <p>{answer}</p>}</article></div>
        </div></section>}
      <section className="panel preferences-panel"><span className="eyebrow">CAPTURE SETTINGS</span><h2>Non-meeting reminders</h2><p className="muted">DaListener warns before sending audio from YouTube or another non-meeting website to OpenAI. Sites you chose not to be reminded about appear here.</p>{captureWarnings.suppressed_domains.length ? <div className="domain-list">{captureWarnings.suppressed_domains.map(domain => <div key={domain}><span>{domain}</span><button onClick={() => api.removeCaptureWarning(domain).then(setCaptureWarnings)}>Show warning again</button></div>)}</div> : <p>No website reminders are suppressed.</p>}<button className="secondary-action" disabled={!captureWarnings.suppressed_domains.length} onClick={() => api.resetCaptureWarnings().then(setCaptureWarnings)}>Reset all non-meeting warnings</button></section>
    </main>
  </>;
}
