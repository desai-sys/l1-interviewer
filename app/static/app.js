"use strict";

const $app = document.getElementById("app");
const BUSY = ["analyzing", "researching_questions", "generating_questions", "transcribing", "evaluating"];
let cfg = { access_code_required: false, max_answer_seconds: 180 };
let pollTimer = null;
let recorder = null; // active recording state

// ---------- helpers ----------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const list = (items) => (items && items.length ? `<ul class="tight">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : `<p class="muted small">None</p>`);
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const tone = (score) => (score >= 65 ? "good" : score >= 50 ? "warn" : "bad");
const accessCode = () => { try { return localStorage.getItem("l1_access_code") || ""; } catch { return ""; } };

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (accessCode()) headers["X-Access-Code"] = accessCode();
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { msg = (await res.json()).detail || msg; } catch {}
    const err = new Error(msg); err.status = res.status; throw err;
  }
  if (opts.raw) return res;
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
}

function dial(value, color) {
  return `<div class="dial" style="--v:${value};--dial-color:var(--${color})"><span>${value}</span></div>`;
}

// ---------- routing ----------
window.addEventListener("hashchange", route);
(async () => { try { cfg = await api("/api/config"); } catch {} route(); })();

function route() {
  clearTimeout(pollTimer);
  endInterviewMode();
  speechSynthesis?.cancel();
  const m = location.hash.match(/^#\/s\/([0-9a-f-]{36})/);
  if (m) loadSession(m[1]); else renderUpload();
}

// ---------- upload ----------
function renderUpload(error = "") {
  $app.innerHTML = `
    <div class="card">
      <h1>Check your fit, then take a voice interview</h1>
      <p class="muted">Upload your resume and the job description. You'll get an evidence-based fit score,
      then answer ${cfg.num_questions || 5} interview questions <b>by voice</b>. Your answers are recorded, transcribed and graded
      strictly against a rubric written before you speak.</p>
      ${error ? `<div class="error">${esc(error)}</div>` : ""}
      <form id="f">
        ${cfg.access_code_required ? `<label for="code">Access code</label><input id="code" type="password" value="${esc(accessCode())}" required>` : ""}
        <label for="resume">Resume (PDF, DOCX or TXT)</label>
        <input id="resume" name="resume" type="file" accept=".pdf,.docx,.txt" required>
        <label for="jd">Job description</label>
        <textarea id="jd" name="jd_text" placeholder="Paste the full job description here..."></textarea>
        <p class="small muted">…or upload it as a file: <input name="jd_file" type="file" accept=".pdf,.docx,.txt" style="width:auto;padding:4px"></p>
        <div class="row" style="margin-top:14px"><button class="primary" id="go">Analyze my profile</button></div>
      </form>
    </div>
    <p class="small muted">Recordings, transcripts and reports are stored in this deployment's private S3 bucket.</p>`;
  document.getElementById("f").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = document.getElementById("code");
    if (code) { try { localStorage.setItem("l1_access_code", code.value.trim()); } catch {} }
    const btn = document.getElementById("go");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Uploading…`;
    try {
      const fd = new FormData(e.target);
      const { id } = await api("/api/sessions", { method: "POST", body: fd });
      location.hash = `#/s/${id}`;
    } catch (err) { renderUpload(err.message); }
  });
}

// ---------- session ----------
async function loadSession(id) {
  let s;
  try { s = await api(`/api/sessions/${id}`); }
  catch (err) { $app.innerHTML = `<div class="card"><div class="error">${esc(err.message)}</div><a class="btn" href="#/">Start over</a></div>`; return; }
  renderSession(s);
  if (BUSY.includes(s.status)) pollTimer = setTimeout(() => loadSession(id), 3000);
}

function renderSession(s) {
  const parts = [];
  if (BUSY.includes(s.status) && !s.fit) parts.push(progressCard(s));
  if (s.status === "failed") parts.push(failedCard(s));
  if (s.fit) parts.push(fitReport(s.fit, s.status === "completed"));
  if (["researching_questions", "generating_questions"].includes(s.status) && s.fit) parts.push(progressCard(s));
  if (["transcribing", "evaluating"].includes(s.status)) parts.push(progressCard(s));
  if (s.status === "completed") parts.push(evaluationReport(s));
  const canAnswer = ["ready", "interviewing"].includes(s.status);
  if (canAnswer) parts.unshift(`<div id="interview"></div>`);
  $app.innerHTML = parts.join("");
  if (canAnswer) renderInterview(s);
  bindRetry(s);
}

function progressCard(s) {
  const order = ["analyzing", "researching_questions", "generating_questions", "transcribing", "evaluating"];
  const labels = { analyzing: "Screening resume against the job description", researching_questions: "Searching the web for real interview questions for this role",
    generating_questions: "Selecting questions and writing the grading rubric",
    transcribing: "Transcribing your recorded answers", evaluating: "Grading each answer and verifying evidence against the transcript" };
  const phase = order.indexOf(s.status) < 3 ? order.slice(0, 3) : order.slice(3);
  const cur = order.indexOf(s.status);
  return `<div class="card"><h2>Working on it…</h2><ul class="steps">${phase.map((p) => {
    const i = order.indexOf(p);
    const st = i < cur ? "done" : i === cur ? "cur" : "todo";
    return `<li class="${st}">${st === "done" ? "✓" : st === "cur" ? '<span class="spinner"></span>' : "○"} ${labels[p]}</li>`;
  }).join("")}</ul><p class="small muted" style="margin-top:10px">This usually takes 1–3 minutes. You can keep this page open or come back to this link later.</p></div>`;
}

function failedCard(s) {
  return `<div class="card"><h2>Something went wrong</h2><div class="error">${esc(s.error?.message || "Unknown error")}</div>
    <button class="primary" id="retry">Retry ${s.error?.stage === "screen" ? "analysis" : "evaluation"}</button></div>`;
}

function bindRetry(s) {
  const b = document.getElementById("retry");
  if (!b) return;
  b.onclick = async () => {
    b.disabled = true;
    try { await api(`/api/sessions/${s.id}/retry`, { method: "POST" }); loadSession(s.id); }
    catch (err) { alert(err.message); b.disabled = false; }
  };
}

// ---------- fit report ----------
const STATUS_PILL = { met: ["good", "Met"], partially_met: ["warn", "Partial"], not_met: ["bad", "Not met"], unclear: ["neutral", "Unclear"] };

function fitReport(fit, collapsed) {
  const sc = fit.scoring;
  const dims = [["skills", "Skills", 40], ["experience", "Experience", 30], ["domain", "Domain", 15], ["education_certifications", "Education & certifications", 15]];
  const body = `
    <div class="score-hero">
      ${dial(sc.overall_score, tone(sc.overall_score))}
      <div style="flex:1;min-width:240px">
        <div class="row"><span class="pill ${tone(sc.overall_score)}">${esc(sc.verdict_label)}</span><span class="muted small">for ${esc(fit.role_title)}</span></div>
        <p style="margin-top:8px">${esc(fit.candidate_summary)}</p>
        ${sc.capped_by_must_haves ? `<div class="notice small">Verdict capped at “Partial fit” because these must-have requirements are not met: ${esc(sc.unmet_must_haves.join("; "))}</div>` : ""}
      </div>
    </div>
    <h3 style="margin-top:18px">Why</h3><p>${esc(fit.overall_reasoning)}</p>
    <div class="bars">${dims.map(([k, label, w]) => {
      const d = fit.score_breakdown[k];
      return `<div><div class="bar-label"><span>${label} <span class="muted small">(${w}%)</span></span><span>${d.score}</span></div>
        <div class="bar"><i style="width:${d.score}%"></i></div><p class="small muted">${esc(d.reasoning)}</p></div>`;
    }).join("")}</div>
    <div class="grid2" style="margin-top:12px">
      <div><h3>Strengths</h3>${list(fit.strengths)}</div>
      <div><h3>Gaps</h3>${list(fit.gaps)}</div>
    </div>
    <details ${collapsed ? "" : "open"}><summary>Requirement-by-requirement evidence (${fit.requirements.length})</summary>
      <div class="table-wrap"><table><thead><tr><th>Requirement</th><th>Status</th><th>Evidence from resume</th></tr></thead><tbody>
      ${fit.requirements.map((r) => {
        const [t, l] = STATUS_PILL[r.status] || ["neutral", r.status];
        return `<tr><td><b>${esc(r.requirement)}</b><br><span class="small muted">${r.importance === "must_have" ? "Must have" : "Nice to have"}</span></td>
          <td><span class="pill ${t}">${l}</span></td><td><span class="small">${esc(r.evidence)}</span><br><span class="small muted">${esc(r.reasoning)}</span></td></tr>`;
      }).join("")}</tbody></table></div></details>
    <details><summary>How this score is calculated</summary>${list(sc.rules)}</details>`;
  return `<div class="card"><h2>Profile fit</h2>${body}</div>`;
}

// ---------- interview ----------
// Rules (also enforced by the server): one question at a time, recording starts automatically as soon as
// the question has been read, one attempt per question, hard time limit, tab switches / fullscreen exits logged.
let iv = null; // live interview state

function renderInterview(s) {
  const el = document.getElementById("interview");
  const resuming = s.status === "interviewing";
  el.innerHTML = `<div class="card">
    <h2>${resuming ? "Continue your interview" : "Voice interview"}</h2>
    <p>${s.question_count} questions (${Math.floor(s.question_count / 2)} concept &amp; foundation, then ${s.question_count - Math.floor(s.question_count / 2)} application &amp; scenario) · up to ${fmtTime(s.max_answer_seconds)} per answer · answers by voice only.</p>
    <div class="notice small"><b>Rules</b>
      <ul class="tight">
        <li>Each question is read aloud once. <b>Recording starts automatically</b> the moment it finishes — start speaking straight away.</li>
        <li>You get <b>one attempt</b> per question. No replay, no skipping, no re-recording.</li>
        <li>When the timer reaches zero the answer is submitted automatically. Press <b>Done answering</b> to submit sooner.</li>
        <li>Questions are revealed one at a time. Closing or reloading the page does not pause the clock.</li>
        <li>The interview runs in fullscreen. Leaving fullscreen or switching tabs is recorded in your report.</li>
      </ul></div>
    <p class="small muted">Use a quiet room and a working microphone. Chrome or Edge recommended.</p>
    <div id="miccheck"></div>
    <button class="primary" id="start">🎙️ ${resuming ? "Continue interview" : "Start interview"}</button></div>`;
  document.getElementById("start").onclick = () => startInterview(s);
}

async function startInterview(s) {
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
  catch { document.getElementById("miccheck").innerHTML = `<div class="error">Microphone access is required. Allow it in your browser settings and try again.</div>`; return; }
  try { await document.documentElement.requestFullscreen?.(); } catch {}
  iv = { s, stream, ctx: new AudioContext(), counters: freshCounters(), hiddenAt: null };
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("fullscreenchange", onFullscreen);
  window.addEventListener("beforeunload", onBeforeUnload);
  if (s.status === "ready" && s.intro) {
    // Untimed welcome from the interviewer, then question 1 (whose clock starts when it is served).
    document.getElementById("interview").innerHTML = `<div class="card interview">
      <div class="qcount">Interviewer</div><div class="question">${esc(s.intro)}</div>
      <p class="muted"><span class="spinner"></span> Question 1 starts right after this…</p></div>`;
    speakThen(s.intro, nextQuestion);
  } else nextQuestion();
}

const SECTION = { concept_foundation: "Concept & Foundation", application_scenario: "Application & Scenario" };

const freshCounters = () => ({ tab_switches: 0, hidden_seconds: 0, fullscreen_exits: 0 });

function onVisibility() {
  if (!iv) return;
  if (document.hidden) { iv.counters.tab_switches++; iv.hiddenAt = Date.now(); }
  else if (iv.hiddenAt) { iv.counters.hidden_seconds += (Date.now() - iv.hiddenAt) / 1000; iv.hiddenAt = null; }
}

function onFullscreen() {
  if (!iv || document.fullscreenElement) { document.getElementById("fswarn")?.remove(); return; }
  iv.counters.fullscreen_exits++;
  const card = document.querySelector(".interview");
  if (card && !document.getElementById("fswarn")) {
    card.insertAdjacentHTML("afterbegin", `<div class="error" id="fswarn">You left fullscreen — this is recorded. <button id="refs">Return to fullscreen</button></div>`);
    document.getElementById("refs").onclick = () => document.documentElement.requestFullscreen?.().catch(() => {});
  }
}

function onBeforeUnload(e) { e.preventDefault(); e.returnValue = ""; }

function endInterviewMode() {
  if (!iv) return;
  stopRecording(true);
  speechSynthesis?.cancel();
  iv.stream.getTracks().forEach((t) => t.stop());
  iv.ctx.close().catch(() => {});
  document.removeEventListener("visibilitychange", onVisibility);
  document.removeEventListener("fullscreenchange", onFullscreen);
  window.removeEventListener("beforeunload", onBeforeUnload);
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  iv = null;
}

async function nextQuestion() {
  const el = document.getElementById("interview");
  let n;
  try { n = await api(`/api/sessions/${iv.s.id}/next`, { method: "POST" }); }
  catch (err) { el.innerHTML = `<div class="card"><div class="error">${esc(err.message)}</div><button id="again">Try again</button></div>`; document.getElementById("again").onclick = nextQuestion; return; }
  if (n.done) return finishInterview(false);

  iv.q = n.question; iv.counters = freshCounters();
  const dots = Array.from({ length: n.total }, (_, i) => `<i class="${i < n.index ? "done" : i === n.index ? "cur" : ""}"></i>`).join("");
  el.innerHTML = `<div class="card interview"><div class="qdots">${dots}</div>
    <div class="qcount">Question ${n.index + 1} of ${n.total} · ${esc(SECTION[n.question.section] || n.question.type)}</div>
    <div class="question">${esc(n.question.question)}</div>
    <div class="recorder">
      <div class="mic" id="mic" aria-hidden="true">🔊</div>
      <canvas class="level" id="level" width="520" height="72"></canvas>
      <div class="timer" id="timer">${fmtTime(n.seconds_left)}</div>
      <div id="recmsg" class="muted">${n.resumed ? "Resuming — recording starts now." : "Listen to the question…"}</div>
      <div class="row" style="justify-content:center">
        <button class="primary" id="done" disabled>Done answering</button>
      </div>
      <button id="endearly" class="small" style="margin-top:18px;opacity:.8">End interview early</button>
    </div></div>`;
  document.getElementById("endearly").onclick = () => {
    if (confirm("End the interview now? This question and all remaining questions will score 0.")) finishInterview(true);
  };
  if (n.resumed || n.seconds_left <= 0) beginAnswer(n);
  else speakThen(n.question.question, () => beginAnswer(n));
}

function speakThen(text, done) {
  let fired = false;
  const go = () => { if (!fired) { fired = true; clearTimeout(safety); done(); } };
  // Safety net in case the speech engine never fires `onend` (or is unavailable).
  const safety = setTimeout(go, Math.min(45000, 2500 + text.split(/\s+/).length * 450));
  if (!("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.97; u.onend = go; u.onerror = go;
  speechSynthesis.speak(u);
}

function beep() {
  try {
    const o = iv.ctx.createOscillator(), g = iv.ctx.createGain();
    o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(iv.ctx.destination);
    o.start(); o.stop(iv.ctx.currentTime + 0.18);
  } catch {}
}

function pickMime() {
  const opts = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return opts.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

function beginAnswer(n) {
  if (!iv) return;
  const limit = Math.max(0, n.seconds_left);
  if (limit <= 0) return upload(new Blob([]), 0);
  beep();
  const mime = pickMime();
  const mr = new MediaRecorder(iv.stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const analyser = iv.ctx.createAnalyser(); analyser.fftSize = 512;
  const src = iv.ctx.createMediaStreamSource(iv.stream); src.connect(analyser);
  recorder = { mr, started: Date.now(), timer: null, raf: null, discard: false };
  mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  mr.onstop = () => {
    const secs = (Date.now() - recorder.started) / 1000;
    clearInterval(recorder.timer); cancelAnimationFrame(recorder.raf); src.disconnect();
    const discard = recorder.discard; recorder = null;
    if (!discard) upload(new Blob(chunks, { type: mr.mimeType || mime || "audio/webm" }), secs);
  };
  mr.start(1000);

  const mic = document.getElementById("mic");
  mic.classList.add("recording"); mic.textContent = "●";
  document.getElementById("recmsg").innerHTML = `<b>Recording — answer now.</b>`;
  const doneBtn = document.getElementById("done");
  doneBtn.disabled = false; doneBtn.onclick = () => stopRecording();
  const timerEl = document.getElementById("timer");
  recorder.timer = setInterval(() => {
    const left = limit - (Date.now() - recorder.started) / 1000;
    timerEl.textContent = fmtTime(Math.max(0, left));
    timerEl.style.color = left < 20 ? "var(--bad)" : "";
    if (left <= 0) stopRecording();
  }, 200);
  const canvas = document.getElementById("level"), g = canvas.getContext("2d"), buf = new Uint8Array(analyser.fftSize);
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent");
  const draw = () => {
    analyser.getByteTimeDomainData(buf);
    g.clearRect(0, 0, canvas.width, canvas.height); g.strokeStyle = accent; g.lineWidth = 3; g.beginPath();
    for (let i = 0; i < buf.length; i++) { const x = (i / buf.length) * canvas.width, y = (buf[i] / 255) * canvas.height; i ? g.lineTo(x, y) : g.moveTo(x, y); }
    g.stroke(); if (recorder) recorder.raf = requestAnimationFrame(draw);
  };
  draw();
}

function stopRecording(discard = false) {
  if (!recorder) return;
  recorder.discard = discard;
  if (recorder.mr.state !== "inactive") recorder.mr.stop();
}

async function upload(blob, secs) {
  if (!iv) return;
  if (iv.hiddenAt) { iv.counters.hidden_seconds += (Date.now() - iv.hiddenAt) / 1000; iv.hiddenAt = Date.now(); }
  const msg = document.getElementById("recmsg"), doneBtn = document.getElementById("done");
  if (doneBtn) doneBtn.disabled = true;
  const mic = document.getElementById("mic");
  if (mic) { mic.classList.remove("recording"); mic.textContent = "⏳"; }
  if (msg) msg.innerHTML = `<span class="spinner"></span> Submitting answer…`;
  const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  const fd = new FormData();
  fd.append("audio", blob.size ? blob : new Blob([new Uint8Array(1)], { type: "audio/webm" }), `${iv.q.id}.${ext}`);
  fd.append("duration_seconds", String(Math.round(secs)));
  fd.append("tab_switches", String(iv.counters.tab_switches));
  fd.append("hidden_seconds", String(Math.round(iv.counters.hidden_seconds)));
  fd.append("fullscreen_exits", String(iv.counters.fullscreen_exits));
  for (let attempt = 1; attempt <= 3; attempt++) {
    try { await api(`/api/sessions/${iv.s.id}/answers/${iv.q.id}`, { method: "POST", body: fd }); break; }
    catch (err) {
      if (err.status === 409) break; // expired / already answered: the server has decided, move on
      if (attempt === 3) { if (msg) msg.innerHTML = `<span class="error">Upload failed: ${esc(err.message)}</span>`; await new Promise((r) => setTimeout(r, 1500)); }
      else await new Promise((r) => setTimeout(r, 1500 * attempt));
    }
  }
  nextQuestion();
}

async function finishInterview(early) {
  const el = document.getElementById("interview");
  if (el) el.innerHTML = `<div class="card interview"><h2>${early ? "Interview ended" : "Interview complete"}</h2><p><span class="spinner"></span> Submitting for evaluation…</p></div>`;
  const id = iv?.s.id || location.hash.split("/").pop();
  endInterviewMode();
  try { await api(`/api/sessions/${id}/finish`, { method: "POST" }); }
  catch (err) { if (err.status !== 409) { el.innerHTML = `<div class="card"><div class="error">${esc(err.message)}</div></div>`; return; } }
  loadSession(id);
}

// ---------- evaluation report ----------
function integrityLine(ans) {
  if (ans.expired && !ans.submitted) return `<p class="small"><span class="pill bad">No answer within the time limit</span></p>`;
  const i = ans.integrity || {};
  if (i.time_to_submit_seconds == null) return "";
  const flags = [];
  if (i.tab_switches) flags.push(`${i.tab_switches} tab switch${i.tab_switches > 1 ? "es" : ""} (${Math.round(i.hidden_seconds || 0)}s away)`);
  if (i.fullscreen_exits) flags.push(`${i.fullscreen_exits} fullscreen exit${i.fullscreen_exits > 1 ? "s" : ""}`);
  return `<p class="small ${flags.length ? "" : "muted"}">Integrity: ${flags.length ? `<span class="pill warn">${esc(flags.join(" · "))}</span>` : "no tab switches or fullscreen exits"} · answered in ${fmtTime(i.time_to_submit_seconds)} incl. question reading</p>`;
}

function researchLine(s) {
  const r = s.web_research, n = s.questions.filter((q) => q.source === "web").length;
  if (!r || r.status === "off") return `<p class="small muted">Questions were generated for this role from the job description and resume (web research is off in this deployment).</p>`;
  if (r.status !== "ok") return `<p class="small muted">Web research ${r.status === "failed" ? "failed" : "found no usable questions"}, so all questions were generated from the job description and resume.</p>`;
  return `<p class="small muted">${n} of ${s.questions.length} questions were selected from ${r.questions_found} real interview questions found on the web (${esc(r.sources.slice(0, 6).join(", "))}); the rest were generated for this role.</p>`;
}

function evaluationReport(s) {
  const ev = s.evaluation, sc = ev.scoring;
  const recTone = { strong_advance: "good", advance: "good", borderline: "warn", do_not_advance: "bad" }[sc.recommendation];
  const byId = Object.fromEntries(ev.answers.map((a) => [a.question_id, a]));
  const cards = s.questions.map((q, i) => {
    const a = byId[q.id] || {}, ans = s.answers[q.id] || {};
    const t = a.score >= 7 ? "good" : a.score >= 5 ? "warn" : "bad";
    return `<div class="card qcard">
      <div class="head"><div style="flex:1"><div class="qcount">Question ${i + 1} · ${esc(SECTION[q.section] || "")} · ${esc(q.type)}</div>
        <h3 style="margin-top:4px">${esc(q.question)}</h3><p class="small muted">Assesses: ${esc(q.assesses)}${q.technology ? ` · Technology: ${esc(q.technology)}` : ""}</p>
        ${q.resume_evidence ? `<p class="small muted">From resume: “${esc(q.resume_evidence)}”${q.resume_verified === false ? ` <span class="pill warn">not found in resume</span>` : ""}</p>` : ""}
        <p class="small">${q.source === "web" && q.source_url ? `<span class="pill neutral">From the web</span> <a href="${esc(q.source_url)}" target="_blank" rel="noopener noreferrer">${esc(new URL(q.source_url).hostname)}</a>${q.source_verified ? "" : ` <span class="muted">(source unverified)</span>`}` : `<span class="pill neutral">AI-generated for this role</span>`}</p></div>
        <div class="qscore">${a.score ?? 0}<span class="muted small">/10</span><br><span class="pill ${t}">${esc((a.rating || "").replace("_", " "))}</span></div></div>
      ${ans.audio_url ? `<audio controls preload="none" src="${esc(ans.audio_url)}"></audio>` : `<p class="muted small">Not answered.</p>`}
      ${ans.transcript != null ? `<h3 style="margin-top:12px">Transcript ${ans.confidence != null ? `<span class="muted small">(transcription confidence ${Math.round(ans.confidence * 100)}%)</span>` : ""}</h3>
        <div class="transcript">${esc(ans.transcript || "[no speech detected]")}</div>` : ""}
      <h3 style="margin-top:12px">Reasoning</h3><p>${esc(a.reasoning)}</p>
      ${a.evidence_quotes?.length ? `<h3>Evidence</h3>${a.evidence_quotes.map((x) => `<blockquote>“${esc(x)}”</blockquote>`).join("")}` : ""}
      <div class="grid2" style="margin-top:10px"><div><h3>Covered</h3>${list(a.key_points_covered)}</div><div><h3>Missed</h3>${list(a.key_points_missed)}</div></div>
      ${a.red_flags_observed?.length ? `<h3>Red flags</h3>${list(a.red_flags_observed)}` : ""}
      ${a.checks?.caps?.length ? `<div class="notice small"><b>Automatic checks:</b>${list(a.checks.caps)}</div>` : ""}
      ${a.checks?.quotes_rejected?.length ? `<p class="small muted">${a.checks.quotes_rejected.length} grader quote(s) were discarded because they did not appear in the transcript${a.checks.regraded ? "; the answer was re-graded" : ""}.</p>` : ""}
      <h3>How to improve</h3><p>${esc(a.improvement_advice)}</p>
      ${integrityLine(ans)}
      ${q.evaluator_note ? `<p class="small"><b>Evaluator note:</b> ${esc(q.evaluator_note)}</p>` : ""}
      <details><summary>Grading rubric (written before the interview)</summary>
        <p><b>Excellent:</b> ${esc(q.rubric?.excellent)}</p><p><b>Adequate:</b> ${esc(q.rubric?.adequate)}</p><p><b>Poor:</b> ${esc(q.rubric?.poor)}</p>
        <b>Key points</b>${list(q.key_points)}</details>
    </div>`;
  }).join("");
  return `<div class="card"><h2>Interview evaluation</h2>
      <div class="score-hero">${dial(sc.overall_score, recTone)}
        <div style="flex:1;min-width:240px"><span class="pill ${recTone}">${esc(sc.recommendation_label)}</span>
          <p style="margin-top:8px">${esc(ev.overall_reasoning)}</p>
          ${sc.capped ? `<div class="notice small">Capped at “Borderline” because two or more answers received no credit.</div>` : ""}</div></div>
      <div class="grid2" style="margin-top:12px"><div><h3>Strengths</h3>${list(ev.strengths)}</div><div><h3>Concerns</h3>${list(ev.concerns)}</div></div>
      ${researchLine(s)}
      <details><summary>How this score is calculated</summary>${list(sc.rules)}
        <p class="small muted">Each answer is graded in a separate request against its own rubric, without the resume, so answers can't influence each other and resume claims can't inflate interview scores. Integrity signals (tab switches, fullscreen exits) are reported for the reviewer but do not change the score automatically.</p></details>
      <div class="row" style="margin-top:14px">
        <button id="dl-transcript">⬇ Full transcript (.txt)</button><button id="dl-report">⬇ Full report (.json)</button>
        <span class="spacer"></span><a class="btn" href="#/">New screening</a></div>
    </div>${cards}`;
}

document.addEventListener("click", async (e) => {
  const id = location.hash.match(/^#\/s\/([0-9a-f-]{36})/)?.[1];
  const which = e.target.id === "dl-transcript" ? "transcript.txt" : e.target.id === "dl-report" ? "report.json" : null;
  if (!id || !which) return;
  try {
    const res = await api(`/api/sessions/${id}/${which}`, { raw: true });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `${which.split(".")[0]}-${id.slice(0, 8)}.${which.split(".")[1]}`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (err) { alert(err.message); }
});
