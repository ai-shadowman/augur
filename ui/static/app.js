if (typeof mermaid !== "undefined") {
  try {
    mermaid.initialize({ startOnLoad: false, theme: "neutral", suppressErrorRendering: true });
  } catch (e) {
    console.error("Failed to initialize mermaid:", e);
  }
}

let _mermaidCounter = 0;

function renderMermaidError(node, err, src) {
  const msg = (err && (err.str || err.message)) ? (err.str || err.message) : String(err || "Unknown Mermaid error");
  const box = document.createElement("div");
  box.className = "cu-diagram-err-box";
  box.innerHTML =
    `<div class="cu-diagram-err-header">\u26a0 Diagram could not be rendered</div>` +
    `<div class="cu-diagram-err-msg">${esc(msg)}</div>` +
    (src ? `<pre class="cu-diagram-err-src"><code>${esc(src)}</code></pre>` : "");
  node.replaceWith(box);
}

async function runMermaid(container) {
  if (!container) return;
  const nodes = Array.from(container.querySelectorAll("pre.mermaid")).filter(n => !n.dataset.processed);
  if (!nodes.length) return;
  if (typeof mermaid === "undefined") {
    for (const node of nodes) {
      node.dataset.processed = "true";
      renderMermaidError(node, new Error("mermaid.js is not loaded"), node.textContent || "");
    }
    return;
  }
  // Render one diagram per event-loop tick so a slow/broken diagram cannot
  // freeze the UI or block other diagrams from rendering.
  for (const node of nodes) {
    if (node.dataset.processed) continue;
    node.dataset.processed = "true";
    const originalSrc = (node.dataset.mermaidSrc || node.textContent || "").trim();
    node.dataset.mermaidSrc = originalSrc;
    if (!originalSrc) {
      renderMermaidError(node, new Error("Empty Mermaid diagram"), "");
      continue;
    }
    await new Promise(resolve => setTimeout(resolve, 16));
    const renderId = `cu-mermaid-${++_mermaidCounter}-${Date.now()}`;
    try {
      const timeout = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Mermaid rendering timed out after 5s")), 5000)
      );
      const rendered = await Promise.race([
        mermaid.render(renderId, originalSrc),
        timeout,
      ]);
      if (rendered && rendered.svg) {
        const div = document.createElement("div");
        div.className = "cu-mermaid-diagram";
        div.innerHTML = rendered.svg;
        if (typeof rendered.bindFunctions === "function") {
          rendered.bindFunctions(div);
        }
        node.replaceWith(div);
      } else {
        renderMermaidError(node, new Error("Mermaid returned empty diagram output"), originalSrc);
      }
    } catch (e) {
      document.getElementById(`d${renderId}`)?.remove();
      document.getElementById(`i${renderId}`)?.remove();
      document.getElementById(renderId)?.remove();
      console.error("Mermaid render error:", e);
      renderMermaidError(node, e, originalSrc);
    }
  }
}

const state = {
  repos: [], selected: new Set(), indexes: [], queryable: [],
  defaultKey: null, poll: null, chat: [], thinking: null,
  animateLatest: false, allRuns: [], runPage: 0, selectedRunId: null,
  runsRefreshTimer: null,
  namespace: (() => { try { return sessionStorage.getItem("cu-namespace") || ""; } catch { return ""; } })(),
};

const $ = (id) => document.getElementById(id);
function hardRefresh() {
  if (window.location.protocol === "about:") {
    if (window.parent && window.parent !== window) {
      try {
        window.parent.postMessage({ type: "cu-reload" }, window.location.origin);
        return;
      } catch {}
    }
    load();
    return;
  }
  const u = new URL(window.location.href);
  u.searchParams.set("_t", String(Date.now()));
  window.location.replace(u.toString());
}
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[ch]));

function inlineMd(raw) {
  const codes = [];
  let text = String(raw ?? "").replace(/`([^`]+)`/g, (_, code) => {
    codes.push(`<code>${esc(code)}</code>`);
    return `%%C${codes.length - 1}%%`;
  });
  text = esc(text);
  text = text.replace(
    /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  text = text.replace(
    /(^|[^"'>=])(https?:\/\/[^\s<]+)/g,
    '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>'
  );
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^*])\*(?!\s)([^*]+?)\*(?!\*)/g, "$1<em>$2</em>");
  return text.replace(/%%C(\d+)%%/g, (_, i) => codes[Number(i)]);
}

function parseTableRow(line) {
  const inner = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return inner.split("|").map(c => c.trim());
}

function sanitizeMermaid(content) {
  return content
    .split("\n").map(l => {
      if (/^\s*%(?!%)/.test(l)) return l.replace(/^(\s*)%/, "$1%%"); // lone % → %%
      return l.replace(/\s*%%.*$/, "");  // strip ALL %% comments (standalone and inline)
    }).join("\n")
    .replace(/\[([^\[\]"\n]+)\]/g, (m, label) => /[()/:@]/.test(label) ? `["${label}"]` : m);
}

function renderMarkdown(src) {
  const fences = [];
  let text = String(src ?? "").replace(/\r\n/g, "\n");
  text = text.replace(/```([\w+-]*)[ \t]*\n?([\s\S]*?)```/g, (_, lang, code) => {
    const content = code.replace(/\n$/, "");
    const isMermaid = lang.toLowerCase() === "mermaid" ||
      (!lang && /^\s*(?:graph|flowchart)\s+(?:TD|TB|BT|RL|LR)\b/m.test(content));
    if (isMermaid) {
      fences.push(`<pre class="mermaid">${esc(sanitizeMermaid(content))}</pre>`);
    } else {
      fences.push(`<pre><code>${esc(content)}</code></pre>`);
    }
    return `\n\n%%FENCE${fences.length - 1}%%\n\n`;
  });
  const lines = text.split("\n");
  const html = [];
  let i = 0;
  const isFence = (line) => line.trim().match(/^%%FENCE(\d+)%%$/);
  const isMermaidStart = (line) => /^\s*(?:graph|flowchart)\s+(?:TD|TB|BT|RL|LR)\b/.test(line);
  const isList = (line) => /^\s*[-*] /.test(line);
  const isOl = (line) => /^\s*\d+\. /.test(line);
  const isHeading = (line) => /^#{1,6}\s+/.test(line);
  const isStep = (line) => /^\*\*\d+\.\s+.+\*\*$/.test(line.trim());
  const isTableRow = (line) => /\|/.test(line.trim()) && line.trim().length > 1;
  const isTableSep = (line) => {
    const trimmed = line.trim();
    if (!trimmed.includes("-") || !trimmed.includes("|")) return false;
    const cells = trimmed.replace(/^\|/, "").replace(/\|$/, "").split("|");
    return cells.length > 0 && cells.every(c => /^\s*:?-+[:]?\s*$/.test(c));
  };
  const isTableStart = (idx) =>
    idx < lines.length &&
    isTableRow(lines[idx]) &&
    idx + 1 < lines.length &&
    isTableSep(lines[idx + 1]);
  while (i < lines.length) {
    const prevI = i;
    const line = lines[i];
    const fence = isFence(line);
    if (fence) { html.push(fences[Number(fence[1])]); i += 1; continue; }
    if (!line.trim()) { i += 1; continue; }
    if (isMermaidStart(line)) {
      const mLines = [line];
      i += 1;
      while (
        i < lines.length &&
        !isHeading(lines[i]) &&
        !isFence(lines[i]) &&
        !/^---+$/.test(lines[i].trim())
      ) {
        if (!lines[i].trim()) {
          if (
            i + 1 < lines.length &&
            /^\s+(?:\w+|subgraph\b|end\b|style\b|classDef\b|class\b|linkStyle\b|direction\b)/.test(lines[i + 1])
          ) {
            i += 1;
            continue;
          }
          break;
        }
        mLines.push(lines[i]);
        i += 1;
      }
      html.push(`<pre class="mermaid">${esc(sanitizeMermaid(mLines.join("\n")))}</pre>`);
      continue;
    }
    if (/^---+$/.test(line.trim())) { html.push("<hr/>"); i += 1; continue; }
    if (isHeading(line)) {
      const level = Math.min(6, line.match(/^#+/)[0].length);
      html.push(`<h${level}>${inlineMd(line.replace(/^#+\s+/, ""))}</h${level}>`);
      i += 1; continue;
    }
    if (isStep(line)) {
      html.push(`<h3>${inlineMd(line.trim())}</h3>`);
      i += 1; continue;
    }
    if (isList(line)) {
      html.push("<ul>");
      while (i < lines.length && isList(lines[i])) {
        html.push(`<li>${inlineMd(lines[i].replace(/^\s*[-*] /, ""))}</li>`);
        i += 1;
      }
      html.push("</ul>");
      continue;
    }
    if (isOl(line)) {
      html.push("<ol>");
      while (i < lines.length && isOl(lines[i])) {
        html.push(`<li>${inlineMd(lines[i].replace(/^\s*\d+\. /, ""))}</li>`);
        i += 1;
      }
      html.push("</ol>");
      continue;
    }
    if (isTableStart(i)) {
      const headers = parseTableRow(line);
      i += 2;
      let tbl = `<table class="cu-md-table"><thead><tr>`;
      headers.forEach(h => { tbl += `<th>${inlineMd(h)}</th>`; });
      tbl += `</tr></thead><tbody>`;
      while (i < lines.length && isTableRow(lines[i]) && !isFence(lines[i])) {
        const cells = parseTableRow(lines[i]);
        tbl += `<tr>`;
        cells.forEach(c => { tbl += `<td>${inlineMd(c)}</td>`; });
        tbl += `</tr>`;
        i += 1;
      }
      tbl += `</tbody></table>`;
      html.push(tbl);
      continue;
    }
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !isHeading(lines[i]) &&
      !isList(lines[i]) &&
      !isOl(lines[i]) &&
      !isFence(lines[i]) &&
      !isMermaidStart(lines[i]) &&
      !isStep(lines[i]) &&
      !isTableStart(i) &&
      !/^---+$/.test(lines[i].trim())
    ) {
      para.push(lines[i]);
      i += 1;
    }
    if (i === prevI) {
      para.push(lines[i]);
      i += 1;
    }
    html.push(`<p>${inlineMd(para.join(" "))}</p>`);
  }
  return `<div class="cu-md">${html.join("")}</div>`;
}

function parseCsv(text) {
  const rows = [];
  let row = [], field = "", inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') { inQuotes = false; }
      else { field += ch; }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ',') {
        row.push(field.trim()); field = "";
      } else if (ch === '\r' && text[i + 1] === '\n') {
        row.push(field.trim()); field = "";
        if (row.some(f => f !== "")) rows.push(row);
        row = []; i++;
      } else if (ch === '\n') {
        row.push(field.trim()); field = "";
        if (row.some(f => f !== "")) rows.push(row);
        row = [];
      } else {
        field += ch;
      }
    }
  }
  row.push(field.trim());
  if (row.some(f => f !== "")) rows.push(row);
  return rows;
}

let _typewriterTimer = null;

function typewriter(el, fullText, onDone) {
  if (_typewriterTimer) { clearInterval(_typewriterTimer); _typewriterTimer = null; }
  const tokens = fullText.split(/(\s+)/);
  const chunkSize = Math.max(1, Math.ceil(tokens.length / 80));
  let idx = 0;
  _typewriterTimer = setInterval(() => {
    idx = Math.min(idx + chunkSize, tokens.length);
    el.innerHTML = renderMarkdown(tokens.slice(0, idx).join(""));
    $("slider-chat-log").scrollTop = $("slider-chat-log").scrollHeight;
    if (idx >= tokens.length) {
      clearInterval(_typewriterTimer);
      _typewriterTimer = null;
      runMermaid(el);
      if (onDone) onDone();
    }
  }, 30);
}

const banner = (msg, danger) => {
  const target = $("banner");
  target.setAttribute("role", danger ? "alert" : "status");
  target.setAttribute("aria-live", danger ? "assertive" : "polite");
  target.innerHTML = msg
    ? `<div class="cu-note" style="${danger ? "" : "background:#e7f1fa;border-left-color:#0066cc"}">${esc(msg)}</div>`
    : "";
};

function setThinking(info) {
  state.thinking = info;
  const bar = $("thinking-bar");
  if (!info) {
    bar.classList.add("hidden");
    $("slider-send").disabled = false;
    $("start-analysis").disabled = false;
    return;
  }
  bar.classList.remove("hidden");
  $("thinking-label").textContent = info.label || "Thinking";
  $("thinking-status").textContent = info.status || "Working on your request…";
  $("slider-send").disabled = true;
  $("start-analysis").disabled = true;
}

function getCookie(name) {
  const matchIn = (raw) => (raw || "").match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
  let m = matchIn(document.cookie);
  if (!m && window.parent && window.parent !== window) {
    try { m = matchIn(window.parent.document.cookie); } catch {}
  }
  return m ? decodeURIComponent(m[1]) : "";
}
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const csrf = getCookie("csrf-token");
  if (csrf) headers["X-CSRFToken"] = csrf;
  if (state.namespace) headers["X-CU-Namespace"] = state.namespace;
  const relPath = String(path || "").replace(/^\/+/, "");
  const res = await fetch(relPath, { credentials: "same-origin", ...opts, headers });
  const text = await res.text();
  if (!res.ok) throw new Error(text || res.statusText);
  return text ? JSON.parse(text) : {};
}
function shortName(url) { return url.replace(/\/$/, "").split("/").pop(); }
function repoKey(r) { return `${r.git_repo}|${r.git_branch}`; }
function selectedRepos() { return state.repos.filter((r) => state.selected.has(repoKey(r))); }
function gitSlug(url, branch) {
  try {
    const path = new URL(url).pathname.replace(/^\//, "").replace(/\.git$/, "");
    const parts = path.split("/").filter(Boolean);
    const name = parts.pop() || "repo";
    const owner = parts.pop() || "";
    return `${owner}-${name}-${branch}`.slice(0, 255);
  } catch {
    return `${url}-${branch}`;
  }
}
function prettyRelativeDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  const now = new Date();
  const isToday = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
  if (isToday) {
    const diffMs = now - d;
    const hours = Math.floor(diffMs / 3600000);
    const mins = Math.floor(diffMs / 60000);
    if (hours >= 1) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    if (mins >= 1) return `${mins} min${mins === 1 ? "" : "s"} ago`;
    return "just now";
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function prettyDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function showTab(name) {
  const highlightTab = name === "analysis" ? "runs" : name;
  document.querySelectorAll(".cu-nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === highlightTab);
  });
  ["repos", "runs", "analysis"].forEach((tab) => {
    $("tab-" + tab).classList.toggle("hidden", tab !== name);
  });
  if (name === "analysis") {
    const el = $("tab-analysis");
    el.classList.remove("cu-animate-in");
    void el.offsetWidth;
    el.classList.add("cu-animate-in");
  }
}

function buildRunDetailUrl(runId, gitRepo, gitBranch, projectSlug) {
  const qs = new URLSearchParams();
  if (gitRepo) {
    qs.set("git_repo", gitRepo);
    if (gitBranch) qs.set("git_branch", gitBranch);
  } else if (projectSlug) {
    qs.set("multi_repo", "true");
    qs.set("project_slug", projectSlug);
  }
  const query = qs.toString();
  return `/api/v2/pipelines/runs/${runId}${query ? `?${query}` : ""}`;
}

function formatReportHeading(gitRepo, gitBranch, projectSlug) {
  return gitRepo
    ? `${shortName(gitRepo).replace(/\.git$/, "")} @ ${gitBranch || "main"}`
    : projectSlug
      ? `Combined repos (${projectSlug})`
      : "Combined repos";
}

function downloadBlob(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function downloadReportAsPdf(markdown, baseFilename, title = "", subtitle = "") {
  const staging = document.createElement("div");
  staging.className = "cu-md";
  staging.style.cssText = "position:fixed;left:-10000px;top:0;width:760px;opacity:0;pointer-events:none";
  document.body.appendChild(staging);
  let renderedHtml = "";
  try {
    try {
      staging.innerHTML = renderMarkdown(markdown);
      await runMermaid(staging);
    } catch (err) {
      staging.innerHTML = `<pre><code>${esc(markdown)}</code></pre>`;
    }
    renderedHtml = staging.innerHTML;
  } finally {
    staging.remove();
  }

  document.getElementById("cu-pdf-print-frame")?.remove();
  const frame = document.createElement("iframe");
  frame.id = "cu-pdf-print-frame";
  frame.setAttribute("aria-hidden", "true");
  frame.style.cssText = "position:fixed;right:0;bottom:0;width:1px;height:1px;opacity:0;pointer-events:none;border:0";
  document.body.appendChild(frame);

  const safeTitle = String(baseFilename || "analysis_report").replace(/\.pdf$/i, "");
  const headerHtml = (title || subtitle)
    ? `<header class="pdf-meta">` +
        (title ? `<div class="pdf-meta-title">${esc(title)}</div>` : "") +
        (subtitle ? `<div class="pdf-meta-sub">${esc(subtitle)}</div>` : "") +
      `</header>`
    : "";

  const doc = frame.contentDocument || frame.contentWindow.document;
  doc.open();
  doc.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(safeTitle)}</title><style>
    @page { size: auto; margin: 14mm 14mm 16mm; }
    * { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    html, body { margin: 0; padding: 0; background: #fff; color: #151515; font: 10.5pt/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .pdf-meta { border-bottom: 2px solid #ee0000; padding-bottom: 6pt; margin-bottom: 14pt; }
    .pdf-meta-title { font-size: 14pt; font-weight: 700; color: #151515; }
    .pdf-meta-sub { font-size: 9.5pt; color: #6a6e73; margin-top: 2pt; }
    .cu-md { line-height: 1.55; overflow-wrap: anywhere; }
    .cu-md > *:first-child { margin-top: 0; }
    h1, h2, h3, h4, h5, h6 { color: #151515; font-weight: 600; line-height: 1.3; margin: 14pt 0 5pt; break-after: avoid; page-break-after: avoid; }
    h1 { font-size: 15.5pt; }
    h2 { font-size: 13pt; border-bottom: 1px solid #d2d2d2; padding-bottom: 3pt; }
    h3 { font-size: 11.5pt; }
    h4, h5, h6 { font-size: 10.5pt; }
    p { margin: 5pt 0; }
    ul, ol { margin: 4pt 0 8pt; padding-left: 18pt; }
    li { margin: 2.5pt 0; }
    a { color: #0066cc; text-decoration: underline; }
    hr { border: 0; border-top: 1px solid #d2d2d2; margin: 12pt 0; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 8.8pt; background: #f0f0f0; padding: 1pt 3pt; border-radius: 3px; }
    pre { background: #f6f8fa; color: #151515; border: 1px solid #d2d2d2; border-radius: 4px; padding: 8pt 10pt; font-size: 8.5pt; line-height: 1.4; white-space: pre-wrap; word-break: break-word; margin: 8pt 0; }
    pre code { background: none; padding: 0; font-size: inherit; }
    .cu-md-table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 8.8pt; word-break: break-word; }
    .cu-md-table thead { display: table-header-group; }
    .cu-md-table tr { break-inside: avoid; page-break-inside: avoid; }
    .cu-md-table th, .cu-md-table td { border: 1px solid #d2d2d2; padding: 4pt 6pt; text-align: left; vertical-align: top; }
    .cu-md-table th { background: #f0f4f8; font-weight: 600; }
    .cu-md-table tr:nth-child(even) td { background: #f9fbfd; }
    .cu-mermaid-diagram { margin: 10pt 0; text-align: center; break-inside: avoid; page-break-inside: avoid; }
    .cu-mermaid-diagram svg { max-width: 100% !important; height: auto !important; }
    .cu-diagram-err-box { margin: 8pt 0; border: 1px solid #f0ab00; border-left: 4px solid #f0ab00; background: #fffbe6; padding: 8pt 10pt; break-inside: avoid; page-break-inside: avoid; }
    .cu-diagram-err-header { color: #8a5d00; font-weight: 700; font-size: 9.5pt; margin-bottom: 3pt; }
    .cu-diagram-err-msg { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 8.5pt; white-space: pre-wrap; }
  </style></head><body>${headerHtml}<article class="cu-md">${renderedHtml}</article></body></html>`);
  doc.close();

  await new Promise((resolve) => setTimeout(resolve, 60));
  const prevTitle = document.title;
  document.title = safeTitle;
  let prevTopTitle = null;
  try {
    if (window.top && window.top !== window && window.top.document) {
      prevTopTitle = window.top.document.title;
      window.top.document.title = safeTitle;
    }
  } catch {}
  const restoreTitles = () => {
    if (document.title === safeTitle) document.title = prevTitle;
    if (prevTopTitle !== null) {
      try {
        if (window.top.document.title === safeTitle) window.top.document.title = prevTopTitle;
      } catch {}
    }
  };
  const win = frame.contentWindow;
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    restoreTitles();
    setTimeout(() => frame.remove(), 500);
  };
  win.addEventListener("afterprint", cleanup, { once: true });
  win.focus();
  win.print();
  setTimeout(restoreTitles, 2000);
}

async function openReport(runId, runName, gitRepo, gitBranch, projectSlug) {
  const snap = await api(buildRunDetailUrl(runId, gitRepo, gitBranch, projectSlug));
  const reportHeading = formatReportHeading(gitRepo, gitBranch, projectSlug);
  $("report-viewer-title").textContent = reportHeading;
  $("report-viewer-subtitle").textContent = runName;

  const reports = { analysis: snap.analysis_report, evaluation: snap.evaluation_report };

  function appendReportDl(rawContent, filename) {
    const isPdf = filename.endsWith(".pdf");
    const footer = document.createElement("div");
    footer.className = "cu-bubble-footer";
    const label = isPdf ? "Download report as PDF" : "Download report";
    footer.innerHTML = `<button class="cu-bubble-dl-btn" title="${label}" aria-label="${label}">${iconDownload}</button>`;
    const dlBtn = footer.querySelector("button");
    dlBtn.onclick = async () => {
      if (isPdf) {
        dlBtn.disabled = true;
        try {
          await downloadReportAsPdf(rawContent, filename, reportHeading, runName);
        } catch (e) {
          banner(String(e.message || e), true);
        } finally {
          dlBtn.disabled = false;
        }
        return;
      }
      downloadBlob(rawContent, filename, "text/csv");
    };
    $("report-viewer-content").appendChild(footer);
  }

  function showReportTab(type) {
    $("tab-analysis").querySelectorAll(".cu-report-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.report === type);
    });
    const content = reports[type];
    if (type === "evaluation") {
      $("report-viewer-content").innerHTML = "";
      if (!content) {
        $("report-viewer-content").innerHTML = `<p class="cu-muted">No evaluation report available.</p>`;
      } else {
        const csvRows = parseCsv(content);
        if (csvRows.length < 2) {
          $("report-viewer-content").innerHTML = `<p class="cu-muted">No evaluation data.</p>`;
        } else {
          const ANALYSIS_EVALUATION_THRESHOLD = 3;
          const headers = csvRows[0];
          const scoreColIndices = headers.reduce((acc, h, i) => {
            if (h.includes("/v1/score")) acc.push(i);
            return acc;
          }, []);
          const failColIndices = headers.reduce((acc, h, i) => {
            if (h.includes("/v1/score") && !h.includes("answer_similarity")) acc.push(i);
            return acc;
          }, []);
          /* ── Aggregate score summary (server-provided) ── */
          const scores = snap.evaluation_scores;
          if (scores && scores.metrics && scores.metrics.length > 0) {
            const heading = document.createElement("h3");
            heading.style.cssText = "margin:0 0 0.75rem";
            heading.textContent = "Summary";
            $("report-viewer-content").appendChild(heading);
            const summary = document.createElement("div");
            summary.className = "cu-eval-summary";
            summary.innerHTML =
              `<div class="cu-eval-summary-section">` +
                `<h4>Per-Metric Averages (1-5)</h4>` +
                `<div class="cu-eval-chips">` +
                  scores.metrics.map(m =>
                    `<span class="cu-eval-chip"><strong>${esc(m.name)}</strong> ${m.average.toFixed(2)}</span>`
                  ).join("") +
                `</div>` +
              `</div>` +
              `<div class="cu-eval-summary-section">` +
                `<h4>Pass Rate (≥ 3)</h4>` +
                `<div class="cu-eval-chips">` +
                  scores.metrics.filter(m => m.pass_rate_3 != null).map(m =>
                    `<span class="cu-eval-chip"><strong>${esc(m.name)}</strong> ${m.pass_rate_3.toFixed(1)}%</span>`
                  ).join("") +
                  `<span class="cu-eval-chip cu-eval-chip--overall"><strong>Mean</strong> ${scores.average_pass_rate_3.toFixed(1)}%</span>` +
                `</div>` +
              `</div>` +
              `<div class="cu-eval-summary-section">` +
                `<h4>Pass Rate (≥ 4)</h4>` +
                `<div class="cu-eval-chips">` +
                  scores.metrics.filter(m => m.pass_rate_4 != null).map(m =>
                    `<span class="cu-eval-chip"><strong>${esc(m.name)}</strong> ${m.pass_rate_4.toFixed(1)}%</span>`
                  ).join("") +
                  `<span class="cu-eval-chip cu-eval-chip--overall"><strong>Mean</strong> ${scores.average_pass_rate_4.toFixed(1)}%</span>` +
                `</div>` +
              `</div>`;
            $("report-viewer-content").appendChild(summary);
          }

          const gridContainer = document.createElement("div");
          $("report-viewer-content").appendChild(gridContainer);
          new gridjs.Grid({
            columns: headers.map((h, colIdx) => {
              const maxLen = csvRows.reduce((max, row) => Math.max(max, (row[colIdx] || "").length), 0);
              return {
                name: h,
                resizable: true,
                width: `${Math.max(80, Math.min(800, maxLen * 8))}px`,
                formatter: (cell) => {
                  const str = cell == null ? "" : String(cell);
                  if (str.length <= 1200) return str;
                  return gridjs.html(
                    `<span>` +
                    `${esc(str.slice(0, 1200))}` +
                    `<span class="cu-cell-rest" style="display:none">${esc(str.slice(1200))}</span>` +
                    `<a href="#" class="cu-cell-toggle">More</a>` +
                    `</span>`
                  );
                },
              };
            }),
            data: csvRows.slice(1).map(row => {
              const r = row.slice(0, headers.length);
              while (r.length < headers.length) r.push("");
              return r;
            }),
            sort: true,
            search: false,
            pagination: { limit: 20 },
            style: { table: { fontSize: "0.875rem" } },
          }).render(gridContainer);
          gridContainer.addEventListener("click", (e) => {
            const toggle = e.target.closest(".cu-cell-toggle");
            if (!toggle) return;
            e.preventDefault();
            const rest = toggle.previousElementSibling;
            if (!rest) return;
            const hidden = rest.style.display === "none";
            rest.style.display = hidden ? "inline" : "none";
            toggle.textContent = hidden ? "Less" : "More";
          });

          /* ── Highlight rows where any score (except answer_similarity) < threshold ── */
          function highlightFailRows() {
            gridContainer.querySelectorAll("tbody tr").forEach(tr => {
              const tds = tr.querySelectorAll("td");
              const fail = failColIndices.some(i => {
                const td = tds[i];
                if (!td) return false;
                return parseFloat(td.textContent) < ANALYSIS_EVALUATION_THRESHOLD;
              });
              tr.classList.toggle("cu-row-fail", fail);
            });
          }
          const obs = new MutationObserver(highlightFailRows);
          obs.observe(gridContainer, { childList: true, subtree: true });
          setTimeout(highlightFailRows, 200);
          appendReportDl(content, `${runName || "report"}_evaluation_report.csv`);
        }
      }
    } else {
      if (!content) {
        $("report-viewer-content").innerHTML = `<p class="cu-muted">No analysis report available.</p>`;
      } else {
        try {
          $("report-viewer-content").innerHTML = renderMarkdown(content);
          runMermaid($("report-viewer-content"));
        } catch (err) {
          console.error("Failed to render analysis report:", err);
          const errMsg = (err && err.message) ? err.message : String(err);
          $("report-viewer-content").innerHTML =
            `<div class="cu-diagram-err-box">` +
            `<div class="cu-diagram-err-header">\u26a0 Report rendering error</div>` +
            `<div class="cu-diagram-err-msg">${esc(errMsg)}</div>` +
            `</div>` +
            `<pre><code>${esc(content)}</code></pre>`;
        }
        appendReportDl(content, `${runName || "report"}_analysis_report.pdf`);
      }
    }
  }

  $("tab-analysis").querySelectorAll(".cu-report-tab").forEach((tab) => {
    tab.onclick = () => showReportTab(tab.dataset.report);
  });

  showTab("analysis");
  showReportTab("analysis");
}


const iconExpand = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="11" y1="9" x2="8" y2="9"/></svg>`;
const iconDownload = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;

function runListCard(run) {
  const succeeded = run.status === "Succeeded" || run.status === "Skipped";
  const isMulti = Boolean(run.multi_repo) || (run.name || "").startsWith("multi_repo");
  const projectSlug = run.project_slug || "";
  const title = isMulti
    ? (projectSlug ? `Combined repos (${projectSlug})` : "Combined repos")
    : run.git_repo ? shortName(run.git_repo).replace(/\.git$/, "") : (run.git_slug || run.name);
  const started = run.start_time ? esc(prettyRelativeDate(run.start_time)) : "—";
  const storedRepos = run.repos || null;
  const repoSummary = storedRepos && storedRepos.length
    ? `${storedRepos.map((r) => `${esc(shortName(r.git_repo))} @ ${esc(r.git_branch)}`).join(", ")}`
    : isMulti
      ? `<i>Combined repos</i>`
      : run.git_repo
        ? `${esc(shortName(run.git_repo))} @ ${esc(run.git_branch || "")}`
        : "";
  const helpIcon = repoSummary
    ? `<span class="cu-help" style="flex-shrink:0"><button type="button" class="cu-help-btn" aria-label="Repositories"><svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg></button><span class="cu-help-popover" role="tooltip">${repoSummary}</span></span>`
    : "";
  const actions = succeeded
    ? `<div class="cu-run-list-actions">
        <button type="button" class="cu-run-expand" data-run-id="${esc(run.run_id)}" data-run-name="${esc(run.name)}" data-git-repo="${esc(run.git_repo || "")}" data-git-branch="${esc(run.git_branch || "")}" data-project-slug="${esc(projectSlug)}" title="View analysis report">${iconExpand}</button>
        <button type="button" class="cu-run-download" data-run-id="${esc(run.run_id)}" data-run-name="${esc(run.name)}" data-git-repo="${esc(run.git_repo || "")}" data-git-branch="${esc(run.git_branch || "")}" data-project-slug="${esc(projectSlug)}" title="Download analysis report">${iconDownload}</button>
      </div>`
    : "";
  const repoList = storedRepos && storedRepos.length
    ? storedRepos.map((r) => esc(r.git_repo))
    : isMulti
      ? []
      : run.git_repo
        ? [esc(run.git_repo)]
        : [];
  const visibleCount = isMulti ? 1 : 2;
  const visibleRepos = repoList.slice(0, visibleCount).join("<br/>");
  const hiddenRepos = repoList.length > visibleCount
    ? `<span class="cu-repo-overflow" style="display:none">${repoList.slice(visibleCount).map((r) => "<br/>" + r).join("")}</span>`
      + `<br/><a href="#" class="cu-repo-toggle" data-total="${repoList.length}" style="font-size:0.8rem">${isMulti ? `Show all (${repoList.length})` : `More (${repoList.length - visibleCount})`}</a>`
    : "";
  const repoCell = repoList.length
    ? visibleRepos + hiddenRepos
    : isMulti ? "<i>Combined repos</i>" : "";
  const inProgress = run.status === "Running" || run.status === "Pending";
  const stageLabel = inProgress
    ? (run.current_stage || (run.status === "Pending" ? "Queued" : "Starting…"))
    : "";
  const stageHtml = stageLabel
    ? `<div class="cu-run-stage">Stage: <span class="cu-run-stage-name">${esc(stageLabel)}</span></div>`
    : "";
  return `<tr>
    <td>
      <div style="display:flex;align-items:center;gap:0.75rem">
        <input type="checkbox" class="cu-check cu-radio cu-run-select" data-run-id="${esc(run.run_id)}" data-run-name="${esc(run.name)}" data-title="${esc(title)}" data-git-repo="${esc(run.git_repo || "")}" data-git-branch="${esc(run.git_branch || "")}" data-project-slug="${esc(projectSlug)}" data-succeeded="${succeeded}"/>
        <div>
          <span class="cu-index-title">${esc(title)}</span>
          <div class="cu-index-owner" style="font-size:0.8rem">${esc(run.name || "")}</div>
        </div>
      </div>
    </td>
    <td><div class="cu-run-repo-cell${stageHtml ? " has-stage" : ""}"><span class="cu-job-dot ${esc(run.status)}"></span><div class="cu-run-repo-info"><span class="cu-muted" style="font-size:0.85rem">${repoCell}</span>${stageHtml}</div></div></td>
    <td><span class="cu-chip ${succeeded ? "indexed" : (run.status === "Failed" || run.status === "Cancelled") ? "failed" : ""}">${esc(run.status)}</span></td>
    <td><span class="cu-muted" style="font-size:0.8rem">${started}</span></td>
    <td style="text-align:center">${succeeded ? `<button type="button" class="cu-btn-icon cu-run-chat" data-git-repo="${esc(run.git_repo || '')}" data-git-branch="${esc(run.git_branch || '')}" data-multi-repo="${isMulti}" data-project-slug="${esc(projectSlug)}" data-run-title="${esc(title)}" data-run-name="${esc(run.name || '')}" data-tooltip="Explore code"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></button>` : ""}</td>
  </tr>`;
}

function updateRunSelectionNote(cb) {
  const note = $("run-selection-note");
  if (cb) {
    const owner = cb.dataset.runName || "";
    const title = cb.dataset.title || "";
    const label = owner ? `${esc(owner)} @ ${esc(title)}` : esc(title);
    note.innerHTML = `<span class="cu-note-detail">${label}</span>`;
    note.classList.remove("hidden");
  } else {
    note.innerHTML = `<span class="cu-note-detail"><i>None selected</i></span>`;
  }
  note.classList.remove("cu-note-expanded");
  note.classList.toggle("cu-note-overflowing", note.scrollWidth > note.clientWidth);
}

function applySelectedRunControls(cb) {
  const selected = Boolean(cb && cb.checked);
  const succeeded = selected && cb.dataset.succeeded === "true";
  state.selectedRunId = selected ? (cb.dataset.runId || null) : null;
  for (const id of ["run-view-report", "run-download-report", "run-download-md-report", "run-rerun"]) {
    const btn = $(id);
    btn.disabled = !(selected && succeeded);
    btn.dataset.runId = selected ? cb.dataset.runId : "";
    btn.dataset.runName = selected ? cb.dataset.runName : "";
    btn.dataset.gitRepo = selected ? cb.dataset.gitRepo : "";
    btn.dataset.gitBranch = selected ? cb.dataset.gitBranch : "";
    btn.dataset.projectSlug = selected ? (cb.dataset.projectSlug || "") : "";
  }
  updateRunSelectionNote(selected ? cb : null);
}

function bindRunCards(container) {
  const root = container || document;
  root.querySelectorAll(".cu-run-select").forEach((cb) => {
    cb.addEventListener("change", () => {
      document.querySelectorAll(".cu-run-select").forEach((other) => {
        if (other !== cb) other.checked = false;
      });
      applySelectedRunControls(cb.checked ? cb : null);
    });
  });
  root.querySelectorAll(".cu-repo-toggle").forEach((link) => {
    const totalCount = parseInt(link.dataset.total || "0", 10);
    const collapsedLabel = link.textContent;
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const overflow = link.parentElement.querySelector(".cu-repo-overflow");
      const visible = overflow.style.display !== "none";
      overflow.style.display = visible ? "none" : "inline";
      link.textContent = visible ? collapsedLabel : "Show less";
    });
  });
  root.querySelectorAll(".cu-run-expand").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const origContent = btn.innerHTML;
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.innerHTML = `<svg style="animation:cu-spin 1s linear infinite" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>`;
      try {
        await openReport(btn.dataset.runId, btn.dataset.runName, btn.dataset.gitRepo, btn.dataset.gitBranch, btn.dataset.projectSlug || "");
      } catch (e) {
        banner(String(e.message || e), true);
      } finally {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        btn.innerHTML = origContent;
      }
    });
  });
  root.querySelectorAll(".cu-run-download").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const snap = await api(
          buildRunDetailUrl(
            btn.dataset.runId,
            btn.dataset.gitRepo || "",
            btn.dataset.gitBranch || "",
            btn.dataset.projectSlug || "",
          )
        );
        const report = snap.analysis_report;
        if (!report) { banner("No analysis report available.", true); return; }
        const heading = formatReportHeading(
          btn.dataset.gitRepo || "",
          btn.dataset.gitBranch || "",
          btn.dataset.projectSlug || "",
        );
        await downloadReportAsPdf(
          report,
          `${btn.dataset.runName || btn.dataset.runId}_analysis_report.pdf`,
          heading,
          btn.dataset.runName || btn.dataset.runId,
        );
      } catch (e) {
        banner(String(e.message || e), true);
      }
    });
  });
  root.querySelectorAll(".cu-run-chat").forEach((btn) => {
    btn.addEventListener("click", () => {
      openChatSlider(
        btn.dataset.gitRepo || "",
        btn.dataset.gitBranch || "",
        btn.dataset.multiRepo === "true",
        btn.dataset.runTitle || "",
        btn.dataset.runName || "",
        btn.dataset.projectSlug || ""
      );
    });
  });
}

function jobCard(job) {
  const kind = (job.name || "").startsWith("cu-query-") ? "Query" : "Pipeline";
  return `<article class="cu-job-card">
    <span class="cu-job-dot ${esc(job.status)}"></span>
    <div>
      <div class="cu-job-kind">${esc(kind)}</div>
      <div class="cu-job-name">${esc(job.name)}</div>
    </div>
    <span class="cu-chip ${job.status === "Succeeded" || job.status === "Skipped" ? "indexed" : ""}">${esc(job.status)}</span>
  </article>`;
}

function renderRepos() {
  $("repo-list").innerHTML = state.repos.map((r) => {
    const key = repoKey(r);
    const slug = (shortName(r.git_repo) || "").replace(/\.git$/, "");
    const indexed = state.indexes.some((i) => !i.multi_repo && (i.git_slug || "").includes(slug));
    const chips = [
      `<span class="cu-chip">${esc(r.git_branch)}</span>`,
      indexed ? '<span class="cu-chip indexed">indexed</span>' : "",
    ].join("");
    return `<label class="cu-repo">
      <input class="cu-check" type="checkbox" data-key="${esc(key)}" ${state.selected.has(key) ? "checked" : ""}/>
      <span class="cu-repo-body">
        <span class="cu-repo-title">${esc(slug || r.git_repo)}</span>
        <span class="cu-repo-url-row">
          <span class="cu-repo-url">${esc(r.git_repo)}</span>
          <a class="cu-repo-link" href="${esc(r.git_repo)}" target="_blank" rel="noopener noreferrer" title="Open repository"><svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg></a>
        </span>
      </span>
      <span class="cu-chips">${chips}</span>
    </label>`;
  }).join("");
  $("repo-list").querySelectorAll("input").forEach((el) => {
    el.addEventListener("change", () => {
      if (el.checked) state.selected.add(el.dataset.key); else state.selected.delete(el.dataset.key);
      renderSelection();
    });
  });
  $("repo-list").querySelectorAll(".cu-repo-link").forEach((link) => {
    link.addEventListener("click", (e) => e.stopPropagation());
  });
  renderSelection();
}
function renderSelection() {
  const n = selectedRepos().length;
  const mode = n > 1 ? "multi-repo" : "single-repo";
  if (n > 0 && $("banner").dataset.reason === "no-selection") {
    banner("");
    delete $("banner").dataset.reason;
  }
  const repoSummary = n
    ? `<span class="cu-note-detail">${selectedRepos().map((r) => `${esc(shortName(r.git_repo))} @ ${esc(r.git_branch)}`).join(", ")}</span>`
    : "<span class=\"cu-note-detail\"><i>None selected</i></span>";
  const note = $("selection-note");
  note.classList.remove("cu-note-expanded");
  note.innerHTML = repoSummary;
  note.classList.toggle("cu-note-overflowing", note.scrollWidth > note.clientWidth);
  $("open-analyze").disabled = n === 0;
}
function renderChatSelect() {
  $("slider-chat-repo").innerHTML = state.queryable.map((o) =>
    `<option value="${esc(o.key)}">${esc(o.label)}</option>`
  ).join("");
}
function renderChat() {
  if (!state.chat.length && !state.thinking) {
    $("slider-chat-log").innerHTML = `<p class="cu-muted" style="margin-bottom:0.25rem">What would you like to know?</p>
      <p class="cu-muted" style="margin-bottom:0;margin-top:0">Sample queries:</p>
      <div class="cu-actions" style="margin-top:0.25rem">
        <button type="button" class="cu-btn cu-btn-secondary" data-q="Migration order for refactoring">What migration order would be recommended when refactoring to reduce breaking changes?</button>
        <button type="button" class="cu-btn cu-btn-secondary" data-q="Riskiest modules to refactor">Which modules or components would be riskiest to refactor first?</button>
        <button type="button" class="cu-btn cu-btn-secondary" data-q="Data stores in this codebase">What are the data stores in this codebase?</button>
      </div>`;
    $("slider-chat-log").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => sendQuestion(b.dataset.q)));
    return;
  }
  const animate = state.animateLatest;
  state.animateLatest = false;
  let lastAssistantIdx = -1;
  if (animate) {
    for (let i = state.chat.length - 1; i >= 0; i--) {
      if (state.chat[i].role === "assistant") { lastAssistantIdx = i; break; }
    }
  }
  const dlFooter = (idx) =>
    `<div class="cu-bubble-footer"><button class="cu-bubble-dl-btn" data-chat-dl="${idx}" title="Download response" aria-label="Download response">${iconDownload}</button></div>`;
  const messages = state.chat.map((m, idx) => {
    if (idx === lastAssistantIdx) {
      return `<div class="cu-bubble assistant" id="cu-animating-bubble"></div>`;
    }
    return m.role === "assistant"
      ? `<div class="cu-bubble assistant">${renderMarkdown(m.content)}${dlFooter(idx)}</div>`
      : `<div class="cu-bubble user">${esc(m.content)}</div>`;
  });
  if (state.thinking && state.thinking.kind === "query") {
    messages.push(`<div class="cu-bubble assistant thinking-msg">
      <span class="cu-spinner" aria-hidden="true"></span>
      Running query${state.thinking.status ? " · " + esc(state.thinking.status) : ""}
    </div>`);
  }
  $("slider-chat-log").innerHTML = messages.join("");
  runMermaid($("slider-chat-log"));
  $("slider-chat-log").scrollTop = $("slider-chat-log").scrollHeight;
  if (lastAssistantIdx >= 0) {
    const el = document.getElementById("cu-animating-bubble");
    if (el) typewriter(el, state.chat[lastAssistantIdx].content, () => {
      el.removeAttribute("id");
      const footer = document.createElement("div");
      footer.className = "cu-bubble-footer";
      footer.innerHTML = `<button class="cu-bubble-dl-btn" data-chat-dl="${lastAssistantIdx}" title="Download response" aria-label="Download response">${iconDownload}</button>`;
      el.appendChild(footer);
    });
  }
}

const RUN_PAGE_SIZE = 5;
let _runsRefreshInFlight = false;

function hasInProgressRuns(runs) {
  return (runs || []).some((r) => r.status === "Running" || r.status === "Pending");
}

function renderRunPage(page) {
  const totalPages = Math.max(1, Math.ceil(state.allRuns.length / RUN_PAGE_SIZE));
  const clampedPage = Math.max(0, Math.min(page, totalPages - 1));
  state.runPage = clampedPage;
  const pageRuns = state.allRuns.slice(clampedPage * RUN_PAGE_SIZE, (clampedPage + 1) * RUN_PAGE_SIZE);
  $("run-list").innerHTML = pageRuns.length
    ? pageRuns.map(runListCard).join("")
    : `<tr><td colspan="6" class="cu-muted" style="padding:0.75rem 0.9rem">No runs yet.</td></tr>`;
  const paginator = $("run-paginator");
  if (totalPages <= 1) {
    paginator.innerHTML = "";
  } else {
    paginator.innerHTML =
      `<button type="button" class="cu-btn cu-btn-secondary" id="run-prev"${clampedPage === 0 ? " disabled" : ""}>&#x2039; Prev</button>` +
      `<span class="cu-muted" style="font-size:0.85rem">Page ${clampedPage + 1} of ${totalPages}</span>` +
      `<button type="button" class="cu-btn cu-btn-secondary" id="run-next"${clampedPage >= totalPages - 1 ? " disabled" : ""}>Next &#x203a;</button>`;
    if (clampedPage > 0) $("run-prev").onclick = () => renderRunPage(clampedPage - 1);
    if (clampedPage < totalPages - 1) $("run-next").onclick = () => renderRunPage(clampedPage + 1);
  }
  bindRunCards($("run-list"));
  let restoredCb = null;
  if (state.selectedRunId) {
    $("run-list").querySelectorAll(".cu-run-select").forEach((cb) => {
      if (cb.dataset.runId === state.selectedRunId) {
        cb.checked = true;
        restoredCb = cb;
      }
    });
  }
  if (restoredCb) {
    applySelectedRunControls(restoredCb);
  } else if (!state.allRuns.some((r) => r.run_id === state.selectedRunId)) {
    applySelectedRunControls(null);
  }
}

async function refreshActiveRuns() {
  if (_runsRefreshInFlight || document.hidden) return;
  _runsRefreshInFlight = true;
  try {
    const prevActiveIds = new Set(
      state.allRuns
        .filter((r) => r.status === "Running" || r.status === "Pending")
        .map((r) => r.run_id)
    );
    const v2runs = await api("/api/v2/pipelines/runs");
    state.allRuns = [...(v2runs.runs || [])];
    renderRunPage(state.runPage);
    const newlyFinished = state.allRuns.some(
      (r) => prevActiveIds.has(r.run_id) && r.status !== "Running" && r.status !== "Pending"
    );
    if (newlyFinished) {
      const indexes = await api("/api/indexes").catch(() => null);
      if (indexes) {
        state.indexes = indexes.indexes || [];
        state.queryable = indexes.queryable || [];
        renderRepos();
        renderChatSelect();
      }
    }
    syncRunsAutoRefresh();
  } catch {
    // Retry quietly on next tick
  } finally {
    _runsRefreshInFlight = false;
  }
}

function syncRunsAutoRefresh(forceActive = false) {
  const active = forceActive || hasInProgressRuns(state.allRuns);
  if (active && !state.runsRefreshTimer) {
    state.runsRefreshTimer = setInterval(refreshActiveRuns, 8000);
  } else if (!active && state.runsRefreshTimer) {
    clearInterval(state.runsRefreshTimer);
    state.runsRefreshTimer = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && hasInProgressRuns(state.allRuns)) {
    refreshActiveRuns();
  }
});

async function load() {
  try {
    const [status, indexes, v2runs, nsData] = await Promise.all([
      api("/api/status"),
      api("/api/indexes"),
      api("/api/v2/pipelines/runs").catch(() => null),
      api("/api/namespaces").catch(() => ({ namespaces: [], current: "" })),
    ]);
    $("status").className = "cu-status" + (status.ok ? " hidden" : " warn");
    $("status").innerHTML = `<div style="font-size:0.75rem">${status.ok ? "Connected" : "Not connected"}</div><div></div>
      <div>${status.ok ? "" : esc(status.message || "")}</div>`;
    const nsList = nsData.namespaces || [];
    const nsCurrent = nsData.current || status.namespace || "";
    if (nsList.length > 0) {
      $("ns-list").innerHTML = nsList.map((ns) => `<option value="${esc(ns)}"></option>`).join("");
      $("ns-select").value = nsCurrent;
      $("ns-row").classList.remove("hidden");
    } else {
      $("ns-row").classList.add("hidden");
    }

    state.repos = indexes.repos || [];
    state.defaultKey = indexes.default_key;
    state.indexes = indexes.indexes || [];
    state.queryable = indexes.queryable || [];
    if (v2runs) state.allRuns = [...(v2runs.runs || [])];
    renderRunPage(state.runPage);
    syncRunsAutoRefresh();
    $("run-view-report").onclick = async function() {
      const btn = this;
      if (!btn.dataset.runId) return;
      const origContent = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<svg style="animation:cu-spin 1s linear infinite" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> View Report`;
      try {
        await openReport(btn.dataset.runId, btn.dataset.runName, btn.dataset.gitRepo, btn.dataset.gitBranch, btn.dataset.projectSlug || "");
      } catch (e) {
        banner(String(e.message || e), true);
      } finally {
        btn.disabled = false;
        btn.innerHTML = origContent;
      }
    };
    $("run-download-report").onclick = async function() {
      const btn = this;
      if (!btn.dataset.runId) return;
      const origContent = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<svg style="animation:cu-spin 1s linear infinite" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Download PDF Report`;
      try {
        const snap = await api(
          buildRunDetailUrl(
            btn.dataset.runId,
            btn.dataset.gitRepo || "",
            btn.dataset.gitBranch || "",
            btn.dataset.projectSlug || "",
          )
        );
        const report = snap.analysis_report;
        if (!report) { banner("No analysis report available.", true); return; }
        const heading = formatReportHeading(
          btn.dataset.gitRepo || "",
          btn.dataset.gitBranch || "",
          btn.dataset.projectSlug || "",
        );
        await downloadReportAsPdf(
          report,
          `${btn.dataset.runName || btn.dataset.runId}_analysis_report.pdf`,
          heading,
          btn.dataset.runName || btn.dataset.runId,
        );
      } catch (e) {
        banner(String(e.message || e), true);
      } finally {
        btn.disabled = !btn.dataset.runId;
        btn.innerHTML = origContent;
      }
    };
    $("run-download-md-report").onclick = async function() {
      const btn = this;
      if (!btn.dataset.runId) return;
      const origContent = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<svg style="animation:cu-spin 1s linear infinite" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Download Markdown Report`;
      try {
        const snap = await api(
          buildRunDetailUrl(
            btn.dataset.runId,
            btn.dataset.gitRepo || "",
            btn.dataset.gitBranch || "",
            btn.dataset.projectSlug || "",
          )
        );
        const report = snap.analysis_report;
        if (!report) { banner("No analysis report available.", true); return; }
        downloadBlob(
          report,
          `${btn.dataset.runName || btn.dataset.runId}_analysis_report.md`,
          "text/markdown",
        );
      } catch (e) {
        banner(String(e.message || e), true);
      } finally {
        btn.disabled = !btn.dataset.runId;
        btn.innerHTML = origContent;
      }
    };
    $("selection-note").classList.remove("hidden");
    $("selection-note").onclick = () => $("selection-note").classList.toggle("cu-note-expanded");
    $("run-selection-note").classList.remove("hidden");
    $("run-selection-note").onclick = () => $("run-selection-note").classList.toggle("cu-note-expanded");

    renderRepos();
    renderChatSelect();
    renderChat();
    if (!state.thinking) banner("");
  } catch (e) {
    banner(String(e.message || e), true);
  }
}

function pollQuery(queryId) {
  const POLL_INTERVAL_MS = 3000;
  const MAX_POLLS = 200; // 10 minutes
  let polls = 0;
  const interval = setInterval(async () => {
    polls += 1;
    if (polls > MAX_POLLS) {
      clearInterval(interval);
      setThinking(null);
      renderChat();
      banner("Query timed out. The analysis is taking longer than expected.", true);
      return;
    }
    try {
      const snap = await api(`/api/v2/queries/${queryId}`);
      if (snap.status === "succeeded") {
        clearInterval(interval);
        state.chat.push({ role: "assistant", content: snap.result || "" });
        setThinking(null);
        state.animateLatest = true;
        renderChat();
      } else if (snap.status === "failed") {
        clearInterval(interval);
        setThinking(null);
        renderChat();
        banner(snap.error || "Query failed.", true);
      }
    } catch (e) {
      clearInterval(interval);
      setThinking(null);
      renderChat();
      banner(String(e.message || e), true);
    }
  }, POLL_INTERVAL_MS);
}

async function pollV2(runId) {
  if (state.poll) clearInterval(state.poll);
  let tickInFlight = false;
  const tick = async () => {
    if (tickInFlight) return;
    tickInFlight = true;
    try {
      const snap = await api(`/api/v2/pipelines/runs/${runId}`);
      if (state.thinking) {
        const stagePart = snap.current_stage ? ` · ${snap.current_stage}` : "";
        setThinking({
          ...state.thinking,
          status: `${snap.status}${stagePart} · ${runId}`,
        });
      }
      await load();
      if (snap.status === "running" || snap.status === "pending") {
        const matched = state.allRuns.find((r) => r.run_id === runId);
        if (matched && snap.current_stage && matched.current_stage !== snap.current_stage) {
          matched.current_stage = snap.current_stage;
          renderRunPage(state.runPage);
        }
        syncRunsAutoRefresh(true);
      }
      if (["succeeded", "skipped", "failed", "cancelled", "running"].includes(snap.status)) {
        clearInterval(state.poll);
        state.poll = null;
        setThinking(null);
        if (snap.status === "succeeded" || snap.status === "skipped") {
          banner("Pipeline completed successfully.");
        } else if (snap.status === "running") {
          banner(`Pipeline running.`);
          setTimeout(() => { if ($("banner").textContent === "Pipeline running.") banner(""); }, 3000);
        } else {
          banner(`Pipeline ${snap.status}`, true);
        }
      }
    } catch (e) {
      clearInterval(state.poll);
      state.poll = null;
      setThinking(null);
      banner(String(e.message || e), true);
    } finally {
      tickInFlight = false;
    }
  };
  tick();
  state.poll = setInterval(tick, 4000);
}

function openChatSlider(gitRepo, gitBranch, isMulti, runTitle, runName, projectSlug) {
  renderChatSelect();
  if (gitRepo || isMulti) {
    const sel = $("slider-chat-repo");
    const match = state.queryable.find((o) => {
      if (isMulti) {
        if (!o.multi_repo) return false;
        if (projectSlug) return (o.project_slug || "") === projectSlug;
        return true;
      }
      return o.git_repo === gitRepo && (!gitBranch || o.git_branch === gitBranch);
    });
    if (match) sel.value = match.key;
  }
  if (runTitle) {
    const label = runName ? `${runTitle}@${runName}` : runTitle;
    $("slider-chat-repo-name").textContent = label;
    $("slider-chat-repo-label").classList.remove("hidden");
  }
  $("chat-slider-overlay").classList.remove("hidden");
  $("chat-slider").classList.add("open");
  renderChat();
}
function closeChatSlider() {
  $("chat-slider").classList.remove("open");
  $("chat-slider-overlay").classList.add("hidden");
  if (state.thinking && state.thinking.kind === "query") {
    $("thinking-status").textContent = "Query cancelled.";
  }
}
$("chat-slider-close").onclick = closeChatSlider;
$("chat-slider-overlay").onclick = closeChatSlider;

async function sendQuestion(text) {
  const question = (text || "").trim();
  if (!question || state.thinking) return;
  if (!$("slider-chat-repo").value) {
    banner("Select a repository before sending.", true);
    $("banner").dataset.reason = "no-chat-repo";
    return;
  }
  banner("");
  const option = state.queryable.find((o) => o.key === $("slider-chat-repo").value) || {};
  state.chat.push({ role: "user", content: question });
  $("slider-question").value = "";
  openChatSlider();
  setThinking({ kind: "query", label: "Thinking", status: "Analyzing knowledge graph…" });
  renderChat();
  try {
    const result = await api("/api/v2/queries", {
      method: "POST",
      body: JSON.stringify({
        question,
        git_repo: option.git_repo || "",
        git_branch: option.git_branch || "main",
        use_global: option.use_global || false,
        multi_repo: option.multi_repo || false,
        project_slug: option.project_slug || "",
      }),
    });
    if (result.status === "succeeded") {
      setThinking(null);
      state.chat.push({ role: "assistant", content: result.answer || result.result || "(empty response)" });
      renderChat();
    } else {
      pollQuery(result.query_id);
    }
  } catch (e) {
    setThinking(null);
    renderChat();
    banner(String(e.message || e), true);
  }
}

document.querySelectorAll(".cu-nav-item").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});
document.querySelectorAll(".cu-breadcrumb a[data-tab]").forEach((a) => {
  a.addEventListener("click", (e) => { e.preventDefault(); showTab(a.dataset.tab); });
});
document.querySelectorAll(".cu-refresh-btn").forEach((btn) => {
  btn.addEventListener("click", hardRefresh);
});
$("back-to-runs").onclick = () => showTab("runs");
$("run-rerun").onclick = () => {
  if (state.thinking) return;
  const btn = $("run-rerun");
  if (!btn.dataset.runId) return;
  // Reset radio to default
  const scratchRadio = document.querySelector('input[name="rerun-mode"][value="scratch"]');
  if (scratchRadio) { scratchRadio.checked = true; scratchRadio.dispatchEvent(new Event("change")); }
  $("rerun-modal").classList.remove("hidden");
};
// Radio change handler
document.querySelectorAll('input[name="rerun-mode"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const mode = document.querySelector('input[name="rerun-mode"]:checked').value;
    $("rerun-description").textContent = mode === "scratch"
      ? "Regenerates the knowledge graph from scratch."
      : "Regenerates the analysis report only.";
  });
});
// Rerun modal close
function closeRerunModal() { $("rerun-modal").classList.add("hidden"); }
$("rerun-close").onclick = closeRerunModal;
$("rerun-cancel").onclick = closeRerunModal;
$("rerun-modal").addEventListener("click", (e) => { if (e.target === $("rerun-modal")) closeRerunModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("rerun-modal").classList.contains("hidden")) closeRerunModal();
});
// Rerun confirm handler
$("rerun-confirm").onclick = async () => {
  const mode = document.querySelector('input[name="rerun-mode"]:checked').value;
  closeRerunModal();
  const btn = $("run-rerun");
  const runId = btn.dataset.runId;
  const gitRepo = btn.dataset.gitRepo;
  const gitBranch = btn.dataset.gitBranch;
  const projectSlug = btn.dataset.projectSlug || "";
  if (!runId) return;
  const origContent = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<svg style="animation:cu-spin 1s linear infinite" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Re-run`;
  const label = mode === "scratch" ? "Re-running from scratch" : "Re-running analysis";
  const statusMsg = mode === "scratch" ? "Submitting full re-run job…" : "Submitting analysis-only job…";
  setThinking({ kind: "pipeline", label, status: statusMsg });
  try {
    const body = { repos: gitRepo ? [{ git_repo: gitRepo, git_branch: gitBranch }] : [] };
    if (projectSlug) body.project_slug = projectSlug;
    if (mode === "report") body.pipeline_run_id = runId;
    const result = await api("/api/v2/pipelines", { method: "POST", body: JSON.stringify(body) });
    state.runPage = 0;
    setThinking({ kind: "pipeline", label, status: `KFP run ${result.job_id} started` });
    pollV2(result.job_id);
  } catch (e) {
    setThinking(null);
    banner(String(e.message || e), true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = origContent;
  }
};
$("select-all").onclick = () => { state.repos.forEach((r) => state.selected.add(repoKey(r))); renderRepos(); };
$("clear").onclick = () => { state.selected.clear(); renderRepos(); };
function closeAddRepo() {
  $("add-repo-modal").classList.add("hidden");
  $("custom-url").value = "";
  $("custom-branch").value = "main";
  $("add-repo-error").classList.add("hidden");
  $("add-repo-error").textContent = "";
}
$("open-add-repo").onclick = () => {
  $("add-repo-modal").classList.remove("hidden");
  $("custom-url").focus();
};
$("add-repo-close").onclick = closeAddRepo;
$("add-repo-cancel").onclick = closeAddRepo;
$("add-repo-modal").addEventListener("click", (e) => { if (e.target === $("add-repo-modal")) closeAddRepo(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("add-repo-modal").classList.contains("hidden")) closeAddRepo();
});
$("add-custom").onclick = async () => {
  const git_repo = $("custom-url").value.trim();
  const git_branch = $("custom-branch").value.trim() || "main";
  if (!git_repo) {
    $("add-repo-error").textContent = "Repository URL is required.";
    $("add-repo-error").classList.remove("hidden");
    return;
  }
  if (!/^https?:\/\//i.test(git_repo)) {
    $("add-repo-error").textContent = "Repository URL must start with http:// or https://";
    $("add-repo-error").classList.remove("hidden");
    return;
  }
  $("add-repo-error").classList.add("hidden");
  const key = `${git_repo}|${git_branch}`;
  if (!state.repos.some((r) => repoKey(r) === key)) state.repos.push({ git_repo, git_branch });
  state.selected.add(key);
  closeAddRepo();
  renderRepos();
  api("/api/v2/repositories", {
    method: "POST",
    body: JSON.stringify({ repos: [{ git_repo, git_branch }] }),
  }).catch(() => {});
};
function closeAnalyzeModal() {
  $("analyze-modal").classList.add("hidden");
}
$("open-analyze").onclick = () => {
  if (state.thinking) return;
  const selected = selectedRepos();
  if (!selected.length) {
    banner("Select at least one repository before running.", true);
    $("banner").dataset.reason = "no-selection";
    return;
  }
  $("analyze-selection").innerHTML = `<strong>Selected:</strong> ${selected.map((r) => `${esc(shortName(r.git_repo))} @ ${esc(r.git_branch)}`).join(", ")}`;
  if (selected.length > 1) {
    $("project-slug-group").classList.remove("hidden");
    const defaultSlug = selected
      .slice(0, 3)
      .map((r) => shortName(r.git_repo).replace(/\.git$/, "").toLowerCase().replace(/[^a-z0-9_-]/g, "-"))
      .join("-");
    $("project-slug-input").value = defaultSlug;
  } else {
    $("project-slug-group").classList.add("hidden");
    $("project-slug-input").value = "";
  }
  $("analyze-modal").classList.remove("hidden");
};
$("analyze-modal-close").onclick = closeAnalyzeModal;
$("analyze-modal-cancel").onclick = closeAnalyzeModal;
$("analyze-modal").addEventListener("click", (e) => { if (e.target === $("analyze-modal")) closeAnalyzeModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("analyze-modal").classList.contains("hidden")) closeAnalyzeModal();
  if (e.key === "Escape" && $("chat-slider").classList.contains("open")) closeChatSlider();
});
$("start-analysis").onclick = async () => {
  const projectSlug = $("project-slug-input").value.trim();
  closeAnalyzeModal();
  $("report-viewer-title").textContent = "";
  $("report-viewer-subtitle").textContent = "";
  $("report-viewer-content").innerHTML = "";
  setThinking({ kind: "pipeline", label: "Running pipeline", status: "Submitting indexing job…" });
  try {
    const submittedRepos = selectedRepos();
    const payload = { repos: submittedRepos };
    if (submittedRepos.length > 1 && projectSlug) payload.project_slug = projectSlug;
    const result = await api("/api/v2/pipelines", { method: "POST", body: JSON.stringify(payload) });
    state.runPage = 0;
    setThinking({ kind: "pipeline", label: "Running pipeline", status: `KFP run ${result.job_id} started` });
    pollV2(result.job_id);
    showTab("runs");
  } catch (e) {
    setThinking(null);
    banner(String(e.message || e), true);
  }
};
$("slider-chat-repo").addEventListener("change", () => {
  if ($("banner").dataset.reason === "no-chat-repo") {
    banner("");
    delete $("banner").dataset.reason;
  }
});
async function commitNamespace() {
  const ns = $("ns-select").value.trim();
  if (!ns) return;
  try {
    await api("/api/namespace", { method: "POST", body: JSON.stringify({ namespace: ns }) });
    state.namespace = ns;
    try { sessionStorage.setItem("cu-namespace", ns); } catch {}
    state.runPage = 0;
    state.selectedRunId = null;
    await load();
  } catch (e) {
    await load(); // revert displayed value to server state before showing error
    banner(String(e.message || e), true);
  }
}
$("ns-select").addEventListener("change", commitNamespace);
$("ns-select").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    $("ns-select").blur();
  }
});
$("slider-chat-log").addEventListener("click", (e) => {
  const btn = e.target.closest(".cu-bubble-dl-btn");
  if (!btn || btn.dataset.chatDl === undefined) return;
  const msg = state.chat[Number(btn.dataset.chatDl)];
  if (!msg) return;
  downloadBlob(msg.content, `chat_response_${Number(btn.dataset.chatDl) + 1}.md`, "text/markdown");
});
$("slider-send").onclick = () => sendQuestion($("slider-question").value);
$("slider-question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    sendQuestion($("slider-question").value);
  }
});
// Help popovers: show on hover or click, close on mouse-leave or outside click
document.querySelectorAll(".cu-help").forEach((wrap) => {
  let leaveTimer;
  wrap.addEventListener("mouseenter", () => {
    clearTimeout(leaveTimer);
    wrap.classList.add("open");
  });
  wrap.addEventListener("mouseleave", () => {
    leaveTimer = setTimeout(() => wrap.classList.remove("open"), 120);
  });
  wrap.querySelector(".cu-help-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = wrap.classList.contains("open");
    document.querySelectorAll(".cu-help.open").forEach((el) => el.classList.remove("open"));
    if (!isOpen) wrap.classList.add("open");
  });
});
document.addEventListener("click", () => {
  document.querySelectorAll(".cu-help.open").forEach((el) => el.classList.remove("open"));
});

// Theme toggle
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("cu-theme") || localStorage.getItem("theme"); } catch {}
  if (saved === "dark") {
    document.body.classList.add("dark");
    const dark = document.querySelector('input[name="theme-mode"][value="dark"]');
    if (dark) dark.checked = true;
  }
})();
document.querySelectorAll('input[name="theme-mode"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const mode = document.querySelector('input[name="theme-mode"]:checked').value;
    document.body.classList.toggle("dark", mode === "dark");
    try { localStorage.setItem("cu-theme", mode); } catch {}
  });
});

load();
