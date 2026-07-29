"""Browser-side capture and playback snippets for local voice."""

from __future__ import annotations

import json


def start_browser_voice_capture_js(*, sink_id: int, mode: str) -> str:
    safe_mode = "dictate" if mode == "dictate" else "talk"
    return f"""
(async () => {{
  const sink = document.getElementById('c{int(sink_id)}');
  const emit = (name, detail) => sink?.dispatchEvent(new CustomEvent(name, {{detail}}));
  const localHost = ['localhost', '127.0.0.1', '::1'].includes(location.hostname);
  if (!window.isSecureContext && !localHost) {{
    emit('row-bot-browser-voice-error', {{code: 'secure_context_required'}});
    return;
  }}
  if (window._rowBotBrowserVoice?.recorder) {{
    window._rowBotBrowserVoice.cancelled = true;
    window._rowBotBrowserVoice.recorder.stop();
  }}
  try {{
    const statusResponse = await fetch('/api/voice/local/status', {{
      credentials: 'same-origin',
      cache: 'no-store',
    }});
    const status = await statusResponse.json();
    if (!status.whisper_ready) {{
      emit('row-bot-browser-voice-error', {{code: 'whisper_model_missing'}});
      return;
    }}
    const stream = await navigator.mediaDevices.getUserMedia({{audio: true}});
    const candidates = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'];
    const mimeType = candidates.find(t => MediaRecorder.isTypeSupported(t)) || '';
    const recorder = new MediaRecorder(stream, mimeType ? {{mimeType}} : undefined);
    const state = {{recorder, stream, chunks: [], cancelled: false, timer: null}};
    window._rowBotBrowserVoice = state;
    recorder.ondataavailable = event => {{
      if (event.data?.size) state.chunks.push(event.data);
    }};
    recorder.onstop = async () => {{
      clearTimeout(state.timer);
      stream.getTracks().forEach(track => track.stop());
      if (state.cancelled) return;
      try {{
        const blob = new Blob(state.chunks, {{type: recorder.mimeType || 'audio/webm'}});
        const response = await fetch('/api/voice/local/transcribe', {{
          method: 'POST',
          credentials: 'same-origin',
          headers: {{'Content-Type': blob.type}},
          body: blob,
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'transcription_failed');
        emit('row-bot-browser-voice-transcript', {{
          text: payload.text || '',
          mode: {json.dumps(safe_mode)},
        }});
      }} catch (error) {{
        emit('row-bot-browser-voice-error', {{code: error.message || 'transcription_failed'}});
      }} finally {{
        if (window._rowBotBrowserVoice === state) window._rowBotBrowserVoice = null;
      }}
    }};
    recorder.start(250);
    state.timer = setTimeout(() => recorder.state === 'recording' && recorder.stop(), 30000);
    emit('row-bot-browser-voice-state', {{state: 'recording', mode: {json.dumps(safe_mode)}}});
  }} catch (error) {{
    emit('row-bot-browser-voice-error', {{code: error.name || 'microphone_unavailable'}});
  }}
}})();
"""


def stop_browser_voice_js(*, cancel: bool = True) -> str:
    cancelled = "true" if cancel else "false"
    return f"""
(() => {{
  const state = window._rowBotBrowserVoice;
  if (state) {{
    state.cancelled = {cancelled};
    clearTimeout(state.timer);
    state.stream?.getTracks().forEach(track => track.stop());
    if (state.recorder?.state === 'recording') state.recorder.stop();
    window._rowBotBrowserVoice = null;
  }}
  const playback = window._rowBotBrowserVoicePlayback;
  if (playback) {{
    playback.audio.pause();
    URL.revokeObjectURL(playback.url);
    window._rowBotBrowserVoicePlayback = null;
  }}
}})();
"""


def speak_browser_voice_js(text: str) -> str:
    return f"""
(async () => {{
  const text = {json.dumps(str(text or ""))};
  if (!text.trim()) return;
  const old = window._rowBotBrowserVoicePlayback;
  if (old) {{
    old.audio.pause();
    URL.revokeObjectURL(old.url);
  }}
  const response = await fetch('/api/voice/local/synthesize', {{
    method: 'POST',
    credentials: 'same-origin',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{text}}),
  }});
  if (!response.ok) return;
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  window._rowBotBrowserVoicePlayback = {{audio, url}};
  const cleanup = () => {{
    if (window._rowBotBrowserVoicePlayback?.audio === audio) {{
      URL.revokeObjectURL(url);
      window._rowBotBrowserVoicePlayback = null;
    }}
  }};
  audio.addEventListener('ended', cleanup, {{once: true}});
  audio.addEventListener('error', cleanup, {{once: true}});
  await audio.play();
}})();
"""
