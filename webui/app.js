"use strict";

// ------------------------------------------------------------------ 状態
const PALETTE = ["#2f6fed", "#e0562d", "#2a9d5c", "#8b46c9", "#c9358a",
                 "#0d8f9e", "#b8860b", "#5566aa", "#606770"];
const DEFAULT_ROLE_NAMES = ["インタビュアー", "インタビュイー"];

let state = null;      // {name, roles:[str], options:{merge,timecodes}, segments:[...]}
let activeIdx = 0;     // キーボード操作のカーソル行
let playingIdx = -1;   // 現在再生中の行
let saveTimer = null;
let dirty = false;
let undoStack = [];    // 破壊的操作の取り消し履歴

const audio = document.getElementById("audio");
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ ユーティリティ
function fmt(t) {
  if (!isFinite(t)) t = 0;
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return m + ":" + String(s).padStart(2, "0");
}
// 行の時刻表示用（分:秒.小数）。編集時の精度を保つため fmt() より細かい。
function fmtPrecise(t) {
  if (!isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return m + ":" + s.toFixed(2).padStart(5, "0");
}
// "1:02.50" / "62.5" / "62.5s" などを秒数へ。解釈できなければ null。
function parseTimeInput(str) {
  str = (str || "").trim().replace(/s$/i, "");
  if (!str) return null;
  if (str.includes(":")) {
    const parts = str.split(":");
    if (parts.length !== 2) return null;
    const m = Number(parts[0]);
    const s = Number(parts[1]);
    if (!isFinite(m) || !isFinite(s) || m < 0 || s < 0) return null;
    return m * 60 + s;
  }
  const v = Number(str);
  return isFinite(v) && v >= 0 ? v : null;
}
/** ボタンに出す短縮名。頭2文字だけ（絵文字などのサロゲートペアも壊さない） */
function shortRole(role) {
  return Array.from(role || "").slice(0, 2).join("");
}
function roleColor(role) {
  if (!state) return "#999";
  const i = state.roles.indexOf(role);
  return i < 0 ? "#999" : PALETTE[i % PALETTE.length];
}
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2200);
}
function isEditing() {
  const a = document.activeElement;
  return a && a.classList && a.classList.contains("text");
}

/** どこかの入力欄に文字を打ち込んでいる最中か。
 *  contenteditable だけを見ていると、ダイアログの <input> で打った
 *  「内田」の d が「現在行を削除」に化けるなどの事故が起きる。 */
function isTyping() {
  const a = document.activeElement;
  if (!a) return false;
  if (a.isContentEditable) return true;
  const tag = a.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/** ダイアログを開いている間は本文のショートカットを止める */
function anyOverlayOpen() {
  return Array.prototype.some.call(document.querySelectorAll(".overlay"), (o) => !o.hidden);
}

// ------------------------------------------------------------------ 保存
function setSaveState(kind) {
  const el = $("saveState");
  el.className = kind;
  el.textContent = { dirty: "未保存…", saving: "保存中…", saved: "保存済み", err: "保存失敗", "": "—" }[kind] || "—";
}
function scheduleSave() {
  dirty = true;
  setSaveState("dirty");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 700);
}
async function doSave() {
  if (!state) return;
  clearTimeout(saveTimer);
  setSaveState("saving");
  try {
    const res = await fetch("/api/save?name=" + encodeURIComponent(state.name), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roles: state.roles, origins: state.origins || {},
                             options: state.options, segments: state.segments }),
    });
    const j = await res.json();
    if (j.ok) { dirty = false; setSaveState("saved"); }
    else setSaveState("err");
  } catch (e) { setSaveState("err"); }
}
// 離脱時に取りこぼしを送る
window.addEventListener("beforeunload", () => {
  if (dirty && state) {
    const blob = new Blob(
      [JSON.stringify({ roles: state.roles, options: state.options, segments: state.segments })],
      { type: "application/json" });
    navigator.sendBeacon("/api/save?name=" + encodeURIComponent(state.name), blob);
  }
});

// ------------------------------------------------------------------ 読み込み
async function loadProjectList() {
  const res = await fetch("/api/list");
  const j = await res.json();
  const sel = $("projectSel");
  sel.innerHTML = "";
  for (const p of j.projects) {
    const opt = document.createElement("option");
    opt.value = p.name;
    // 文字起こしがまだのものも一覧に出る。ここに出ないと処理を始められない。
    opt.textContent = p.name + (p.annotated ? " ●" : "")
                    + (p.transcribed ? "" : "（未文字起こし）")
                    + (p.audio ? "" : "（音声なし）");
    sel.appendChild(opt);
  }
  const last = localStorage.getItem("lastProject");
  if (last && j.projects.some((p) => p.name === last)) sel.value = last;
  if (sel.value) await loadProject(sel.value);
  else { $("emptyMsg").style.display = "block"; }
}

async function loadProject(name) {
  if (dirty) await doSave();
  const res = await fetch("/api/project?name=" + encodeURIComponent(name));
  if (!res.ok) { toast("読み込み失敗"); return; }
  state = await res.json();
  state.roles = state.roles || [];
  state.options = state.options || { merge: true, timecodes: false };
  activeIdx = 0; playingIdx = -1; dirty = false;
  undoStack = []; updateUndoBtn();
  localStorage.setItem("lastProject", name);

  audio.pause();
  audio.src = state.has_audio ? "/audio?name=" + encodeURIComponent(name) : "";
  seekbar.disabled = !state.has_audio;
  seekbar.value = 0;
  seekbar.style.background = "var(--line)";
  audio.playbackRate = parseFloat($("rate").value);
  $("optMerge").checked = !!state.options.merge;
  $("optTime").checked = !!state.options.timecodes;
  setSaveState("");
  renderRoles();
  if (state.transcribed === false) {
    // 音声だけ置かれている状態。行が無いので、次にやることを出す。
    $("segments").innerHTML = "";
    $("emptyMsg").innerHTML =
      "この収録はまだ文字起こしされていません。<br>ヘッダーの「文字起こし」から実行できます。";
    $("emptyMsg").style.display = "block";
  } else {
    $("emptyMsg").style.display = "none";
    renderSegments();
  }
  syncSpeakerCount();
}

// ------------------------------------------------------------------ ロール凡例
function renderRoles() {
  // 人数に応じて行のロール欄の幅を決める。2人なら従来どおり、増えるほど広げる。
  // 2段に収めるので、列数は人数の半分（切り上げ）。幅もそれに合わせる。
  const n = state ? state.roles.length : 2;
  const cols = Math.max(1, Math.ceil(n / 2));
  document.documentElement.style.setProperty("--role-cols", cols);
  document.documentElement.style.setProperty("--role-col", (cols * 52 + 6) + "px");

  // ヘッダーのチップは表示専用。編集は「話者を登録」のパネルで行う。
  const box = $("roles");
  box.innerHTML = "";
  state.roles.forEach((role, i) => {
    const chip = document.createElement("div");
    chip.className = "role-chip";
    chip.title = "キー " + (i + 1) + " でこの話者を付ける";
    chip.innerHTML =
      '<span class="key">' + (i + 1) + '</span>' +
      '<span class="swatch" style="background:' + PALETTE[i % PALETTE.length] + '"></span>' +
      '<span class="name"></span>';
    chip.querySelector(".name").textContent = role;
    box.appendChild(chip);
  });
}

// ------------------------------------------------------------------ セグメント描画
function renderSegments() {
  const box = $("segments");
  box.innerHTML = "";
  const topAdd = document.createElement("button");
  topAdd.className = "add-row-btn";
  topAdd.textContent = "＋ 先頭に行を追加";
  topAdd.title = "最初のセリフより前に、聞き取れていない発話を追加";
  topAdd.addEventListener("click", () => insertSegAt(0));
  box.appendChild(topAdd);
  const frag = document.createDocumentFragment();
  state.segments.forEach((seg, idx) => frag.appendChild(buildSeg(seg, idx)));
  box.appendChild(frag);
  updateActiveClass();
}

function buildSeg(seg, idx) {
  const row = document.createElement("div");
  row.className = "seg" + (seg.manual ? " manual" : "");
  row.dataset.idx = idx;

  row.appendChild(buildTimeCell(seg, idx));

  // ロールは常に2段に並べる。名前は頭2文字だけ出し、番号（キーボードの数字キー）を
  // 添える。「インタビュアー」と「インタビュイー」は2文字だと区別が付かないため。
  const roleCell = document.createElement("div");
  roleCell.className = "role-cell";
  state.roles.forEach((role, ri) => {
    const b = document.createElement("button");
    b.className = "role-btn" + (seg.role === role ? " on" : "");
    const num = document.createElement("span");
    num.className = "n";
    num.textContent = String(ri + 1);
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = shortRole(role);
    b.appendChild(num); b.appendChild(nm);
    if (seg.role === role) b.style.background = PALETTE[ri % PALETTE.length];
    b.title = role + "（キー " + (ri + 1) + "）";
    b.addEventListener("click", (e) => { e.stopPropagation(); assignRole(idx, seg.role === role ? null : role); });
    roleCell.appendChild(b);
  });
  row.appendChild(roleCell);

  // 「辞書へ」は下の ops に入れるが、text の input ハンドラから触るので先に作る
  const dictB = mkOp("辞書へ", "この修正を「誤→正」として置換辞書に登録する",
                     () => registerFromRow(idx));
  dictB.classList.add("to-dict");
  dictB.hidden = seg.text === seg.original;   // 直した行にだけ出す

  const text = document.createElement("div");
  text.className = "text" + (seg.text !== seg.original ? " edited" : "")
                          + (seg.unclear ? " unclear" : "");
  if (seg.unclear) text.title = "話者の推定に迷いあり（2位と僅差）。聞いて確かめてください";
  text.contentEditable = "true";
  text.spellcheck = false;
  text.textContent = seg.text;
  text.addEventListener("focus", () => setActive(idx, false));
  text.addEventListener("input", () => {
    seg.text = text.textContent;
    const changed = seg.text !== seg.original;
    text.classList.toggle("edited", changed);
    dictB.hidden = !changed;
    scheduleSave();
  });
  row.appendChild(text);

  const ops = document.createElement("div");
  ops.className = "ops";
  const splitB = mkOp("分割", "カーソル位置で分割（再生中ならその時刻を境界に使用）", () => splitSeg(idx));
  const mergeB = mkOp("↑結合", "上の行と結合", () => mergeUp(idx));
  const addB = mkOp("＋行", "この行の後に、聞き取れていない発話用の行を追加", () => insertSegAt(idx + 1));
  const delB = mkOp("削除", "この行を削除 (D)", () => deleteSeg(idx));
  ops.appendChild(dictB); ops.appendChild(splitB); ops.appendChild(mergeB); ops.appendChild(addB); ops.appendChild(delB);
  row.appendChild(ops);

  row.addEventListener("mousedown", (e) => {
    if (e.target.closest("button") || e.target.classList.contains("text") || e.target.classList.contains("t-val")) return;
    setActive(idx);
  });
  return row;
}
function mkOp(label, title, fn) {
  const b = document.createElement("button");
  b.textContent = label; b.title = title;
  b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
  return b;
}

// --- 時刻セル（開始/終了を個別に編集・再生位置から設定） ------------
function buildTimeCell(seg, idx) {
  const cell = document.createElement("div");
  cell.className = "time-cell";

  ["start", "end"].forEach((field) => {
    const trow = document.createElement("div");
    trow.className = "t-row";

    const val = document.createElement("span");
    val.className = "t-val";
    val.contentEditable = "false";
    val.spellcheck = false;
    val.textContent = fmtPrecise(seg[field]);
    val.title = "クリック: この位置へ移動 / ダブルクリック: 時刻を直接編集";
    val.addEventListener("click", (e) => {
      e.stopPropagation();
      if (val.isContentEditable) return;
      seek(seg[field], true);
      setActive(idx);
    });
    val.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      beginEditTime(val, idx, field);
    });
    trow.appendChild(val);

    const setB = document.createElement("button");
    setB.className = "t-set";
    setB.textContent = "●";
    setB.title = (field === "start" ? "開始" : "終了") + "時刻を今の再生位置に合わせる";
    setB.addEventListener("click", (e) => {
      e.stopPropagation();
      setTimeFromPlayback(idx, field);
    });
    trow.appendChild(setB);

    cell.appendChild(trow);
  });

  if (seg.manual) {
    const badge = document.createElement("span");
    badge.className = "badge-manual";
    badge.textContent = "追加";
    badge.title = "手動で追加した行";
    cell.appendChild(badge);
  }
  return cell;
}

function beginEditTime(el, idx, field) {
  el.contentEditable = "true";
  el.classList.add("editing");
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  const onKey = (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); el.blur(); }
    else if (e.key === "Escape") {
      e.preventDefault();
      el.textContent = fmtPrecise(state.segments[idx][field]);
      el.blur();
    }
  };
  const onBlur = () => {
    el.removeEventListener("keydown", onKey);
    el.removeEventListener("blur", onBlur);
    el.contentEditable = "false";
    el.classList.remove("editing");
    commitTimeEdit(idx, field, el.textContent);
  };
  el.addEventListener("keydown", onKey);
  el.addEventListener("blur", onBlur);
}

function commitTimeEdit(idx, field, text) {
  const seg = state.segments[idx];
  const val = parseTimeInput(text);
  if (val === null) { toast("時刻を解釈できません（例: 12.5 や 1:02.50）"); refreshRow(idx); return; }
  if (field === "start" && val >= seg.end) { toast("開始は終了より前にしてください"); refreshRow(idx); return; }
  if (field === "end" && val <= seg.start) { toast("終了は開始より後にしてください"); refreshRow(idx); return; }
  pushUndo("時刻編集");
  seg[field] = val;
  refreshRow(idx);
  scheduleSave();
}

function setTimeFromPlayback(idx, field) {
  if (!state.has_audio) { toast("音声がありません"); return; }
  commitTimeEdit(idx, field, String(audio.currentTime));
}

// 聞き取れていない発話を挿入する。insertIdx の位置に空行を差し込む。
function insertSegAt(insertIdx) {
  pushUndo("追加");
  const prev = state.segments[insertIdx - 1];
  const next = state.segments[insertIdx];
  const start = prev ? prev.end : Math.max(0, (next ? next.start : 1.5) - 1.5);
  let end = (next && next.start > start) ? Math.min(next.start, start + 1.5) : start + 1.5;
  if (end <= start) end = start + 0.5;
  const seg = {
    id: Date.now() + insertIdx, start, end,
    text: "", original: null, role: null, manual: true,
  };
  state.segments.splice(insertIdx, 0, seg);
  renderSegments();
  setActive(insertIdx);
  scheduleSave();
  toast("行を追加しました。時刻を調整し、テキストを入力してください（⌘Z で取り消し）");
  requestAnimationFrame(() => {
    const el = document.querySelector('.seg[data-idx="' + insertIdx + '"] .text');
    if (el) el.focus();
  });
}

// ------------------------------------------------------------------ 行操作
function assignRole(idx, role) {
  state.segments[idx].role = role;
  refreshRow(idx);
  scheduleSave();
}
function refreshRow(idx) {
  const old = document.querySelector('.seg[data-idx="' + idx + '"]');
  if (old) old.replaceWith(buildSeg(state.segments[idx], idx));
  updateActiveClass();
}

function setActive(idx, scroll = true) {
  activeIdx = Math.max(0, Math.min(idx, state.segments.length - 1));
  updateActiveClass();
  if (scroll) scrollToRow(activeIdx);
}
function updateActiveClass() {
  document.querySelectorAll(".seg").forEach((el) => {
    const i = +el.dataset.idx;
    el.classList.toggle("active", i === activeIdx);
    el.classList.toggle("playing", i === playingIdx);
  });
}
function scrollToRow(idx) {
  const el = document.querySelector('.seg[data-idx="' + idx + '"]');
  if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
}

// カーソル位置で分割
function caretOffset(el) {
  const sel = window.getSelection();
  if (!sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  if (!el.contains(range.startContainer)) return null;
  const pre = range.cloneRange();
  pre.selectNodeContents(el);
  pre.setEnd(range.startContainer, range.startOffset);
  return pre.toString().length;
}
function splitSeg(idx) {
  const seg = state.segments[idx];
  const el = document.querySelector('.seg[data-idx="' + idx + '"] .text');
  let pos = el ? caretOffset(el) : null;
  const text = seg.text;
  if (pos === null || pos <= 0 || pos >= text.length) pos = Math.floor(text.length / 2);
  if (text.length < 2) { toast("分割できません"); return; }
  pushUndo("分割");
  let boundary;
  const t = audio.currentTime;
  if (state.has_audio && t > seg.start && t < seg.end) {
    boundary = t; // 再生位置がこの行の範囲内なら、それを境界として使う（文字数比率より正確）
  } else {
    const ratio = pos / text.length;
    boundary = seg.start + (seg.end - seg.start) * ratio;
  }
  const first = { ...seg, text: text.slice(0, pos).trim(), end: boundary };
  const second = {
    id: Date.now() + idx, start: boundary, end: seg.end,
    text: text.slice(pos).trim(), original: "", role: seg.role,
  };
  first.original = "";  // 編集扱い
  state.segments.splice(idx, 1, first, second);
  renderSegments();
  setActive(idx + 1);
  scheduleSave();
}
function pushUndo(label) {
  undoStack.push({ segs: JSON.parse(JSON.stringify(state.segments)), active: activeIdx, label });
  if (undoStack.length > 100) undoStack.shift();
  updateUndoBtn();
}
function undo() {
  if (!undoStack.length) { toast("これ以上は戻せません"); return; }
  const u = undoStack.pop();
  state.segments = u.segs;
  renderSegments();
  setActive(Math.min(u.active, state.segments.length - 1));
  scheduleSave();
  updateUndoBtn();
  toast("元に戻しました" + (u.label ? "（" + u.label + "）" : ""));
}
function updateUndoBtn() {
  const b = $("undoBtn");
  if (b) b.disabled = undoStack.length === 0;
}

function deleteSeg(idx) {
  if (state.segments.length <= 1) { toast("最後の1行は削除できません"); return; }
  pushUndo("削除");
  state.segments.splice(idx, 1);
  renderSegments();
  setActive(Math.min(idx, state.segments.length - 1));
  scheduleSave();
  toast("1行削除しました（⌘Z で取り消し）");
}
function mergeUp(idx) {
  if (idx === 0) { toast("先頭行は結合できません"); return; }
  pushUndo("結合");
  const prev = state.segments[idx - 1], cur = state.segments[idx];
  prev.text = (prev.text + " " + cur.text).trim();
  prev.end = cur.end;
  prev.original = "";
  if (!prev.role) prev.role = cur.role;
  state.segments.splice(idx, 1);
  renderSegments();
  setActive(idx - 1);
  scheduleSave();
}

// ------------------------------------------------------------------ 音声
function seek(t, play) {
  if (!state || !state.has_audio) return;
  audio.currentTime = Math.max(0, t);
  if (play) audio.play();
}
function updatePlaying() {
  const t = audio.currentTime;
  let idx = -1;
  const segs = state.segments;
  for (let i = 0; i < segs.length; i++) {
    if (t >= segs[i].start && t < segs[i].end) { idx = i; break; }
    if (segs[i].start > t) break;
  }
  if (idx !== playingIdx) {
    playingIdx = idx;
    updateActiveClass();
    if (idx >= 0 && $("follow").checked && !isEditing()) scrollToRow(idx);
  }
}

// --- シークバー ---
let scrubbing = false;
const seekbar = $("seekbar");
function renderSeek() {
  const d = audio.duration;
  const pct = d ? (audio.currentTime / d) * 100 : 0;
  if (!scrubbing) seekbar.value = Math.round(pct * 10); // 0..1000
  seekbar.style.background =
    "linear-gradient(to right, var(--accent) " + pct + "%, var(--line) " + pct + "%)";
}
seekbar.addEventListener("input", () => {
  scrubbing = true;
  const d = audio.duration || 0;
  const t = (seekbar.value / 1000) * d;
  $("clock").textContent = fmt(t) + " / " + fmt(d);
  seekbar.style.background =
    "linear-gradient(to right, var(--accent) " + (seekbar.value / 10) + "%, var(--line) " + (seekbar.value / 10) + "%)";
});
seekbar.addEventListener("change", () => {
  const d = audio.duration || 0;
  audio.currentTime = (seekbar.value / 1000) * d;
  scrubbing = false;
  if (state && state.has_audio) audio.play(); // クリック/シークした位置から再生
});

audio.addEventListener("timeupdate", () => {
  $("clock").textContent = fmt(audio.currentTime) + " / " + fmt(audio.duration);
  renderSeek();
  updatePlaying();
});
audio.addEventListener("play", () => { $("playPause").textContent = "⏸"; });
audio.addEventListener("pause", () => { $("playPause").textContent = "▶"; });
audio.addEventListener("loadedmetadata", () => { $("clock").textContent = "0:00 / " + fmt(audio.duration); renderSeek(); });

function togglePlay() {
  if (!state || !state.has_audio) { toast("音声がありません"); return; }
  if (audio.paused) audio.play(); else audio.pause();
}

// ------------------------------------------------------------------ キーボード
document.addEventListener("keydown", (e) => {
  if (!state) return;
  // IME で変換している最中のキーは横取りしない。日本語入力では
  // 確定前のキーがそのまま飛んでくるため（「内田」の d など）。
  if (e.isComposing || e.keyCode === 229) return;
  // ダイアログを開いている間は本文の操作を止める
  if (anyOverlayOpen()) return;
  // 入力欄・テキスト編集中はショートカット無効（Escで抜ける）
  if (isTyping()) {
    if (e.key === "Escape") document.activeElement.blur();
    return;
  }
  if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) { e.preventDefault(); undo(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (e.key === " ") { e.preventDefault(); togglePlay(); return; }
  if (e.key >= "1" && e.key <= "9") {
    const ri = +e.key - 1;
    if (ri < state.roles.length) {
      e.preventDefault();
      assignRole(activeIdx, state.roles[ri]);
      if (activeIdx < state.segments.length - 1) setActive(activeIdx + 1);
    }
    return;
  }
  if (e.key === "0") { e.preventDefault(); assignRole(activeIdx, null); return; }
  if (e.key === "j" || e.key === "J" || e.key === "ArrowDown") {
    e.preventDefault(); setActive(activeIdx + 1); return;
  }
  if (e.key === "k" || e.key === "K" || e.key === "ArrowUp") {
    e.preventDefault(); setActive(activeIdx - 1); return;
  }
  if (e.key === "Enter") {
    e.preventDefault();
    const el = document.querySelector('.seg[data-idx="' + activeIdx + '"] .text');
    if (el) { el.focus(); document.getSelection().selectAllChildren(el); document.getSelection().collapseToEnd(); }
    return;
  }
  if (e.key === "d" || e.key === "D") { e.preventDefault(); deleteSeg(activeIdx); return; }
  if (e.key === "i" || e.key === "I") { e.preventDefault(); insertSegAt(activeIdx + 1); return; }
  if (e.key === "r" || e.key === "R") { e.preventDefault(); seek(state.segments[activeIdx].start, true); return; }
  if (e.key === "ArrowLeft") { e.preventDefault(); seek(audio.currentTime - 3, false); return; }
  if (e.key === "ArrowRight") { e.preventDefault(); seek(audio.currentTime + 3, false); return; }
});

// ------------------------------------------------------------------ ヘッダー操作
$("projectSel").addEventListener("change", (e) => loadProject(e.target.value));
$("playPause").addEventListener("click", togglePlay);
$("undoBtn").addEventListener("click", undo);
$("rate").addEventListener("change", (e) => { audio.playbackRate = parseFloat(e.target.value); });
$("optMerge").addEventListener("change", (e) => { state.options.merge = e.target.checked; scheduleSave(); });
$("optTime").addEventListener("change", (e) => { state.options.timecodes = e.target.checked; scheduleSave(); });

$("exportBtn").addEventListener("click", async () => {
  if (!state) return;
  if (dirty) await doSave();
  const res = await fetch("/api/export?name=" + encodeURIComponent(state.name), { method: "POST" });
  const j = await res.json();
  if (j.ok) {
    let msg = "書き出しました: " + j.path.split("/").pop() + "（" + j.lines + "行）";
    if (j.unlabeled) msg += " ／ 未ラベル " + j.unlabeled + " 行は (未設定) で出力";
    toast(msg);
  } else toast("書き出し失敗");
});

$("resetBtn").addEventListener("click", async () => {
  if (!state) return;
  if (!confirm("トランスクリプトから作り直します。付与したロールと編集は失われます。よろしいですか？")) return;
  const res = await fetch("/api/reset?name=" + encodeURIComponent(state.name), { method: "POST" });
  if (res.ok) { state = await res.json(); activeIdx = 0; renderRoles(); renderSegments(); toast("再読込しました"); }
});

// ------------------------------------------------------------------ 起動
loadProjectList();

// ------------------------------------------------------------------ 置換辞書
// 「誤 → 正」の決定的な置換。モデルを上げても残る同音語・言い間違い・
// 漢字の揺れを潰すためのもの。件数の上限は無い。
let replacements = [];   // [{from, to}]

/**
 * 編集前後の文字列から、実際に変わった部分だけを取り出す。
 * 「こちらケイト25歳です」→「内田圭人25歳です」なら {from:"こちらケイト", to:"内田圭人"}。
 * 行まるごとを辞書に入れると再利用できないので、共通の前後を削って芯だけ残す。
 */
// 漢字・カタカナ・英数字は語を作る文字。ひらがなと記号は語の切れ目とみなす。
const WORDY = /[\u4E00-\u9FFF\u3005\u30A1-\u30FA\u30FC0-9A-Za-z]/;

function diffPair(before, after) {
  if (!before || before === after) return null;
  let head = 0;
  while (head < before.length && head < after.length && before[head] === after[head]) head++;
  let tail = 0;
  while (tail < before.length - head && tail < after.length - head &&
         before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail++;

  // 差分が1〜2文字だと規則が広すぎる。「小平倫太郎→小平凛太郎」を直しただけで
  // 「倫→凛」を登録すると、無関係な「倫理」まで置換してしまう。
  // そういうときだけ、前後の語のかたまりを巻き込むまで範囲を広げる。
  if (before.slice(head, before.length - tail).length <= 2) {
    while (head > 0 && WORDY.test(before[head - 1])) head--;
    while (tail > 0 && WORDY.test(before[before.length - tail])) tail--;
  }

  const from = before.slice(head, before.length - tail);
  const to = after.slice(head, after.length - tail);
  if (!from) return null;                 // 追加しただけ（消すべき誤りが無い）
  if (from.length > 30) return null;      // 長すぎるものは辞書に向かない
  return { from, to };
}

async function loadReplacements() {
  try {
    const r = await fetch("/api/replacements");
    const j = await r.json();
    replacements = j.replacements || [];
    $("replPath").textContent = j.path || "";
  } catch (e) { replacements = []; }
}

function renderReplRows() {
  const box = $("replRows");
  box.innerHTML = "";
  if (!replacements.length) {
    const d = document.createElement("div");
    d.className = "repl-empty";
    d.textContent = "まだ登録がありません。行を編集して「辞書へ」を押すか、下の「＋ 行を追加」から登録します。";
    box.appendChild(d);
  }
  replacements.forEach((rule, i) => {
    const row = document.createElement("div");
    row.className = "repl-row";

    const from = document.createElement("input");
    from.value = rule.from;
    from.placeholder = "誤（文字起こしに出る形）";
    from.addEventListener("input", () => { rule.from = from.value; });

    const arrow = document.createElement("span");
    arrow.className = "arrow";
    arrow.textContent = "→";

    const to = document.createElement("input");
    to.value = rule.to;
    to.placeholder = "正（置き換えたい形）";
    to.addEventListener("input", () => { rule.to = to.value; });

    const hit = document.createElement("span");
    hit.className = "hit";
    hit.textContent = state ? countHits(rule.from) : "";

    const del = document.createElement("button");
    del.textContent = "×";
    del.title = "この行を削除";
    del.addEventListener("click", () => { replacements.splice(i, 1); renderReplRows(); });

    row.appendChild(from); row.appendChild(arrow); row.appendChild(to);
    row.appendChild(hit); row.appendChild(del);
    box.appendChild(row);
  });
  $("replCount").textContent = replacements.length + " 件";
}

/** いま開いている収録の中で、その語が何箇所あるか（適用前の目安） */
function countHits(from) {
  if (!from || !state) return "";
  let n = 0;
  for (const s of state.segments) {
    if (!s.text) continue;
    let i = 0;
    while ((i = s.text.indexOf(from, i)) !== -1) { n++; i += from.length; }
  }
  return n ? n + "箇所" : "—";
}

function openRepl() {
  renderReplRows();
  $("replOverlay").hidden = false;
}
function closeRepl() { $("replOverlay").hidden = true; }

async function saveReplacements() {
  const res = await fetch("/api/replacements", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ replacements }),
  });
  const j = await res.json();
  replacements = j.replacements || [];
  renderReplRows();
  toast("辞書を保存しました（" + replacements.length + "件）");
}

async function applyReplacements() {
  if (!state) { toast("収録が選ばれていません"); return; }
  await saveReplacements();                 // 編集中の内容を先に確定させる
  if (!replacements.length) { toast("辞書が空です"); return; }
  pushUndo("置換辞書の適用");               // ⌘Z で戻せるようにする
  const res = await fetch("/api/apply-replacements?name=" + encodeURIComponent(state.name),
                          { method: "POST" });
  const j = await res.json();
  if (j.error) { toast("適用に失敗しました"); undoStack.pop(); updateUndoBtn(); return; }
  if (!j.applied) { toast("置換対象はありませんでした"); undoStack.pop(); updateUndoBtn(); return; }
  state.segments = j.state.segments;
  renderSegments();
  setActive(Math.min(activeIdx, state.segments.length - 1));
  renderReplRows();
  const top = j.details.slice(0, 3).map((d) => d.from + "×" + d.count).join("、");
  toast("置換 " + j.applied + "箇所 / " + j.segments + "行（" + top + "）");
}

/** 編集済みの行から「誤→正」を拾って辞書に足す */
async function registerFromRow(idx) {
  const seg = state.segments[idx];
  const pair = diffPair(seg.original || "", seg.text || "");
  if (!pair) { toast("辞書に入れられる差分がありません"); return; }
  const dup = replacements.find((r) => r.from === pair.from);
  if (dup) {
    dup.to = pair.to;
    toast("登録を更新: " + pair.from + " → " + pair.to);
  } else {
    replacements.push(pair);
    toast("辞書に登録: " + pair.from + " → " + pair.to);
  }
  await saveReplacements();
}

$("replBtn").addEventListener("click", openRepl);
$("replClose").addEventListener("click", closeRepl);
$("replAdd").addEventListener("click", () => { replacements.push({ from: "", to: "" }); renderReplRows(); });
$("replSave").addEventListener("click", saveReplacements);
$("replApply").addEventListener("click", applyReplacements);
$("replOverlay").addEventListener("click", (e) => { if (e.target.id === "replOverlay") closeRepl(); });
// Escape はどのダイアログでも閉じる。手前（後から開いたもの）から順に。
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  for (const id of ["confirmOverlay", "jobOverlay", "rolesOverlay", "diarOverlay", "replOverlay"]) {
    if (!$(id).hidden) {
      e.stopPropagation();
      // 確認ダイアログは「やめる」と同じ扱い。Escape で消えて勝手に進むことはない。
      ({ confirmOverlay: () => $("confirmCancel").click(),
         jobOverlay: closeJob,
         rolesOverlay: closeRoles, diarOverlay: closeDiar, replOverlay: closeRepl })[id]();
      return;
    }
  }
}, true);

loadReplacements();

// ------------------------------------------------------------------ 話者の登録
// ヘッダーのチップは表示専用にしてあるので、人数と名前の編集はすべてここで行う。
// パネルは下書き（draftRoles）を編集し、「保存」を押して初めて state に反映する。
let draftRoles = [];

function syncSpeakerCount() {
  if (!state) return;
  $("speakerCount").value = String(Math.min(9, Math.max(2, state.roles.length)));
}

/** その名前が、話者分離のどのラベル（SPEAKER_00 など）だったか */
function roleOrigin(name) {
  const o = (state && state.origins) || {};
  for (const label of Object.keys(o)) if (o[label] === name) return label;
  return null;
}

/** その話者が今この収録の何行で使われているか */
function roleUsage(role) {
  if (!state || !role) return 0;
  return state.segments.filter((s) => s.role === role).length;
}

function renderRolesRows() {
  const box = $("rolesRows");
  box.innerHTML = "";
  draftRoles.forEach((role, i) => {
    const row = document.createElement("div");
    row.className = "repl-row";

    const key = document.createElement("span");
    key.className = "role-key";
    key.textContent = String(i + 1);

    const swatch = document.createElement("span");
    swatch.className = "role-swatch";
    swatch.style.background = PALETTE[i % PALETTE.length];

    const input = document.createElement("input");
    input.value = role;
    input.placeholder = "話者の名前（例: インタビュアー）";
    input.addEventListener("input", () => { draftRoles[i] = input.value; });

    // 話者分離から来た話者は、元のラベルを添えて対応が追えるようにする。
    // 照合は手動なので、あとから見直せることが大事。
    const origin = document.createElement("span");
    origin.className = "role-origin";
    const label = roleOrigin(role);
    origin.textContent = label || "";
    origin.title = label ? "話者分離のラベル" : "";

    const used = document.createElement("span");
    used.className = "hit";
    const n = roleUsage(role);
    used.textContent = n ? n + "行" : "—";

    const del = document.createElement("button");
    del.textContent = "×";
    del.title = "この話者を削除";
    del.addEventListener("click", () => {
      if (draftRoles.length <= 1) { toast("1人は必要です"); return; }
      draftRoles.splice(i, 1);
      renderRolesRows();
    });

    row.appendChild(key); row.appendChild(swatch); row.appendChild(input);
    row.appendChild(origin); row.appendChild(used); row.appendChild(del);
    box.appendChild(row);
  });
  $("rolesCount").textContent = draftRoles.length + "人";
  $("speakerCount").value = String(Math.min(9, Math.max(2, draftRoles.length)));
}

function openRoles() {
  if (!state) { toast("収録が選ばれていません"); return; }
  draftRoles = state.roles.slice();
  if (!draftRoles.length) draftRoles = DEFAULT_ROLE_NAMES.slice();
  renderRolesRows();
  $("rolesOverlay").hidden = false;
}
function closeRoles() { $("rolesOverlay").hidden = true; }

/** 人数セレクタ: 下書きの長さを合わせるだけ。反映は「保存」で */
function setDraftCount(n) {
  while (draftRoles.length < n) draftRoles.push("話者" + (draftRoles.length + 1));
  if (draftRoles.length > n) draftRoles = draftRoles.slice(0, n);
  renderRolesRows();
}

function saveRoles() {
  const next = draftRoles.map((r) => r.trim()).filter((r) => r);
  if (!next.length) { toast("名前を1つ以上入れてください"); return; }
  if (new Set(next).size !== next.length) { toast("同じ名前が複数あります"); return; }

  const before = state.roles;
  // 位置が同じものは「改名」とみなして、割り当て済みの行も追従させる
  const renamed = {};
  before.forEach((old, i) => { if (next[i] && next[i] !== old) renamed[old] = next[i]; });
  // 消えた話者に割り当てられていた行は未設定に戻す
  const gone = before.filter((r, i) => !next.includes(r) && !renamed[r]);
  const lost = state.segments.filter((s) => gone.includes(s.role)).length;
  if (lost && !confirm(gone.join("、") + " に割り当てられた " + lost +
                       " 行が未設定に戻ります。よろしいですか？")) return;

  // 元ラベルとの対応も改名に追従させる（照合の履歴を失わないため）
  const origins = Object.assign({}, state.origins || {});
  for (const label of Object.keys(origins)) {
    if (renamed[origins[label]]) origins[label] = renamed[origins[label]];
    else if (!next.includes(origins[label])) delete origins[label];
  }
  state.origins = origins;

  state.roles = next;
  state.segments.forEach((s) => {
    if (!s.role) return;
    if (renamed[s.role]) s.role = renamed[s.role];
    else if (!next.includes(s.role)) s.role = null;
  });
  renderRoles(); renderSegments(); scheduleSave(); syncSpeakerCount();
  closeRoles();
  toast("話者を保存しました（" + next.length + "人）");
}

$("rolesBtn").addEventListener("click", openRoles);
$("rolesClose").addEventListener("click", closeRoles);
$("rolesAdd").addEventListener("click", () => {
  if (draftRoles.length >= 9) { toast("9人までです"); return; }
  draftRoles.push("話者" + (draftRoles.length + 1));
  renderRolesRows();
});
$("rolesSave").addEventListener("click", saveRoles);
$("rolesOverlay").addEventListener("click", (e) => { if (e.target.id === "rolesOverlay") closeRoles(); });
$("speakerCount").addEventListener("change", (e) => setDraftCount(parseInt(e.target.value, 10)));

// ------------------------------------------------------------------ 話者分離の取り込み
let diarInfo = null;   // {found, file, spans, speakers}

async function openDiar() {
  if (!state) { toast("収録が選ばれていません"); return; }
  const body = $("diarBody");
  body.innerHTML = "";
  $("diarNote").textContent = "";
  $("diarFile").textContent = "";
  $("diarOverlay").hidden = false;

  const r = await fetch("/api/diarization?name=" + encodeURIComponent(state.name));
  diarInfo = await r.json();
  if (!diarInfo.found) {
    body.innerHTML = '<div class="repl-empty">' +
      '話者分離のファイルが見つかりません。<br>' +
      '収録と同じ場所に <code>' + state.name + '.json</code>（WhisperX の出力）か ' +
      '<code>' + state.name + '.rttm</code> を置いてください。</div>';
    $("diarApply").disabled = true;
    return;
  }
  $("diarApply").disabled = false;
  $("diarFile").textContent = diarInfo.file + "（" + diarInfo.spans + "区間）";
  // 検出された話者を並べるだけ。名前を付ける（人物との照合）のは
  // 割り当てたあとの「話者を登録」で行う。場所を1つにしておく。
  diarInfo.speakers.forEach((spk, i) => {
    const row = document.createElement("div");
    row.className = "repl-row";
    const key = document.createElement("span");
    key.className = "role-key";
    key.textContent = String(i + 1);
    const swatch = document.createElement("span");
    swatch.className = "role-swatch";
    swatch.style.background = PALETTE[i % PALETTE.length];
    const label = document.createElement("span");
    label.textContent = spk;
    label.style.cssText = "flex:1;font-size:13px";
    row.appendChild(key); row.appendChild(swatch); row.appendChild(label);
    body.appendChild(row);
  });
  $("diarNote").textContent = diarInfo.speakers.length + "人を検出";
}

function closeDiar() { $("diarOverlay").hidden = true; }

async function applyDiar() {
  if (!state || !diarInfo || !diarInfo.found) return;
  // 全行を振り直すので、手で直した割り当ては失われる。黙って消さない。
  const assigned = state.segments.filter((s) => s.role).length;
  if (assigned &&
      !confirm("すでに " + assigned + " 行に話者が付いています。\n" +
               "読み込み直すと全行が振り直され、手で直した分は失われます。\n" +
               "（付けた話者の名前は引き継がれます。⌘Z で戻せます）\n\nよろしいですか？")) {
    return;
  }
  pushUndo("話者の割り当て");           // ⌘Z で戻せるようにする
  const res = await fetch("/api/apply-speakers?name=" + encodeURIComponent(state.name), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const j = await res.json();
  if (!j.ok) {
    undoStack.pop(); updateUndoBtn();
    toast(j.error === "no_file" ? "話者分離のファイルがありません" : "読み込めませんでした");
    return;
  }
  state.roles = j.state.roles;
  state.origins = j.state.origins || {};
  state.segments = j.state.segments;
  renderRoles(); renderSegments();
  setActive(Math.min(activeIdx, state.segments.length - 1));
  syncSpeakerCount();
  closeDiar();
  const kept = Object.keys(j.kept_names || {}).length;
  toast("話者を割り当てました: " + j.assigned + "行 / 迷い " + j.unclear +
        "行 / 対応なし " + j.unmatched + "行" +
        (kept ? "（名前 " + kept + "件を引き継ぎ）" : ""));
  openRoles();   // そのまま「誰がどの話者か」の照合へ進む
}

$("diarBtn").addEventListener("click", openDiar);
$("diarClose").addEventListener("click", closeDiar);
$("diarApply").addEventListener("click", applyDiar);
$("diarOverlay").addEventListener("click", (e) => { if (e.target.id === "diarOverlay") closeDiar(); });

// ------------------------------------------------------------------ 文字起こし・話者分離
// 実行はサーバー側の別プロセス。状態もサーバーが持つので、ページを閉じて
// 開き直しても進捗に戻れる。
let caps = null;         // この環境でできること
let jobPoll = null;      // 進捗のポーリング
let jobTarget = null;    // いま処理しようとしている収録
let jobProject = null;   // その収録の情報（長さなど。目安の計算に使う）

function fmtDur(sec) {
  if (sec === null || sec === undefined) return "";
  sec = Math.round(sec);
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
}

async function ensureCaps() {
  if (caps) return caps;
  // 古いサーバーが動いていると 404 になる。annotator.py は起動時に一度しか
  // 読まれないので、webui だけ新しくてサーバーが古い状態が起きうる。
  // 黙って例外で止まると「押しても何も起きない」に見えるので、必ず知らせる。
  let j;
  try {
    const r = await fetch("/api/capabilities");
    if (!r.ok) throw new Error("HTTP " + r.status);
    j = await r.json();
  } catch (e) {
    toast("サーバーが古いようです。annotator.py を再起動してください");
    return null;
  }
  caps = j.capabilities || {};
  caps.source_dir = j.source_dir || "";
  caps.models = j.models || [];
  fillModels(caps.models);
  return caps;
}

/** モデルの選択肢を models.json の内容で作る。増やすのは JSON 側の仕事。 */
function fillModels(models) {
  const sel = $("jobModel");
  const keep = sel.value;
  sel.innerHTML = "";
  for (const m of models) {
    const o = document.createElement("option");
    o.value = m.model;
    o.textContent = m.label;
    o.title = m.note || m.model;
    sel.appendChild(o);
  }
  if (keep && models.some((m) => m.model === keep)) sel.value = keep;
  $("jobModelNote").textContent =
    (models.find((m) => m.model === sel.value) || {}).note || "";
}

/** 上書きの確認。「上書き」なら "overwrite"、「両方残す」なら "keep_both"、
 *  やめたら null を返す。黙って消さないための共通ダイアログ。 */
function confirmOverwrite(title, html, opts) {
  opts = opts || {};
  return new Promise((resolve) => {
    $("confirmTitle").textContent = title;
    $("confirmBody").innerHTML = html;
    $("confirmKeep").hidden = !opts.allowKeepBoth;
    $("confirmOver").textContent = opts.overwriteLabel || "上書きする";
    // 破壊的でない確認（「割り当てますか」など）は赤くしない
    $("confirmOver").classList.toggle("danger", opts.danger !== false);
    $("confirmCancel").textContent = opts.cancelLabel || "やめる";
    $("confirmOverlay").hidden = false;
    const done = (v) => {
      $("confirmOverlay").hidden = true;
      $("confirmCancel").onclick = $("confirmKeep").onclick = $("confirmOver").onclick = null;
      resolve(v);
    };
    $("confirmCancel").onclick = () => done(null);
    $("confirmKeep").onclick = () => done("keep_both");
    $("confirmOver").onclick = () => done("overwrite");
  });
}

async function openJob() {
  if (!(await ensureCaps())) return;   // サーバーが古い等。toast 済み
  $("jobOverlay").hidden = false;
  $("jobSourceDir").value = $("jobSourceDir").value || caps.source_dir || "";
  setJobTarget(state && state.has_audio ? state.name : null);
  await loadSources();
  await pollJob();          // 走っている最中に開いたら進捗を出す
}

function closeJob() {
  $("jobOverlay").hidden = true;
  // ポーリングは止めない。処理はサーバー側で走っているので、ダイアログを
  // 閉じてアノテーションを続けられる。終わったらここで気づける。
}

async function loadSources() {
  const box = $("jobSourceList");
  box.innerHTML = '<div class="repl-empty">読み込み中…</div>';
  const r = await fetch("/api/sources?dir=" + encodeURIComponent($("jobSourceDir").value));
  const j = await r.json();
  box.innerHTML = "";
  if (j.error === "not_a_dir") {
    box.innerHTML = '<div class="repl-empty">そのディレクトリはありません。</div>';
    return;
  }
  if (!j.files.length) {
    box.innerHTML = '<div class="repl-empty">音声ファイルが見つかりません。</div>';
    return;
  }
  for (const f of j.files) {
    const row = document.createElement("div");
    row.className = "src-row";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = f.file;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = [f.duration ? fmtDur(f.duration) : "",
                        (f.size / 1048576).toFixed(1) + "MB",
                        f.exists ? "すでにある" : ""].filter(Boolean).join(" / ");
    const btn = document.createElement("button");
    btn.textContent = "追加する";
    btn.addEventListener("click", () => addSource(j.dir, f.file));
    row.appendChild(name); row.appendChild(meta); row.appendChild(btn);
    box.appendChild(row);
  }
}

async function addSource(dir, file, mode) {
  const r = await fetch("/api/add-source", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir, file, mode: mode || "ask" }),
  });
  const j = await r.json();
  if (j.error === "exists") {
    const items = (j.artifacts || []).map((a) => "<li>" + a.label + "：" + a.file + "</li>").join("");
    const choice = await confirmOverwrite(
      "同じ名前の収録があります",
      "<strong>" + j.name + "</strong> は既に取り込まれています。<br>" +
      "上書きすると、下のものが消えます。" +
      (items ? "<ul>" + items + "</ul>" : "<ul><li>音声のみ</li></ul>"),
      { allowKeepBoth: true });
    if (!choice) return;
    return addSource(dir, file, choice);
  }
  if (!j.ok) { toast("追加できませんでした"); return; }
  toast("追加しました: " + j.file);
  await loadProjectList();
  await loadSources();
  setJobTarget(j.name);
}

/** 処理の対象を決め、チェックボックスの初期状態をその収録の状況で決める。 */
async function setJobTarget(name) {
  jobTarget = name;
  jobProject = null;
  $("jobRun").hidden = !name;
  if (!name) { $("jobNote").textContent = "追加する音声を選んでください。"; return; }

  const r = await fetch("/api/project?name=" + encodeURIComponent(name));
  const p = r.ok ? await r.json() : {};
  jobProject = p;
  $("jobTarget").innerHTML = "対象: <strong>" + name + "</strong>";

  const tBox = $("jobDoTranscribe"), dBox = $("jobDoDiarize");
  // 既にあるものは既定でオフ。押した瞬間に消えるのを避ける。
  tBox.checked = !p.transcribed;
  dBox.checked = !p.diarization;
  tBox.disabled = !caps.transcribe_mlx;
  dBox.disabled = !caps.diarize;
  $("jobModel").disabled = !caps.transcribe_mlx;
  $("jobSpeakers").disabled = !caps.diarize;

  const missing = [];
  if (!caps.transcribe_mlx) missing.push("文字起こし（mlx-whisper が未導入、または Mac ではない）");
  if (!caps.diarize) missing.push("話者分離（pyannote.audio が未導入）");
  const warn = $("jobWarn");
  const notes = [];
  if (missing.length) notes.push("この環境では使えません: " + missing.join(" / "));
  if (p.transcribed) notes.push("文字起こしは既にあります。やり直すと、手で直した本文と話者ラベルが消えます。");
  if (p.diarization) notes.push("話者分離の結果は既にあります。");
  warn.innerHTML = notes.map((t) => "・" + t).join("<br>");
  warn.hidden = !notes.length;
  $("jobNote").textContent = "";
  updateEstimates(p);
}

function updateEstimates(p) {
  // 目安は実測の倍速から。文字起こしはモデルで違うので models.json の値を使う。
  const dur = (p && p.duration) || (state && state.name === jobTarget && audio.duration) || null;
  const m = (caps.models || []).find((x) => x.model === $("jobModel").value);
  $("jobTEst").textContent = dur ? "約 " + fmtDur(dur / ((m && m.speed) || 4.6)) : "";
  $("jobDEst").textContent = dur ? "約 " + fmtDur(dur / 12) : "";
}

async function startJob() {
  if (!jobTarget) { toast("対象がありません"); return; }
  const doT = $("jobDoTranscribe").checked, doD = $("jobDoDiarize").checked;
  if (!doT && !doD) { toast("実行する処理を選んでください"); return; }

  if (doT) {
    const r = await fetch("/api/artifacts?name=" + encodeURIComponent(jobTarget));
    const a = (await r.json()).artifacts || [];
    if (a.length) {
      const items = a.map((x) => "<li>" + x.label + "：" + x.file + "</li>").join("");
      const choice = await confirmOverwrite(
        "作り直すと消えるものがあります",
        "<strong>" + jobTarget + "</strong> を文字起こしし直します。<br>" +
        "下のものが作り直され、<strong>手で直した本文と話者ラベルは戻せません</strong>。" +
        "<ul>" + items + "</ul>",
        { allowKeepBoth: false, overwriteLabel: "作り直す" });
      if (!choice) return;
    }
  }

  const speakers = $("jobSpeakers").value;
  const res = await fetch("/api/job", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: jobTarget, transcribe: doT, diarize: doD,
                           speakers: speakers ? Number(speakers) : null,
                           model: $("jobModel").value || null }),
  });
  const j = await res.json();
  if (!j.ok) {
    toast(j.error === "busy" ? "別の処理が動いています: " + j.running
        : j.error === "unknown_model" ? "models.json にないモデルです"
        : "開始できませんでした");
    return;
  }
  renderJob(j);
  if (!jobPoll) jobPoll = setInterval(pollJob, 2000);
}

async function pollJob() {
  const r = await fetch("/api/job");
  const j = (await r.json()).job;
  if (!j) { $("jobProgress").hidden = true; return; }
  renderJob(j);
  if (j.status === "running" && !jobPoll) jobPoll = setInterval(pollJob, 2000);
  if (j.status !== "running" && jobPoll) {
    clearInterval(jobPoll); jobPoll = null;
    await loadProjectList();
    if (j.status === "done") {
      toast("完了しました: " + j.name);
      // 編集中の収録を横から差し替えない。別の収録を触っていたら知らせるだけ。
      if (!state || state.name === j.name || !dirty) {
        await loadProject(j.name);
        $("projectSel").value = j.name;
      }
      if (!$("jobOverlay").hidden) await setJobTarget(j.name);
      await offerAssign(j);
    } else if (j.status === "failed") {
      toast("失敗しました: " + (j.error || j.name));
    }
  }
}

/** 話者分離まで終わったら、そのまま割り当てるか聞く。
 *  黙って割り当てない。全行を振り直すので、手で付けたラベルが消えるため。 */
async function offerAssign(j) {
  const diarized = j.stages.some((s) => s.key === "diarize" && s.status === "done");
  // 別の収録を編集中なら聞かない（その収録に割り当ててしまうため）
  if (!diarized || !state || state.name !== j.name || !state.diarization) return;

  const labeled = state.segments.filter((s) => s.role).length;
  const body =
    "話者分離が終わりました。<strong>全行に話者を割り当てますか。</strong>" +
    (labeled ? "<ul><li>いま話者が付いている " + labeled +
               "行は、分離の結果で置き換わります（⌘Z で戻せます）</li></ul>"
             : "<ul><li>割り当てたあと、誰がどの話者かを「話者を登録」で照合します</li></ul>");
  const ok = await confirmOverwrite("話者を割り当てますか", body,
    { allowKeepBoth: false, overwriteLabel: "割り当てる",
      cancelLabel: "あとで", danger: false });
  if (!ok) return;
  closeJob();
  await openDiar();     // いつもの「話者を読み込む」の流れに合流する
}

/** ヘッダーのボタンに実行中を出す。ダイアログを閉じていても分かるように。 */
function renderJobBadge(j) {
  const btn = $("jobBtn");
  const running = j && j.status === "running";
  btn.classList.toggle("running", !!running);
  if (running) {
    const cur = j.stages.find((s) => s.status === "running");
    btn.textContent = (cur ? cur.label : "処理") + "中… " + fmtDur(j.elapsed);
    btn.title = j.name + " を処理しています。押すと進捗を見られます";
  } else {
    btn.textContent = "文字起こし";
    btn.title = "音声を追加して、文字起こしと話者分離を回す";
  }
}

function renderJob(j) {
  renderJobBadge(j);
  const box = $("jobProgress");
  if ($("jobOverlay").hidden) return;   // 閉じている間は中身を作らない
  box.hidden = false;
  const total = j.stages.reduce((a, s) => a + (s.estimate || 0), 0);
  const doneEst = j.stages.filter((s) => s.status === "done")
                          .reduce((a, s) => a + (s.estimate || 0), 0);
  const pct = total ? Math.min(99, Math.round(100 * Math.max(doneEst, j.elapsed) / total)) : 0;
  const mark = { waiting: "・", running: "▶", done: "✓", failed: "×" };
  const lines = j.stages.map((s) =>
    '<div class="stage">' + (mark[s.status] || "・") + " " + s.label +
    (s.status === "running" ? "（実行中）" : "") +
    (s.estimate ? " <span class='muted-sm'>約 " + fmtDur(s.estimate) + "</span>" : "") +
    "</div>").join("");
  const bar = j.status === "running"
    ? '<div class="bar"><i style="width:' + pct + '%"></i></div>' : "";
  const head = j.status === "running"
    ? "経過 " + fmtDur(j.elapsed) + (total ? " / 目安 " + fmtDur(total) : "")
    : (j.status === "done" ? "完了（" + fmtDur(j.elapsed) + "）"
                           : "失敗しました：" + (j.error || ""));
  box.innerHTML = "<div><strong>" + head + "</strong></div>" + bar + lines +
                  '<div class="log">' + (j.log || []).slice(-4).join("\n") + "</div>";
}

$("jobBtn").addEventListener("click", openJob);
$("jobClose").addEventListener("click", closeJob);
$("jobStart").addEventListener("click", startJob);
$("jobBrowse").addEventListener("click", loadSources);
$("jobModel").addEventListener("change", () => {
  const m = (caps.models || []).find((x) => x.model === $("jobModel").value) || {};
  $("jobModelNote").textContent = m.note || "";
  updateEstimates(jobProject);
});
$("jobSourceDir").addEventListener("keydown", (e) => { if (e.key === "Enter") loadSources(); });
$("jobOverlay").addEventListener("click", (e) => { if (e.target.id === "jobOverlay") closeJob(); });

// ページを開き直しても、走っている処理を拾い直す（状態はサーバーが持っている）
pollJob().catch(() => {});
