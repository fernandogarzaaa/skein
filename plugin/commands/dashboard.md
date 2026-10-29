---
description: Start the skein live dashboard and print its URL
---

Check whether anything listens on port 8765 (`lsof -i :8765`). If not, start `skein serve --port 8765` in the background. Print `http://127.0.0.1:8765/`.
