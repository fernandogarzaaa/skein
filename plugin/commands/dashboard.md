---
description: Start the skein live dashboard and print its URL
---

Check for a listener on port 8765 with `lsof -nP -iTCP:8765 -sTCP:LISTEN`.

- No listener: start `skein serve --port 8765` in the background and print `http://127.0.0.1:8765/`.
- A `skein serve` process holds it: print the URL; it is already running.
- Anything else holds it: do not print that URL. Start `skein serve --port 8766` instead (or the next free port) and print that URL, saying why.
