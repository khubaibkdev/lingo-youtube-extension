// =========================================================
//  SubFlow Chrome Extension — Popup Script
// =========================================================

const DEFAULTS = {
  lang: 'ps',
  apiKey: '',
  ignoreDefaultSubtitle: false,
};

const langEl = document.getElementById('lang');
const apiKeyEl = document.getElementById('apiKey');
const ignoreDefaultSubtitleEl = document.getElementById('ignoreDefaultSubtitle');
const saveBtn = document.getElementById('saveBtn');
const statusEl = document.getElementById('status');

// Load saved settings
chrome.storage.local.get(DEFAULTS, (data) => {
  langEl.value = data.lang;
  apiKeyEl.value = data.apiKey;
  ignoreDefaultSubtitleEl.checked = data.ignoreDefaultSubtitle;
});

// Save
saveBtn.addEventListener('click', () => {
  const settings = {
    lang: langEl.value,
    apiKey: apiKeyEl.value.trim(),
    ignoreDefaultSubtitle: ignoreDefaultSubtitleEl.checked,
  };

  chrome.storage.local.set(settings, () => {
    statusEl.classList.add('show');
    setTimeout(() => statusEl.classList.remove('show'), 2000);
  });
});
