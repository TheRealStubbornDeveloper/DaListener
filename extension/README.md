# DaListener Chromium extension

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable **Developer mode** and choose **Load unpacked**.
3. Select this `extension` directory, or the installed `BrowserExtension` folder opened from the DaListener dashboard.
4. Start DaListener, copy its pairing data, paste it into the extension options, and save.
5. Open each audio tab and click the DaListener icon. `REC` means that tab has an independent capture stream; click again to stop it.

Known meeting sites start directly. YouTube and other media or unfamiliar sites show a confirmation describing the source and OpenAI processing. Approval can be remembered for that website and removed later under **Capture settings** in the dashboard.

The extension captures tab audio only. It never requests microphone access and never receives the OpenAI API key.

After pulling an extension update, use **Reload** on `chrome://extensions` and pair once more. Version 0.2.1 keeps the local token and stable port across normal DaListener restarts. `REC` appears only after the full capture handshake succeeds; hover an `ERR` badge or open extension options to see the last error.
