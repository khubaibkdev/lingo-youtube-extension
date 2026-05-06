chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'downloadChunks') {
    handleParallelDownload(request.url, request.filesize)
      .then(blobData => sendResponse({ success: true, blobData }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true; // Keep channel open for async response
  }
});

async function handleParallelDownload(streamUrl, filesize) {
  const CHUNKS = 5;
  const chunkSize = Math.ceil(filesize / CHUNKS);
  const promises = [];

  console.log(`[Background] Starting parallel fetch for ${filesize} bytes`);

  for (let i = 0; i < CHUNKS; i++) {
    const start = i * chunkSize;
    const end = i === CHUNKS - 1 ? filesize - 1 : (i + 1) * chunkSize - 1;

    promises.push(
      fetch(streamUrl, {
        headers: { 'Range': `bytes=${start}-${end}` }
      }).then(r => {
        if (!r.ok) throw new Error(`Chunk ${i} failed: ${r.status}`);
        return r.arrayBuffer();
      })
    );
  }

  const results = await Promise.all(promises);
  
  // Convert arrayBuffers to base64 to send back over messaging
  // Blobs cannot be sent directly across runtime messages reliably
  const totalBuffer = combineBuffers(results);
  return arrayBufferToBase64(totalBuffer);
}

function combineBuffers(buffers) {
  let totalLength = buffers.reduce((acc, b) => acc + b.byteLength, 0);
  let combined = new Uint8Array(totalLength);
  let offset = 0;
  for (let b of buffers) {
    combined.set(new Uint8Array(b), offset);
    offset += b.byteLength;
  }
  return combined.buffer;
}

function arrayBufferToBase64(buffer) {
  let binary = '';
  let bytes = new Uint8Array(buffer);
  let len = bytes.byteLength;
  for (let i = 0; i < len; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}
