"use strict";

const $app = document.getElementById("app");
const BUSY = ["analyzing", "generating_questions", "transcribing", "evaluating"];
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
  stopRecording(true);
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
  if (s.status === "generating_questions") parts.push(progressCard(s));
  if (["transcribing", "evaluating"].includes(s.status)) parts.push(progressCard(s));
  if (s.status === "completed") parts.push(evaluationReport(s));
  const canAnswer = ["ready", "interviewing"].includes(s.status) || (s.status === "failed" && s.error?.stage === "evaluation");
  if (canAnswer && s.questions?.length) parts.unshift(`<div id="interview"></div>`);
  $app.innerHTML = parts.join("");
  if (canAnswer && s.questions?.length) renderInterview(s);
  bindRetry(s);
}

function progressCard(s) {
  const order = ["analyzing", "generating_questions", "transcribing", "evaluating"];
  const labels = { analyzing: "Screening resume against the job description", generating_questions: "Writing interview questions and grading rubric",
    transcribing: "Transcribing your recorded answers", evaluating: "Grading each answer against the rubric" };
  const phase = s.status === "analyzing" || s.status === "generating_questions" ? order.slice(0, 2) : order.slice(2);
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
function renderInterview(s) {
  const el = document.getElementById("interview");
  const qs = s.questions;
  const answered = (id) => s.answers?.[id]?.submitted;
  const skipped = renderInterview.skipped || (renderInterview.skipped = new Set());
  const idx = qs.findIndex((q) => !answered(q.id) && !skipped.has(q.id));
  const doneCount = qs.filter((q) => answered(q.id)).length;
  const dots = `<div class="qdots">${qs.map((q, i) => `<i class="${answered(q.id) ? "done" : i === idx ? "cur" : ""}" title="${q.id}"></i>`).join("")}</div>`;

  if (s.status === "ready" && !renderInterview.started) {
    el.innerHTML = `<div class="card interview">${dots}<h2>Voice interview</h2>
      <p class="muted">${qs.length} questions. Each question is read aloud; answer by speaking — there is no typing.
      You have up to ${fmtTime(s.max_answer_seconds)} per answer, and you can re-record before submitting.</p>
      <p class="small muted">Use a quiet room. Your browser will ask for microphone permission.</p>
      <div id="miccheck"></div>
      <button class="primary" id="start">🎙️ Start interview</button></div>`;
    document.getElementById("start").onclick = async () => {
      try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach((t) => t.stop()); }
      catch { document.getElementById("miccheck").innerHTML = `<div class="error">Microphone access is required. Allow it in your browser settings and try again.</div>`; return; }
      renderInterview.started = true; renderInterview(s);
    };
    return;
  }

  if (idx === -1) {
    el.innerHTML = `<div class="card interview">${dots}<h2>All done</h2>
      <p>${doneCount} of ${qs.length} questions answered.${doneCount < qs.length ? " Unanswered questions score 0." : ""}</p>
      <div class="row" style="justify-content:center">
        ${doneCount < qs.length ? `<button id="back">Go back to skipped questions</button>` : ""}
        <button class="primary" id="finish" ${doneCount ? "" : "disabled"}>Submit interview for evaluation</button></div></div>`;
    const back = document.getElementById("back");
    if (back) back.onclick = () => { skipped.clear(); renderInterview(s); };
    document.getElementById("finish").onclick = async (e) => {
      e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span> Submitting…`;
      try { await api(`/api/sessions/${s.id}/finish`, { method: "POST" }); loadSession(s.id); }
      catch (err) { alert(err.message); e.target.disabled = false; e.target.textContent = "Submit interview for evaluation"; }
    };
    return;
  }

  const q = qs[idx];
  el.innerHTML = `<div class="card interview">${dots}
    <div class="qcount">Question ${idx + 1} of ${qs.length} · ${esc(q.type.replace("_", " "))}</div>
    <div class="question">${esc(q.question)}</div>
    <div class="recorder" id="rec">
      <button class="mic" id="mic" aria-label="Start recording">🎙️</button>
      <canvas class="level" id="level" width="520" height="72"></canvas>
      <div class="timer" id="timer">0:00 / ${fmtTime(s.max_answer_seconds)}</div>
      <div id="recmsg" class="muted small">Press the microphone and answer out loud.</div>
      <div id="preview"></div>
      <div class="row" style="justify-content:center">
        <button id="replay">🔊 Replay question</button>
        <button id="skip">Skip</button>
      </div>
    </div></div>`;
  speak(q.question);
  document.getElementById("replay").onclick = () => speak(q.question);
  document.getElementById("skip").onclick = () => {
    if (!confirm("Skip this question? An unanswered question scores 0.")) return;
    stopRecording(true); skipped.add(q.id); renderInterview(s);
  };
  document.getElementById("mic").onclick = () => (recorder ? stopRecording() : startRecording(s, q));
}

function speak(text) {
  if (!("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.97;
  speechSynthesis.speak(u);
}

function pickMime() {
  const opts = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return opts.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

async function startRecording(s, q) {
  speechSynthesis?.cancel();
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
  catch { document.getElementById("recmsg").innerHTML = `<span class="error">Microphone access denied.</span>`; return; }
  const mime = pickMime();
  const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const ctx = new AudioContext();
  const analyser = ctx.createAnalyser(); analyser.fftSize = 512;
  ctx.createMediaStreamSource(stream).connect(analyser);
  recorder = { mr, stream, ctx, started: Date.now(), timer: null, raf: null, q, s };
  mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  mr.onstop = () => {
    const blob = new Blob(chunks, { type: (mr.mimeType || mime || "audio/webm") });
    const secs = (Date.now() - recorder.started) / 1000;
    cleanup();
    if (!recorder.discard) showPreview(s, q, blob, secs);
    recorder = null;
  };
  mr.start(1000);
  const mic = document.getElementById("mic");
  mic.classList.add("recording"); mic.textContent = "■"; mic.setAttribute("aria-label", "Stop recording");
  document.getElementById("recmsg").textContent = "Recording… press stop when you're finished.";
  document.getElementById("preview").innerHTML = "";
  const timerEl = document.getElementById("timer");
  recorder.timer = setInterval(() => {
    const el = (Date.now() - recorder.started) / 1000;
    timerEl.textContent = `${fmtTime(el)} / ${fmtTime(s.max_answer_seconds)}`;
    if (el >= s.max_answer_seconds) stopRecording();
  }, 250);
  const canvas = document.getElementById("level"), g = canvas.getContext("2d"), buf = new Uint8Array(analyser.fftSize);
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent");
  const draw = () => {
    analyser.getByteTimeDomainData(buf);
    g.clearRect(0, 0, canvas.width, canvas.height); g.strokeStyle = accent; g.lineWidth = 3; g.beginPath();
    for (let i = 0; i < buf.length; i++) { const x = (i / buf.length) * canvas.width, y = (buf[i] / 255) * canvas.height; i ? g.lineTo(x, y) : g.moveTo(x, y); }
    g.stroke(); recorder.raf = requestAnimationFrame(draw);
  };
  draw();

  function cleanup() {
    clearInterval(recorder.timer); cancelAnimationFrame(recorder.raf);
    stream.getTracks().forEach((t) => t.stop()); ctx.close();
  }
}

function stopRecording(discard = false) {
  if (!recorder) return;
  recorder.discard = discard;
  if (recorder.mr.state !== "inactive") recorder.mr.stop();
}

function showPreview(s, q, blob, secs) {
  const mic = document.getElementById("mic");
  if (!mic) return;
  mic.classList.remove("recording"); mic.textContent = "🎙️"; mic.setAttribute("aria-label", "Re-record");
  document.getElementById("recmsg").textContent = `Recorded ${fmtTime(secs)}. Listen back, then submit — or press the microphone to re-record.`;
  const url = URL.createObjectURL(blob);
  document.getElementById("preview").innerHTML = `<audio controls src="${url}"></audio>
    <div class="row" style="justify-content:center;margin-top:8px"><button class="primary" id="submit">Submit answer</button></div>`;
  document.getElementById("submit").onclick = async (e) => {
    e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span> Uploading…`;
    mic.disabled = true;
    const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
    const fd = new FormData();
    fd.append("audio", blob, `${q.id}.${ext}`);
    fd.append("duration_seconds", String(Math.round(secs)));
    try {
      await api(`/api/sessions/${s.id}/answers/${q.id}`, { method: "POST", body: fd });
      URL.revokeObjectURL(url);
      s.answers = { ...(s.answers || {}), [q.id]: { submitted: true } };
      if (s.status === "ready") s.status = "interviewing";
      renderInterview(s);
    } catch (err) {
      alert(`Upload failed: ${err.message}`);
      e.target.disabled = false; e.target.textContent = "Submit answer"; mic.disabled = false;
    }
  };
}

// ---------- evaluation report ----------
function evaluationReport(s) {
  const ev = s.evaluation, sc = ev.scoring;
  const recTone = { strong_advance: "good", advance: "good", borderline: "warn", do_not_advance: "bad" }[sc.recommendation];
  const byId = Object.fromEntries(ev.answers.map((a) => [a.question_id, a]));
  const cards = s.questions.map((q, i) => {
    const a = byId[q.id] || {}, ans = s.answers[q.id] || {};
    const t = a.score >= 7 ? "good" : a.score >= 5 ? "warn" : "bad";
    return `<div class="card qcard">
      <div class="head"><div style="flex:1"><div class="qcount">Question ${i + 1} · ${esc(q.type.replace("_", " "))}</div>
        <h3 style="margin-top:4px">${esc(q.question)}</h3><p class="small muted">Assesses: ${esc(q.assesses)}</p></div>
        <div class="qscore">${a.score ?? 0}<span class="muted small">/10</span><br><span class="pill ${t}">${esc((a.rating || "").replace("_", " "))}</span></div></div>
      ${ans.audio_url ? `<audio controls preload="none" src="${esc(ans.audio_url)}"></audio>` : `<p class="muted small">Not answered.</p>`}
      ${ans.transcript != null ? `<h3 style="margin-top:12px">Transcript ${ans.confidence != null ? `<span class="muted small">(transcription confidence ${Math.round(ans.confidence * 100)}%)</span>` : ""}</h3>
        <div class="transcript">${esc(ans.transcript || "[no speech detected]")}</div>` : ""}
      <h3 style="margin-top:12px">Reasoning</h3><p>${esc(a.reasoning)}</p>
      ${a.evidence_quotes?.length ? `<h3>Evidence</h3>${a.evidence_quotes.map((x) => `<blockquote>“${esc(x)}”</blockquote>`).join("")}` : ""}
      <div class="grid2" style="margin-top:10px"><div><h3>Covered</h3>${list(a.key_points_covered)}</div><div><h3>Missed</h3>${list(a.key_points_missed)}</div></div>
      ${a.red_flags_observed?.length ? `<h3>Red flags</h3>${list(a.red_flags_observed)}` : ""}
      <h3>How to improve</h3><p>${esc(a.improvement_advice)}</p>
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
      <details><summary>How this score is calculated</summary>${list(sc.rules)}
        <p class="small muted">Each answer is graded in a separate request against its own rubric, without the resume, so answers can't influence each other and resume claims can't inflate interview scores.</p></details>
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
