from __future__ import annotations

import json
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .pipeline import load_course
from .utils import ensure_dir


def build_viewer(course_dir: Path) -> Path:
    course_dir = course_dir.resolve()
    config = load_course(course_dir)
    video_path = "../" + config["media"]["course_video"]
    title = config["title"]
    html = VIEWER_TEMPLATE.replace("__TITLE__", _json_string(title)).replace(
        "__VIDEO_PATH__", _json_string(video_path)
    )
    output = course_dir / "viewer" / "index.html"
    ensure_dir(output.parent)
    output.write_text(html, encoding="utf-8")
    return output


def serve_course(course_dir: Path, *, host: str, port: int, open_browser: bool) -> None:
    course_dir = course_dir.resolve()
    build_viewer(course_dir)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(course_dir), **kwargs)

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/viewer/index.html"
    print(f"Viewer: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


VIEWER_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ChalkSync</title>
  <style>
    :root { color-scheme: light; --ink:#18201d; --muted:#65706b; --line:#d9dfdc; --paper:#f7f8f7; --accent:#087f5b; --warm:#c24b31; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; color:var(--ink); background:var(--paper); font-family:Inter,ui-sans-serif,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
    header { height:52px; display:flex; align-items:center; gap:14px; padding:0 18px; border-bottom:1px solid var(--line); background:#fff; }
    header h1 { margin:0; font-size:16px; font-weight:650; letter-spacing:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    header span { color:var(--muted); font-size:12px; white-space:nowrap; }
    main { display:grid; grid-template-columns:minmax(0, 58fr) minmax(360px, 42fr); height:calc(100vh - 52px); }
    .video-pane { display:flex; align-items:center; justify-content:center; min-width:0; padding:16px; background:#111512; }
    video { width:100%; max-height:calc(100vh - 84px); background:#000; }
    .notes-pane { min-width:0; overflow:auto; padding:24px clamp(18px,3vw,40px) 80px; border-left:1px solid var(--line); background:#fff; }
    #notes { max-width:820px; margin:0 auto; line-height:1.72; font-size:15px; }
    #notes h1 { margin:0 0 22px; font-size:28px; letter-spacing:0; line-height:1.25; }
    #notes h2 { margin:32px 0 12px; padding-top:14px; border-top:1px solid var(--line); font-size:20px; letter-spacing:0; }
    #notes h3 { margin:22px 0 8px; font-size:16px; letter-spacing:0; }
    #notes p, #notes li { margin:7px 0; }
    #notes ul { margin:8px 0 14px; padding-left:22px; }
    #notes pre { padding:14px; overflow:auto; background:#f1f3f2; border-left:3px solid var(--accent); font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; }
    .timestamp { display:inline-flex; align-items:center; min-height:24px; margin:0 3px; padding:1px 6px; border:1px solid #abd6c8; border-radius:4px; color:var(--accent); background:#effaf6; font:600 12px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; cursor:pointer; }
    .timestamp:hover { border-color:var(--accent); background:#dff4ec; }
    .loading, .error { color:var(--muted); }
    .error { color:var(--warm); }
    @media (max-width:900px) {
      main { grid-template-columns:1fr; grid-template-rows:auto 1fr; height:auto; }
      .video-pane { position:sticky; top:0; z-index:2; padding:0; }
      video { max-height:42vh; }
      .notes-pane { border-left:0; border-top:1px solid var(--line); min-height:58vh; }
    }
  </style>
</head>
<body>
  <header><h1 id="title"></h1><span>视频与证据笔记</span></header>
  <main>
    <section class="video-pane"><video id="video" controls preload="metadata"></video></section>
    <section class="notes-pane"><article id="notes"><p class="loading">正在载入笔记…</p></article></section>
  </main>
  <script>
    const title = __TITLE__;
    const videoPath = __VIDEO_PATH__;
    const video = document.getElementById('video');
    const notes = document.getElementById('notes');
    document.title = title;
    document.getElementById('title').textContent = title;
    video.src = videoPath;

    function appendWithTimestamps(parent, text) {
      const pattern = /\[(\d{1,3}):(\d{2}):(\d{2})\](?:\((https?:\/\/[^)\s]+)\))?/g;
      let cursor = 0;
      for (const match of text.matchAll(pattern)) {
        parent.append(document.createTextNode(text.slice(cursor, match.index)));
        const link = document.createElement('a');
        link.className = 'timestamp';
        link.textContent = `${match[1]}:${match[2]}:${match[3]}`;
        link.href = match[4] || '#';
        link.target = '_blank';
        link.rel = 'noreferrer';
        link.title = match[4] ? '在本地视频跳转，或在新标签页打开网页视频' : '跳转到本地视频位置';
        link.addEventListener('click', event => {
          if (!video.error) event.preventDefault();
          video.currentTime = Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
          video.play();
        });
        parent.append(link);
        cursor = match.index + match[0].length;
      }
      parent.append(document.createTextNode(text.slice(cursor)));
    }

    function renderMarkdown(markdown) {
      notes.replaceChildren();
      let inCode = false;
      let code = null;
      let list = null;
      for (const rawLine of markdown.split(/\r?\n/)) {
        if (rawLine.trim().startsWith('```')) {
          if (inCode) { notes.append(code); code = null; }
          else { code = document.createElement('pre'); }
          inCode = !inCode;
          continue;
        }
        if (inCode) { code.append(document.createTextNode(rawLine + '\n')); continue; }
        const heading = rawLine.match(/^(#{1,3})\s+(.*)$/);
        if (heading) {
          list = null;
          const element = document.createElement('h' + heading[1].length);
          appendWithTimestamps(element, heading[2]);
          notes.append(element);
          continue;
        }
        const bullet = rawLine.match(/^\s*[-*]\s+(.*)$/);
        if (bullet) {
          if (!list) { list = document.createElement('ul'); notes.append(list); }
          const item = document.createElement('li');
          appendWithTimestamps(item, bullet[1]);
          list.append(item);
          continue;
        }
        list = null;
        if (!rawLine.trim()) continue;
        const paragraph = document.createElement('p');
        appendWithTimestamps(paragraph, rawLine);
        notes.append(paragraph);
      }
    }

    fetch('../notes/course.md')
      .then(response => { if (!response.ok) throw new Error('笔记尚未生成'); return response.text(); })
      .then(renderMarkdown)
      .catch(error => { notes.innerHTML = ''; const p = document.createElement('p'); p.className = 'error'; p.textContent = error.message; notes.append(p); });
  </script>
</body>
</html>
'''
