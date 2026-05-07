(function () {
  const instances = new Map();
  const generated = new Map();
  const IDLE_REPLAY_DELAY_MS = 60000;
  const CLIP_CROSSFADE_MS = 280;
  const LOOP_RESTART_PADDING_SECONDS = 0.08;
  const DESKTOP_STATUS_HOLD_MS = 5500;
  const BACKGROUND_COLOR_DISTANCE_THRESHOLD = 24;
  const BACKGROUND_LUMA_DELTA_THRESHOLD = 15;
  const BACKGROUND_SEED_RATIO = 0.05;

  function qs(root, selector) {
    return root ? root.querySelector(selector) : null;
  }

  function setStatus(root, message) {
    const status = qs(root, '.buddy-status');
    if (status) status.textContent = message || '';
  }

  function setUnavailable(root, message) {
    root.classList.add('buddy-unavailable');
    root.classList.remove('buddy-generated', 'buddy-ready');
    setStatus(root, message || 'Animation unavailable');
  }

  const PERSONALITY_STATUS = {
    warm_mystical: {
      approval: 'I am holding the threshold.',
      working: 'I am gathering the threads.',
      success: 'The path is clear.',
      error: 'Something asks for care.',
      idle: 'I am nearby.',
    },
    calm_focus: {
      approval: 'Decision paused for you.',
      working: 'Working steadily.',
      success: 'Done and stable.',
      error: 'Issue detected.',
      idle: 'Ready when needed.',
    },
    playful_helper: {
      approval: 'Your call next.',
      working: 'On it now.',
      success: 'That landed nicely.',
      error: 'That needs another pass.',
      idle: 'I am here.',
    },
    quiet_guardian: {
      approval: 'Waiting for your approval.',
      working: 'Watching the work.',
      success: 'Complete.',
      error: 'Attention needed.',
      idle: 'Standing by.',
    },
    curious_scholar: {
      approval: 'A decision point appeared.',
      working: 'Following the evidence.',
      success: 'The result checks out.',
      error: 'A contradiction surfaced.',
      idle: 'Ready to investigate.',
    },
  };

  function statusKind(snapshot, message) {
    const animation = snapshot && snapshot.animation ? String(snapshot.animation) : '';
    const mood = snapshot && snapshot.mood ? String(snapshot.mood) : '';
    const text = String(message || '').toLowerCase();
    if (animation === 'tap_glass') return 'approval';
    if (animation.indexOf('celebrate') === 0 || mood === 'proud' || text.indexOf('done') !== -1 || text.indexOf('generated') !== -1) return 'success';
    if (
      animation === 'pause' ||
      mood === 'concerned' ||
      text.indexOf('error') !== -1 ||
      text.indexOf('failed') !== -1 ||
      text.indexOf('cancelled') !== -1 ||
      text.indexOf('canceled') !== -1 ||
      text.indexOf('denied') !== -1 ||
      text.indexOf('timed out') !== -1 ||
      text.indexOf('stopped') !== -1 ||
      text.indexOf('interrupted') !== -1
    ) return 'error';
    if (text.indexOf('approval') !== -1) return 'approval';
    if (animation && animation !== 'idle_breathe') return 'working';
    return 'idle';
  }

  function generatedStatus(root, snapshot) {
    const message = snapshot && (snapshot.message || snapshot.label) ? String(snapshot.message || snapshot.label) : '';
    if (!message) return '';
    const surface = root && root.dataset ? root.dataset.surface || '' : '';
    const verbosity = root && root.dataset ? root.dataset.bubbleVerbosity || 'normal' : 'normal';
    const kind = statusKind(snapshot, message);
    const now = Date.now();
    const heldUntil = root && root.dataset ? Number(root.dataset.statusHoldUntil || 0) : 0;
    if (surface === 'desktop' && kind === 'idle' && heldUntil > now) {
      return root.dataset.statusHoldText || '';
    }
    if (verbosity === 'quiet' && !(surface === 'desktop' && kind !== 'idle')) return '';
    let result = message;
    if (verbosity === 'chatty') {
      const personality = root && root.dataset ? root.dataset.personality || 'warm_mystical' : 'warm_mystical';
      const table = PERSONALITY_STATUS[personality] || PERSONALITY_STATUS.warm_mystical;
      const line = table[kind] || message;
      result = line === message ? message : line + ' ' + message;
    }
    if (surface === 'desktop' && kind !== 'idle' && root && root.dataset) {
      root.dataset.statusHoldText = result;
      root.dataset.statusHoldUntil = String(now + DESKTOP_STATUS_HOLD_MS);
    }
    return result;
  }

  function drawCoverSource(ctx, source, x, y, width, height) {
    const sourceWidth = source.videoWidth || source.naturalWidth || width;
    const sourceHeight = source.videoHeight || source.naturalHeight || height;
    const sourceRatio = sourceWidth / sourceHeight;
    const targetRatio = width / height;
    let sx = 0;
    let sy = 0;
    let sw = sourceWidth;
    let sh = sourceHeight;
    if (sourceRatio > targetRatio) {
      sw = sourceHeight * targetRatio;
      sx = (sourceWidth - sw) / 2;
    } else if (sourceRatio < targetRatio) {
      sh = sourceWidth / targetRatio;
      sy = (sourceHeight - sh) / 2;
    }
    ctx.drawImage(source, sx, sy, sw, sh, x, y, width, height);
  }

  function drawContainSource(ctx, source, x, y, width, height) {
    const sourceWidth = source.videoWidth || source.naturalWidth || width;
    const sourceHeight = source.videoHeight || source.naturalHeight || height;
    const sourceRatio = sourceWidth / sourceHeight;
    const targetRatio = width / height;
    let dw = width;
    let dh = height;
    if (sourceRatio > targetRatio) {
      dh = width / sourceRatio;
    } else if (sourceRatio < targetRatio) {
      dw = height * sourceRatio;
    }
    const dx = x + (width - dw) / 2;
    const dy = y + (height - dh) / 2;
    ctx.drawImage(source, dx, dy, dw, dh);
  }

  function colorDistance(red, green, blue, color) {
    const dr = red - color.red;
    const dg = green - color.green;
    const db = blue - color.blue;
    return Math.sqrt(dr * dr + dg * dg + db * db);
  }

  function sampleBackgroundColor(data, width, height) {
    const samples = [];
    const sampleSize = Math.max(3, Math.floor(Math.min(width, height) * 0.05));
    const corners = [
      [0, 0],
      [width - sampleSize, 0],
      [0, height - sampleSize],
      [width - sampleSize, height - sampleSize],
    ];
    for (const corner of corners) {
      const startX = Math.max(0, corner[0]);
      const startY = Math.max(0, corner[1]);
      for (let y = startY; y < Math.min(height, startY + sampleSize); y += 1) {
        for (let x = startX; x < Math.min(width, startX + sampleSize); x += 1) {
          const index = (y * width + x) * 4;
          const alpha = data[index + 3];
          if (alpha === 0) continue;
          samples.push([data[index], data[index + 1], data[index + 2]]);
        }
      }
    }
    if (!samples.length) return { red: 7, green: 10, blue: 14, luma: 10 };
    const total = samples.reduce(function (acc, sample) {
      acc.red += sample[0];
      acc.green += sample[1];
      acc.blue += sample[2];
      return acc;
    }, { red: 0, green: 0, blue: 0 });
    const red = total.red / samples.length;
    const green = total.green / samples.length;
    const blue = total.blue / samples.length;
    return {
      red,
      green,
      blue,
      luma: red * 0.2126 + green * 0.7152 + blue * 0.0722,
    };
  }

  function isVideoBackgroundPixel(data, index, background) {
    const red = data[index];
    const green = data[index + 1];
    const blue = data[index + 2];
    const alpha = data[index + 3];
    if (alpha === 0) return false;
    const luma = red * 0.2126 + green * 0.7152 + blue * 0.0722;
    const distance = colorDistance(red, green, blue, background);
    const lumaDelta = Math.abs(luma - background.luma);
    return distance < BACKGROUND_COLOR_DISTANCE_THRESHOLD && lumaDelta < BACKGROUND_LUMA_DELTA_THRESHOLD;
  }

  function seedBackgroundCorners(enqueue, width, height) {
    const seedSize = Math.max(3, Math.floor(Math.min(width, height) * BACKGROUND_SEED_RATIO));
    const corners = [
      [0, 0],
      [width - seedSize, 0],
      [0, height - seedSize],
      [width - seedSize, height - seedSize],
    ];
    for (const corner of corners) {
      const startX = Math.max(0, corner[0]);
      const startY = Math.max(0, corner[1]);
      for (let row = startY; row < Math.min(height, startY + seedSize); row += 1) {
        for (let col = startX; col < Math.min(width, startX + seedSize); col += 1) {
          enqueue(row * width + col);
        }
      }
    }
  }

  function ensureKeyCanvas(state, width, height) {
    if (!state.keyCanvas) {
      state.keyCanvas = document.createElement('canvas');
      state.keyCtx = state.keyCanvas.getContext('2d', { willReadFrequently: true });
    }
    if (state.keyCanvas.width !== width || state.keyCanvas.height !== height) {
      state.keyCanvas.width = width;
      state.keyCanvas.height = height;
    }
    return state.keyCtx;
  }

  function drawTransparentSource(ctx, state, source, x, y, width, height) {
    const outputWidth = Math.max(1, Math.round(width));
    const outputHeight = Math.max(1, Math.round(height));
    const keyCtx = ensureKeyCanvas(state, outputWidth, outputHeight);
    keyCtx.clearRect(0, 0, outputWidth, outputHeight);
    drawContainSource(keyCtx, source, 0, 0, outputWidth, outputHeight);
    let frame;
    try {
      frame = keyCtx.getImageData(0, 0, outputWidth, outputHeight);
    } catch (error) {
      drawContainSource(ctx, source, x, y, width, height);
      return;
    }
    const data = frame.data;
    const background = sampleBackgroundColor(data, outputWidth, outputHeight);
    const total = outputWidth * outputHeight;
    const remove = new Uint8Array(total);
    const queue = [];
    function enqueue(pixel) {
      if (remove[pixel]) return;
      const index = pixel * 4;
      if (!isVideoBackgroundPixel(data, index, background)) return;
      remove[pixel] = 1;
      queue.push(pixel);
    }
    seedBackgroundCorners(enqueue, outputWidth, outputHeight);
    for (let cursor = 0; cursor < queue.length; cursor += 1) {
      const pixel = queue[cursor];
      const row = Math.floor(pixel / outputWidth);
      const col = pixel - row * outputWidth;
      if (col > 0) enqueue(pixel - 1);
      if (col < outputWidth - 1) enqueue(pixel + 1);
      if (row > 0) enqueue(pixel - outputWidth);
      if (row < outputHeight - 1) enqueue(pixel + outputWidth);
    }
    for (let pixel = 0; pixel < total; pixel += 1) {
      if (!remove[pixel]) continue;
      data[pixel * 4 + 3] = 0;
    }
    keyCtx.putImageData(frame, 0, 0);
    ctx.drawImage(state.keyCanvas, x, y, width, height);
  }

  function parseMotionPack(raw) {
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw);
      if (!parsed || !parsed.clips || typeof parsed.clips !== 'object') return null;
      return parsed;
    } catch (error) {
      return null;
    }
  }

  function clipForSnapshot(state, snapshot) {
    const pack = state.motionPack;
    if (!pack || !pack.clips) return '';
    const animation = snapshot && snapshot.animation ? snapshot.animation : 'idle_breathe';
    const mapped = pack.animationMap && pack.animationMap[animation] ? pack.animationMap[animation] : '';
    if (mapped && state.videos && state.videos[mapped]) return mapped;
    const defaultClip = pack.defaultClip || 'idle';
    if (state.videos && state.videos[defaultClip]) return defaultClip;
    const keys = Object.keys(state.videos || {});
    return keys.length ? keys[0] : '';
  }

  function isIdleAnimation(snapshot) {
    const animation = snapshot && snapshot.animation ? snapshot.animation : 'idle_breathe';
    return animation === 'idle_breathe';
  }

  function defaultClipId(state) {
    return state && state.motionPack && state.motionPack.defaultClip ? state.motionPack.defaultClip : 'idle';
  }

  function shouldUseIdleCadence(state, snapshot) {
    return Boolean(state && state.activeClip === defaultClipId(state) && isIdleAnimation(snapshot));
  }

  function nowMs() {
    return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
  }

  function resetIdleCadence(state) {
    state.idleStillUntil = 0;
    state.idlePlaybackMode = '';
  }

  function playVideoOnce(video) {
    try {
      video.currentTime = 0;
    } catch (error) {}
    const playPromise = video.play();
    if (playPromise && playPromise.catch) {
      playPromise.catch(function () {});
    }
  }

  function restartVideoSmoothly(video) {
    if (!video || video.readyState < 2) return;
    try {
      const start = Math.min(0.033, Math.max(0, (video.duration || 1) - 0.12));
      video.currentTime = start;
    } catch (error) {}
    const playPromise = video.play();
    if (playPromise && playPromise.catch) {
      playPromise.catch(function () {});
    }
  }

  function smoothLoopIfNeeded(state, video, snapshot) {
    if (!video || shouldUseIdleCadence(state, snapshot) || video.readyState < 2 || !video.duration) return;
    const remaining = video.duration - video.currentTime;
    if (remaining <= LOOP_RESTART_PADDING_SECONDS) restartVideoSmoothly(video);
  }

  function playbackRateForSnapshot(state, snapshot) {
    const animation = snapshot && snapshot.animation ? snapshot.animation : '';
    if (state && state.activeClip === 'approval') return 0.72;
    if (animation === 'tap_glass') return 0.72;
    if (animation === 'pause') return 0.82;
    return 1;
  }

  function applyPlaybackRate(video, rate) {
    try {
      video.playbackRate = rate;
    } catch (error) {}
  }

  function configureActiveVideoPlayback(state, snapshot, forceRestart) {
    const video = state.activeClip && state.videos ? state.videos[state.activeClip] : null;
    if (!video) return;
    const idleCadence = shouldUseIdleCadence(state, snapshot);
    applyPlaybackRate(video, playbackRateForSnapshot(state, snapshot));
    video.loop = false;
    if (!idleCadence) {
      resetIdleCadence(state);
      if (forceRestart || video.paused || video.ended) playVideoOnce(video);
      smoothLoopIfNeeded(state, video, snapshot);
      return;
    }
    const now = nowMs();
    if (state.idleStillUntil && now < state.idleStillUntil) {
      if (!video.paused) video.pause();
      return;
    }
    if (state.idleStillUntil && now >= state.idleStillUntil) {
      state.idleStillUntil = 0;
      state.idlePlaybackMode = 'video';
      playVideoOnce(video);
      return;
    }
    if (forceRestart || state.idlePlaybackMode !== 'video') {
      state.idlePlaybackMode = 'video';
      playVideoOnce(video);
    }
  }

  function useIdleStill(state, snapshot) {
    return shouldUseIdleCadence(state, snapshot) && state.idleStillUntil && nowMs() < state.idleStillUntil && state.image;
  }

  function selectGeneratedClip(root, state, snapshot) {
    const nextClip = clipForSnapshot(state, snapshot || state.snapshot || {});
    if (!nextClip) return;
    if (nextClip === state.activeClip) {
      configureActiveVideoPlayback(state, snapshot || state.snapshot || {}, false);
      return;
    }
    const previous = state.activeClip && state.videos ? state.videos[state.activeClip] : null;
    const transitionSource = state.currentSource || previous || state.image;
    if (previous && previous.pause) previous.pause();
    resetIdleCadence(state);
    state.activeClip = nextClip;
    root.dataset.motionClip = nextClip;
    if (transitionSource) {
      state.transitionFromSource = transitionSource;
      state.transitionStartedAt = nowMs();
      state.transitionUntil = state.transitionStartedAt + CLIP_CROSSFADE_MS;
    }
    configureActiveVideoPlayback(state, snapshot || state.snapshot || {}, true);
  }

  function applySnapshot(root, snapshot) {
    if (!root || !snapshot) return;
    root.dataset.mood = snapshot.mood || 'curious';
    root.dataset.animation = snapshot.animation || 'idle_breathe';
    root.style.setProperty('--buddy-energy', String(snapshot.energy || 0));
    root.style.setProperty('--buddy-focus', String(snapshot.focus || 0));
    root.style.setProperty('--buddy-alert', String(snapshot.alert || 0));
    const canvas = qs(root, 'canvas');
    const riveInstance = canvas ? instances.get(canvas.id) : null;
    if (riveInstance && riveInstance.stateMachineInputs) {
      try {
        const inputs = riveInstance.stateMachineInputs('ThothBuddy') || [];
        for (const input of inputs) {
          if (input.name === 'energy') input.value = Number(snapshot.energy || 0);
          if (input.name === 'focus') input.value = Number(snapshot.focus || 0);
          if (input.name === 'alert') input.value = Number(snapshot.alert || 0);
          if (input.name === 'trigger' && input.fire) input.fire();
        }
      } catch (error) {
        setUnavailable(root, 'Buddy state-machine inputs unavailable');
      }
    }

    const generatedInstance = canvas ? generated.get(canvas.id) : null;
    if (generatedInstance) {
      generatedInstance.snapshot = snapshot;
      selectGeneratedClip(root, generatedInstance, snapshot);
      root.classList.add('buddy-generated', 'buddy-ready');
      root.classList.remove('buddy-unavailable');
      setStatus(root, generatedStatus(root, snapshot));
    }
  }

  function drawGlow(ctx, size, snapshot, pulse) {
    const energy = Number((snapshot && snapshot.energy) || 40) / 100;
    const alert = Number((snapshot && snapshot.alert) || 0) / 100;
    const focus = Number((snapshot && snapshot.focus) || 0) / 100;
    const radius = size * (0.28 + energy * 0.08 + pulse * 0.02);
    const gradient = ctx.createRadialGradient(size / 2, size * 0.48, size * 0.05, size / 2, size * 0.48, radius);
    gradient.addColorStop(0, 'rgba(228, 194, 94, 0.42)');
    gradient.addColorStop(0.46, 'rgba(77, 184, 171, ' + (0.18 + focus * 0.2) + ')');
    gradient.addColorStop(1, 'rgba(247, 118, 87, ' + (alert * 0.24) + ')');
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(size / 2, size * 0.5, radius, 0, Math.PI * 2);
    ctx.fill();
  }

  function drawGeneratedSource(ctx, state, source, x, y, width, height, alpha) {
    ctx.save();
    ctx.globalAlpha = Math.max(0, Math.min(1, alpha));
    drawTransparentSource(ctx, state, source, x, y, width, height);
    ctx.restore();
  }

  function drawTransitionedSource(ctx, state, source, x, y, width, height) {
    const now = nowMs();
    const fromSource = state.transitionFromSource;
    if (!fromSource || fromSource === source || !state.transitionUntil || now >= state.transitionUntil) {
      state.transitionFromSource = null;
      state.transitionStartedAt = 0;
      state.transitionUntil = 0;
      drawGeneratedSource(ctx, state, source, x, y, width, height, 1);
      return;
    }
    const rawProgress = (now - state.transitionStartedAt) / CLIP_CROSSFADE_MS;
    const progress = Math.max(0, Math.min(1, rawProgress));
    const eased = progress * progress * (3 - 2 * progress);
    drawGeneratedSource(ctx, state, fromSource, x, y, width, height, 1 - eased);
    drawGeneratedSource(ctx, state, source, x, y, width, height, eased);
  }

  function drawGeneratedBuddy(state, time) {
    const canvas = state.canvas;
    const ctx = state.ctx;
    const snapshot = state.snapshot || {};
    const size = canvas.width;
    const phase = time / 1000;
    const energy = Number(snapshot.energy || 45) / 100;
    const alert = Number(snapshot.alert || 0) / 100;
    const focus = Number(snapshot.focus || 0) / 100;
    const animation = snapshot.animation || '';
    const isApproval = animation === 'tap_glass';
    const idleStill = useIdleStill(state, snapshot);
    const bounce = idleStill ? 0 : Math.sin(phase * (1.4 + energy * 2.2));
    const shake = idleStill ? 0 : (isApproval ? Math.sin(phase * 2.1) * 0.7 : (alert > 0.55 ? Math.sin(phase * 16) * alert * 1.4 : 0));
    const imageSize = size * 0.76;
    const x = (size - imageSize) / 2 + shake;
    const y = (size - imageSize) / 2;

    ctx.clearRect(0, 0, size, size);
    ctx.save();
    drawGlow(ctx, size, snapshot, Math.abs(bounce));
    ctx.shadowColor = alert > 0.5 ? 'rgba(247, 118, 87, 0.45)' : 'rgba(77, 184, 171, 0.34)';
    ctx.shadowBlur = 18 + focus * 18;
    selectGeneratedClip(state.root, state, snapshot);
    const activeVideo = state.activeClip && state.videos ? state.videos[state.activeClip] : null;
    smoothLoopIfNeeded(state, activeVideo, snapshot);
    const source = idleStill ? state.image : (activeVideo && activeVideo.readyState >= 2 ? activeVideo : (state.video && state.video.readyState >= 2 ? state.video : state.image));
    if (source) {
      drawTransitionedSource(ctx, state, source, x, y, imageSize, imageSize);
      state.currentSource = source;
    }
    ctx.restore();

    if (isApproval || alert > 0.6) {
      ctx.save();
      ctx.strokeStyle = isApproval ? 'rgba(228, 194, 94, 0.28)' : 'rgba(247, 118, 87, ' + (0.22 + alert * 0.28) + ')';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(size / 2, size / 2, size * (0.38 + Math.abs(Math.sin(phase * (isApproval ? 1.6 : 3.2))) * (isApproval ? 0.035 : 0.065)), 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }

    if ((snapshot.animation || '').indexOf('celebrate') === 0 || snapshot.mood === 'proud') {
      ctx.save();
      ctx.fillStyle = 'rgba(228, 194, 94, 0.75)';
      for (let i = 0; i < 7; i += 1) {
        const angle = phase * 1.8 + i * 0.9;
        const px = size / 2 + Math.cos(angle) * size * 0.38;
        const py = size / 2 + Math.sin(angle * 1.2) * size * 0.32;
        ctx.fillRect(px, py, 3, 3);
      }
      ctx.restore();
    }

    state.frame = requestAnimationFrame(function (nextTime) {
      drawGeneratedBuddy(state, nextTime);
    });
  }

  function markGeneratedReady(root, state, message) {
    root.classList.add('buddy-generated', 'buddy-ready');
    root.classList.remove('buddy-unavailable');
    setStatus(root, message || 'Generated motion ready');
    if (!state.frame) {
      drawGeneratedBuddy(state, performance.now());
    }
  }

  function initPackVideo(root, state, clipId, clip) {
    if (!clip || !clip.src) return;
    const video = document.createElement('video');
    video.muted = true;
    video.loop = false;
    video.autoplay = false;
    video.playsInline = true;
    video.preload = 'auto';
    video.dataset.clipId = clipId;
    state.videos[clipId] = video;
    video.addEventListener('ended', function () {
      if (state.activeClip !== clipId) return;
      if (!shouldUseIdleCadence(state, state.snapshot || {})) {
        restartVideoSmoothly(video);
        return;
      }
      state.idlePlaybackMode = 'still';
      state.idleStillUntil = nowMs() + IDLE_REPLAY_DELAY_MS;
      video.pause();
    });
    video.addEventListener('loadeddata', function () {
      if (!state.activeClip || clipId === state.motionPack.defaultClip) {
        selectGeneratedClip(root, state, state.snapshot || {});
      }
      if (state.activeClip === clipId) configureActiveVideoPlayback(state, state.snapshot || {}, false);
      markGeneratedReady(root, state, 'Generated motion pack ready');
    });
    video.addEventListener('error', function () {
      delete state.videos[clipId];
      if (Object.keys(state.videos).length === 0 && state.image) {
        markGeneratedReady(root, state, 'Generated art ready');
      }
    });
    video.src = clip.src;
    video.load();
  }

  function initGeneratedRoot(root, canvas, preview, motion, motionPack) {
    const ctx = canvas.getContext('2d');
    const state = { root, canvas, ctx, image: null, video: null, videos: {}, motionPack: motionPack || null, activeClip: '', snapshot: {}, frame: null, keyCanvas: null, keyCtx: null, idleStillUntil: 0, idlePlaybackMode: '', currentSource: null, transitionFromSource: null, transitionStartedAt: 0, transitionUntil: 0 };
    generated.set(canvas.id, state);

    if (preview) {
      const image = new Image();
      image.onload = function () {
        state.image = image;
        markGeneratedReady(root, state, motionPack ? 'Loading generated motion pack' : (motion ? 'Loading generated motion' : 'Generated motion ready'));
      };
      image.onerror = function () {
        if (!motion) setUnavailable(root, 'Generated art failed to load');
      };
      image.src = preview;
    }

    if (motion) {
      const video = document.createElement('video');
      video.muted = true;
      video.loop = false;
      video.autoplay = true;
      video.playsInline = true;
      video.preload = 'auto';
      video.addEventListener('loadeddata', function () {
        state.video = video;
        restartVideoSmoothly(video);
        markGeneratedReady(root, state, 'Generated motion ready');
      });
      video.addEventListener('ended', function () {
        restartVideoSmoothly(video);
      });
      video.addEventListener('error', function () {
        if (state.image) {
          markGeneratedReady(root, state, 'Generated art ready');
        } else {
          setUnavailable(root, 'Generated motion failed to load');
        }
      });
      video.src = motion;
      video.load();
    }

    if (motionPack && motionPack.clips) {
      Object.keys(motionPack.clips).forEach(function (clipId) {
        initPackVideo(root, state, clipId, motionPack.clips[clipId]);
      });
    }

    if (!preview && !motion && !motionPack) {
      setUnavailable(root, 'Generate a companion look in Settings to activate animation');
    }
  }

  function initRiveRoot(root, canvas, riv) {
    if (!window.rive || !window.rive.Rive) {
      setUnavailable(root, 'Rive runtime unavailable');
      return;
    }
    try {
      const instance = new window.rive.Rive({
        src: riv,
        canvas: canvas,
        autoplay: true,
        stateMachines: 'ThothBuddy',
        fit: window.rive.Fit ? window.rive.Fit.Contain : undefined,
        onLoad: function () {
          root.classList.add('buddy-ready');
          root.classList.remove('buddy-unavailable');
          setStatus(root, 'Ready');
          instance.resizeDrawingSurfaceToCanvas();
        },
        onLoadError: function () {
          setUnavailable(root, 'Rive asset failed to load');
        },
      });
      instances.set(canvas.id, instance);
    } catch (error) {
      setUnavailable(root, 'Rive runtime failed to initialize');
    }
  }

  function initRoot(root) {
    if (!root || root.dataset.buddyInitialized === '1') return;
    root.dataset.buddyInitialized = '1';
    const canvas = qs(root, 'canvas');
    if (!canvas) return;
    const preview = root.dataset.preview;
    const motion = root.dataset.motion;
    const motionPack = parseMotionPack(root.dataset.motionPack);
    if (preview || motion || motionPack) {
      initGeneratedRoot(root, canvas, preview, motion, motionPack);
      return;
    }
    const riv = root.dataset.riv;
    if (riv) {
      initRiveRoot(root, canvas, riv);
      return;
    }
    setUnavailable(root, 'Generate a companion look in Settings to activate animation');
  }

  function initAll() {
    document.querySelectorAll('[data-thoth-buddy]').forEach(initRoot);
  }

  window.ThothBuddy = {
    initAll,
    debugState: function () {
      const result = [];
      document.querySelectorAll('[data-thoth-buddy]').forEach(function (root) {
        const canvas = qs(root, 'canvas');
        const state = canvas ? generated.get(canvas.id) : null;
        const videoStates = {};
        if (state && state.videos) {
          Object.keys(state.videos).forEach(function (clipId) {
            const video = state.videos[clipId];
            videoStates[clipId] = {
              readyState: video.readyState,
              paused: video.paused,
              currentSrc: video.currentSrc || video.src || '',
            };
          });
        }
        result.push({
          id: root.id,
          activeClip: state ? state.activeClip : '',
          videoKeys: state && state.videos ? Object.keys(state.videos).sort() : [],
          videoStates,
          defaultClip: state && state.motionPack ? state.motionPack.defaultClip : '',
          animationMap: state && state.motionPack ? state.motionPack.animationMap : {},
          datasetClip: root.dataset.motionClip || '',
          datasetAnimation: root.dataset.animation || '',
          idleStillRemainingMs: state && state.idleStillUntil ? Math.max(0, Math.round(state.idleStillUntil - nowMs())) : 0,
          idlePlaybackMode: state ? state.idlePlaybackMode || '' : '',
          transitionRemainingMs: state && state.transitionUntil ? Math.max(0, Math.round(state.transitionUntil - nowMs())) : 0,
        });
      });
      return result;
    },
    setState: function (snapshot) {
      document.querySelectorAll('[data-thoth-buddy]').forEach(function (root) {
        applySnapshot(root, snapshot);
      });
    },
  };

  document.addEventListener('DOMContentLoaded', initAll);
  setTimeout(initAll, 50);
})();