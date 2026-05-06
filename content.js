
(function () {
  'use strict';
  if (window.__subflow_injected) return;
  window.__subflow_injected = true;
  console.log('[LingoSub] Content script loaded on:', location.href);

  const DEFAULTS = {
    lang: 'ps',
    apiKey: '',
    ignoreDefaultSubtitle: false,
  };
  const API_BASE_URL = 'http://localhost:8000'; // Update to your backend host
  const DEBUG_OVERLAY = true;  // toggle off to hide the diagnostic HUD
  let segments = [];
  let targetKey = 'translation';
  let syncInterval = null;
  let activeSegmentIdx = -1;
  let overlayEl = null;
  let overlayTextEl = null;
  let overlayEnglishEl = null;
  let debugHudEl = null;
  let segmentActivatedAt = -1;       // video.currentTime when current segment became active
  let translateBtn = null;
  let isTranslating = false;

  function applyIgnoreDefaultSubtitle(enabled) {
    const target = document.body || document.documentElement;
    if (!target) return;
    target.classList.toggle('subflow-hide-native-captions', !!enabled);
  }
  function getVideoElement() {
    return document.querySelector('video.html5-main-video') || document.querySelector('video');
  }
  function extractVideoId() {
    const params = new URLSearchParams(window.location.search);
    return params.get('v');
  }

  async function fetchWithKey(endpoint, method = 'GET', body = null, apiKey) {
    const options = {
      method,
      headers: {
        'x-api-key': apiKey,
      }
    };
    if (body) {
      if (body instanceof FormData) {
        options.body = body;
      } else {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
    }
    return fetch(`${API_BASE_URL}${endpoint}`, options);
  }

  async function fetchAudioBlob(videoId) {
    const t0 = performance.now();
    const settings = await new Promise(resolve => chrome.storage.local.get(DEFAULTS, resolve));

    // Step 1: Backend resolves stream URL
    console.log('[SubFlow] ⏱ Step 1: Backend resolving stream URL...');
    showToast('Resolving stream URL...');
    const t1 = performance.now();

    const urlResp = await fetchWithKey(
      `/v1/youtube/get-stream-url?videoId=${videoId}`,
      'GET', null, settings.apiKey.trim()
    );
    console.log(`[SubFlow] ⏱ Step 1 done in ${((performance.now()-t1)/1000).toFixed(1)}s`);

    if (!urlResp.ok) {
      const err = await urlResp.json().catch(() => ({}));
      throw new Error(`Stream URL failed: ${err.detail || urlResp.status}`);
    }
    const { stream_url, duration, filesize, video_language, category } = await urlResp.json();
    console.log(`[SubFlow] ℹ️ YouTube Metadata: Lang=${video_language}, Category=${category}`);

    // Step 2: Parallel Chunk Download
    console.log(`[SubFlow] ⏱ Step 2: Starting 5-threaded download (${(filesize/1024/1024).toFixed(2)} MB)...`);
    showToast(`Downloading audio in 5 parallel threads...`);
    const t2 = performance.now();
    const CHUNKS = 5;
    const chunkSize = Math.ceil(filesize / CHUNKS);
    const promises = [];

    async function downloadChunk(i) {
      const start = i * chunkSize;
      const end = i === CHUNKS - 1 ? filesize - 1 : (i + 1) * chunkSize - 1;
      
      const resp = await fetch(stream_url, {
        headers: { 'Range': `bytes=${start}-${end}` }
      });
      if (!resp.ok) throw new Error(`Chunk ${i} failed: ${resp.status}`);
      console.log(`[SubFlow] ⏱ Chunk ${i} started (bytes=${start}-${end})`);
      return resp.arrayBuffer();
    }

    try {
      // Ask background to do the heavy parallel lifting to bypass CORS
      const response = await new Promise((resolve, reject) => {
        chrome.runtime.sendMessage({
          action: 'downloadChunks',
          url: stream_url,
          filesize: filesize
        }, (resp) => {
          if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
          else if (!resp || !resp.success) reject(new Error(resp?.error || 'Unknown error in background'));
          else resolve(resp.blobData);
        });
      });

      // Convert base64 back to blob
      const binaryString = atob(response);
      const bytes = new Uint8Array(binaryString.length);
      for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
      }
      const blob = new Blob([bytes], { type: 'audio/webm' });

      console.log(`[SubFlow] ⏱ Step 2 (Parallel Background) done in ${((performance.now()-t2)/1000).toFixed(1)}s`);
      console.log(`[SubFlow] ⏱ Total time: ${((performance.now()-t0)/1000).toFixed(1)}s — ${(blob.size/1024/1024).toFixed(2)} MB`);
      return { blob, duration };

    } catch (err) {
      console.warn('[SubFlow] Parallel background download failed, falling back to single stream...', err);
      const audioResp = await fetch(stream_url);
      const blob = await audioResp.blob();
      return { blob, duration };
    }
  }





  function findSegmentIndex(t) {
    let lo = 0, hi = segments.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const seg = segments[mid];
      if (t < seg.start) hi = mid - 1;
      else if (t > seg.end) lo = mid + 1;
      else return mid;
    }
    return -1;
  }
  function createOverlay() {
    const old = document.getElementById('subflow-overlay');
    if (old) old.remove();

    const playerContainer = document.querySelector('#movie_player') || document.querySelector('.html5-video-player');
    if (!playerContainer) {
      console.log('[SubFlow] Cannot find player container for overlay');
      return;
    }

    overlayEl = document.createElement('div');
    overlayEl.id = 'subflow-overlay';
    overlayEl.innerHTML = `
      <div id="subflow-overlay-text"></div>
      <div id="subflow-overlay-english"></div>
    `;
    playerContainer.appendChild(overlayEl);

    overlayTextEl = document.getElementById('subflow-overlay-text');
    overlayEnglishEl = document.getElementById('subflow-overlay-english');

    if (DEBUG_OVERLAY) {
      const oldHud = document.getElementById('subflow-debug-hud');
      if (oldHud) oldHud.remove();
      debugHudEl = document.createElement('div');
      debugHudEl.id = 'subflow-debug-hud';
      debugHudEl.style.cssText = `
        position: absolute;
        top: 8px;
        left: 8px;
        z-index: 99999;
        font-family: Consolas, 'Courier New', monospace;
        font-size: 11px;
        line-height: 1.5;
        color: #0f0;
        background: rgba(0,0,0,0.75);
        padding: 4px 8px;
        border-radius: 4px;
        pointer-events: none;
        white-space: pre;
      `;
      playerContainer.appendChild(debugHudEl);
    }
    console.log('[SubFlow] Overlay created');
  }

  function createTranslateButton() {
    const old = document.getElementById('subflow-translate-btn');
    if (old) old.remove();
    translateBtn = null;

    const rightControls = document.querySelector('.ytp-right-controls');
    if (!rightControls) {
      console.log('[SubFlow] .ytp-right-controls not found yet');
      return false;
    }

    translateBtn = document.createElement('button');
    translateBtn.id = 'subflow-translate-btn';
    translateBtn.className = 'ytp-button';
    translateBtn.title = 'SubFlow: Translate Subtitles';
    translateBtn.setAttribute('aria-label', 'SubFlow Translate');
    translateBtn.innerHTML = `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="pointer-events:none;">
        <path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0014.07 6H17V4h-7V2H8v2H1v1.99h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z" fill="white"/>
      </svg>
    `;
    translateBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      onTranslateClick();
    });

    rightControls.insertBefore(translateBtn, rightControls.firstChild);
    console.log('[SubFlow] Translate button injected into player controls');
    return true;
  }

  async function onTranslateClick() {
    if (isTranslating) return;

    const videoId = extractVideoId();
    if (!videoId) {
      showToast('Could not detect video ID.');
      return;
    }

    // Get settings
    const settings = await new Promise((resolve) => {
      chrome.storage.local.get(DEFAULTS, resolve);
    });

    if (!settings.apiKey || !settings.apiKey.trim()) {
      showToast('Missing API key. Please set it in the extension popup.');
      return;
    }

    const apiKey = settings.apiKey.trim();
    const LANG_LABELS = { ps: 'Pashto', bal: 'Balochi', en: 'English', ur: 'Urdu' };
    const langLabel = LANG_LABELS[settings.lang] || settings.lang;
    applyIgnoreDefaultSubtitle(settings.ignoreDefaultSubtitle);

    isTranslating = true;
    if (translateBtn) {
      translateBtn.classList.add('subflow-loading');
      translateBtn.title = 'Processing...';
    }

    try {
      // STEP 1: Check if video is already unlocked or cached
      showToast(`Checking for translations in ${langLabel}...`);
      const checkResp = await fetchWithKey(`/v1/youtube/check?videoId=${videoId}&lang=${settings.lang}`, 'GET', null, apiKey);

      if (!(await handleAuthErrors(checkResp, 'check'))) return;
      if (!checkResp.ok) throw new Error(`Backend check failed (${checkResp.status})`);

      const checkData = await checkResp.json();
      if (showQuotaExceededIfPresent(checkData)) return;

      if (checkData.status === 'unlocked') {
        segments = checkData.segments || [];
        showToast(`✅ Translation loaded instantly!`);
        finalizeTranslation();
        return;
      }

      // STEP 2: If status is 'requires_audio', we must extract and upload
      if (checkData.status === 'requires_audio') {
        showToast(`Capturing audio from YouTube (Bypassing IP bans)...`);
        const { blob, duration } = await fetchAudioBlob(videoId);

        showToast(`Uploading & translating (10–30s for short clips)...`);
        const formData = new FormData();
        formData.append('videoId', videoId);
        formData.append('lang', settings.lang);
        formData.append('duration', duration);
        formData.append('audioFile', blob, 'video_audio.webm');

        const uploadResp = await fetchWithKey('/v1/youtube/upload-audio', 'POST', formData, apiKey);

        if (!(await handleAuthErrors(uploadResp, 'upload'))) return;
        if (!uploadResp.ok) {
          const err = await uploadResp.json().catch(() => ({}));
          throw new Error(`Upload failed (${uploadResp.status}): ${err.detail || ''}`);
        }

        const uploadData = await uploadResp.json();
        if (showQuotaExceededIfPresent(uploadData)) return;

        if (uploadData.status === 'unlocked' && Array.isArray(uploadData.segments)) {
          segments = uploadData.segments;
          showToast(`✅ Translation ready!`);
          finalizeTranslation();
        } else {
          showToast(uploadData.message || 'Translation finished but returned no segments.');
        }
      }

    } catch (err) {
      showToast(`Error: ${err.message}`);
      console.error(err);
    } finally {
      isTranslating = false;
      if (translateBtn) translateBtn.classList.remove('subflow-loading');
    }
  }

  // Surfaces 401 (invalid key) as a user-friendly toast and returns false so
  // the caller can short-circuit. Returns true if no auth error.
  async function handleAuthErrors(resp, label) {
    if (resp.status !== 401) return true;
    const err = await resp.json().catch(() => ({}));
    const reason = err.detail || 'invalid or revoked';
    showToast(`❌ API key rejected (${reason}). Update it in the extension popup.`);
    console.warn(`[SubFlow] 401 from ${label}:`, err);
    return false;
  }

  // If the response body indicates quota exhaustion, render a toast with the
  // numbers from the platform and return true so the caller can stop. Else false.
  function showQuotaExceededIfPresent(data) {
    if (!data || data.status !== 'quota_exceeded') return false;
    const have = Number(data.balance_minutes ?? 0).toFixed(2);
    const need = Number(data.required ?? 0).toFixed(2);
    showToast(`❌ Quota exceeded. Need ${need} min, have ${have} min. Top up on the platform.`);
    return true;
  }

  function finalizeTranslation() {
    if (segments.length === 0) return;
    createOverlay();
    startSync();
    if (translateBtn) {
      translateBtn.classList.add('subflow-active');
      translateBtn.title = 'SubFlow: Subtitles active';
    }
  }

  function startSync() {
    if (syncInterval) clearInterval(syncInterval);
    syncInterval = setInterval(syncSubtitles, 250);
  }

  function syncSubtitles() {
    const video = getVideoElement();
    if (!video || !overlayTextEl) return;

    const t = video.currentTime;
    const idx = findSegmentIndex(t);

    // Update the debug HUD every tick (independent of segment-change short-circuit).
    if (DEBUG_OVERLAY && debugHudEl) {
      const seg = idx >= 0 ? segments[idx] : null;
      if (seg) {
        const gapStart = (t - seg.start);    // how far into the segment we are
        const gapEnd   = (seg.end - t);      // how much of segment remains
        debugHudEl.textContent =
          `t=${t.toFixed(2)}s  seg[${idx}/${segments.length-1}]\n` +
          `start=${seg.start.toFixed(2)}  end=${seg.end.toFixed(2)}\n` +
          `into=+${gapStart.toFixed(2)}s  rem=${gapEnd.toFixed(2)}s`;
      } else {
        // No active segment — find the next upcoming one for context.
        const next = segments.find(s => s.start > t);
        const prev = [...segments].reverse().find(s => s.end < t);
        debugHudEl.textContent =
          `t=${t.toFixed(2)}s  [GAP]\n` +
          (prev ? `prev end=${prev.end.toFixed(2)} (-${(t-prev.end).toFixed(2)}s)\n` : `prev: none\n`) +
          (next ? `next start=${next.start.toFixed(2)} (+${(next.start-t).toFixed(2)}s)` : `next: none`);
      }
    }

    if (idx === activeSegmentIdx) return;
    activeSegmentIdx = idx;
    segmentActivatedAt = t;

    if (DEBUG_OVERLAY && idx >= 0) {
      const seg = segments[idx];
      // Positive lateness = activated AFTER seg.start (subtitle appears late).
      const lateness = t - seg.start;
      console.log(
        `[SubFlow] seg[${idx}] activated at t=${t.toFixed(2)}s  ` +
        `(seg.start=${seg.start.toFixed(2)}, lateness=${lateness >= 0 ? '+' : ''}${lateness.toFixed(2)}s)`
      );
    }

    if (idx >= 0) {
      const seg = segments[idx];
      overlayTextEl.textContent = seg[targetKey] || '';
      overlayTextEl.classList.add('subflow-visible');
      overlayEnglishEl.textContent = seg.en || '';
      overlayEnglishEl.classList.add('subflow-visible');
    } else {
      overlayTextEl.classList.remove('subflow-visible');
      overlayEnglishEl.classList.remove('subflow-visible');
    }
  }

  function showToast(message) {
    let toast = document.getElementById('subflow-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'subflow-toast';
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('subflow-toast-show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      toast.classList.remove('subflow-toast-show');
    }, 5000);
  }

  function waitForPlayerAndInject() {
    if (!location.pathname.startsWith('/watch')) return;

    let attempts = 0;
    const maxAttempts = 30;
    const tryInject = () => {
      attempts++;
      const success = createTranslateButton();
      if (success) {
        console.log(`[SubFlow] Button injected after ${attempts} attempt(s)`);
      } else if (attempts < maxAttempts) {
        setTimeout(tryInject, 1000);
      } else {
        console.log('[SubFlow] Gave up waiting for player controls after 30s');
      }
    };

    tryInject();
  }

  document.addEventListener('yt-navigate-finish', () => {
    console.log('[SubFlow] yt-navigate-finish fired, URL:', location.href);

    segments = [];
    activeSegmentIdx = -1;
    if (syncInterval) clearInterval(syncInterval);
    if (overlayEl) {
      overlayEl.remove();
      overlayEl = null;
      overlayTextEl = null;
      overlayEnglishEl = null;
    }
    if (debugHudEl) {
      debugHudEl.remove();
      debugHudEl = null;
    }

    // Re-inject button on watch pages
    if (location.pathname.startsWith('/watch')) {
      translateBtn = null;
      waitForPlayerAndInject();
    }
  });

  // Keep native captions visibility in sync with settings
  chrome.storage.local.get(DEFAULTS, (settings) => {
    applyIgnoreDefaultSubtitle(settings.ignoreDefaultSubtitle);
  });

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== 'local' || !changes.ignoreDefaultSubtitle) return;
    applyIgnoreDefaultSubtitle(changes.ignoreDefaultSubtitle.newValue);
  });

  // Also watch for yt-page-data-updated (fired on some YT versions)
  document.addEventListener('yt-page-data-updated', () => {
    if (location.pathname.startsWith('/watch') && !document.getElementById('subflow-translate-btn')) {
      console.log('[SubFlow] yt-page-data-updated — re-injecting button');
      translateBtn = null;
      waitForPlayerAndInject();
    }
  });

  // Initial injection
  console.log('[SubFlow] Starting initial injection...');
  waitForPlayerAndInject();

})();
