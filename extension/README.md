# DaListener Chromium extension

1. Open `chrome://extensions` (or `edge://extensions`).
2. Enable **Developer mode** and choose **Load unpacked**.
3. Select this `extension` directory.
4. Start the DaListener dashboard, copy its pairing data, and paste it into the extension options.
5. Open each Zoom or other meeting tab and click the DaListener extension icon. `REC` means that tab has its own capture stream. Click again to stop it.

The extension captures tab audio only. It never requests microphone access and never receives the OpenAI API key.
