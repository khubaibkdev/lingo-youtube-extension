# Lingo Subtitle — YouTube Subtitle Translator (Chrome Extension)

Translate YouTube subtitles into Pashto or Balochi in real time using a local backend (Whisper + M2M100). The extension injects a translate button into the YouTube player, fetches translated segments from your backend, and overlays the translated subtitles (plus English) on the video.

## Features
- One-click subtitle translation on YouTube watch pages.
- Pashto and Balochi targets.
- Overlay rendering with synced timing.
- Popup settings for language, backend URL, and force Whisper mode.
- Toast status notifications and loading state.

## Requirements
- Google Chrome (Manifest V3 supported).
- A running backend server that exposes:
  - `POST /translate` for Pashto
  - `POST /translate-ast` for Balochi

The extension expects the backend to return JSON like:
```json
{
  "segments": [
    {
      "start": 0.0,
      "end": 2.5,
      "en": "Hello",
      "ps": "...",
      "ast": "..."
    }
  ]
}
```

## Install (Development)
1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click "Load unpacked".
4. Select this project folder: `/home/umar/work/lingo-subtitle-extension`.

## Usage
1. Open a YouTube watch page.
2. Click the SubFlow translate button (added to the right controls).
3. Wait for translation to complete. Subtitles will appear on the video.
4. Open the extension popup to change language or backend URL.

## Settings (Popup)
- `Language`: Pashto (`ps`) or Balochi (`ast`).
- `Force Whisper`: When enabled, the backend is instructed to re-run Whisper.
- `Backend URL`: Base URL for your translation backend (no trailing slash needed).

## Permissions
- `activeTab` and `storage` are used for interaction and saving settings.
- Host permissions are configured for local backends:
  - `http://192.168.18.122:8000/*`
  - `http://localhost:8000/*`

Update `manifest.json` if you want to point to a different backend host.

## Project Structure
- `manifest.json` Extension manifest (MV3).
- `content.js` Injected script that adds the translate button, calls the backend, and syncs subtitles.
- `content.css` Overlay styles and UI affordances.
- `popup.html` UI for settings.
- `popup.js` Logic for storing and loading settings.
- `icons/` Extension icons.

## Troubleshooting
- Button not showing: Reload the page or ensure you are on a YouTube `/watch` URL.
- No subtitles: Confirm the backend is running and returning `segments`.
- CORS or network errors: Ensure your backend allows requests from the extension and is reachable from the browser.

## Development Notes
- The content script uses YouTube SPA events (`yt-navigate-finish`, `yt-page-data-updated`) to re-inject the button.
- Subtitle syncing runs every 250ms and uses binary search to find the active segment.

## Privacy
- The extension only sends the YouTube video URL and translation options to your backend.
- No analytics or third-party services are used by the extension itself.
