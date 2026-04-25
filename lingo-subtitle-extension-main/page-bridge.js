// page-bridge.js — runs in MAIN world at document_start
// Listens for a request from the content script and responds with the audio URL

window.addEventListener('message', function(event) {
  if (!event.data || event.data.type !== 'SUBFLOW_REQUEST_AUDIO_URL') return;

  try {
    // Try multiple sources where YouTube stores player data
    let formats = null;
    let duration = 0;

    // Source 1: ytInitialPlayerResponse (most common)
    const pr = window.ytInitialPlayerResponse;
    if (pr && pr.streamingData && pr.streamingData.adaptiveFormats) {
      formats = pr.streamingData.adaptiveFormats;
      duration = parseFloat((pr.videoDetails && pr.videoDetails.lengthSeconds) || 0);
      console.log('[SubFlow Bridge] Found formats in ytInitialPlayerResponse:', formats.length);
    }

    // Source 2: ytplayer.config (older fallback)
    if (!formats && window.ytplayer && window.ytplayer.config) {
      try {
        const args = window.ytplayer.config.args;
        if (args && args.player_response) {
          const pr2 = typeof args.player_response === 'string' ? JSON.parse(args.player_response) : args.player_response;
          if (pr2 && pr2.streamingData && pr2.streamingData.adaptiveFormats) {
            formats = pr2.streamingData.adaptiveFormats;
            duration = parseFloat((pr2.videoDetails && pr2.videoDetails.lengthSeconds) || 0);
            console.log('[SubFlow Bridge] Found formats in ytplayer.config:', formats.length);
          }
        }
      } catch(e) { console.log('[SubFlow Bridge] ytplayer.config parse failed:', e.message); }
    }

    if (!formats || formats.length === 0) {
      window.postMessage({ type: 'SUBFLOW_PLAYER_RESPONSE', error: 'No adaptiveFormats found in any source' }, '*');
      return;
    }

    // Log all formats for debugging
    const audioFormats = formats.filter(f => f.mimeType && f.mimeType.includes('audio/'));
    console.log('[SubFlow Bridge] Available audio formats:', audioFormats.map(f => ({
      mimeType: f.mimeType, hasUrl: !!f.url, hasCipher: !!(f.signatureCipher || f.cipher)
    })));

    // Try to find one with a direct URL (no cipher needed)
    const audio = audioFormats.find(f => f.url && f.mimeType.includes('audio/webm'))
               || audioFormats.find(f => f.url && f.mimeType.includes('audio/mp4'))
               || audioFormats.find(f => f.url); // any audio with direct URL

    if (!audio) {
      window.postMessage({ 
        type: 'SUBFLOW_PLAYER_RESPONSE', 
        error: `All ${audioFormats.length} audio formats are cipher-protected. Cannot extract URL without JS decryption.` 
      }, '*');
      return;
    }

    window.postMessage({
      type: 'SUBFLOW_PLAYER_RESPONSE',
      payload: {
        url: audio.url,
        mimeType: audio.mimeType,
        duration: duration
      }
    }, '*');

  } catch (e) {
    window.postMessage({ type: 'SUBFLOW_PLAYER_RESPONSE', error: e.message }, '*');
  }
});
