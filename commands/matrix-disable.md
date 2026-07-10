---
description: Disable Matrix bridge — stop forwarding messages to your phone
allowed-tools: [Bash]
---

Disable the Matrix bridge so Claude Code messages are NOT forwarded to Matrix.

Run:
```bash
rm -f ~/.ccmatrix/enabled
```

Then confirm to the user: "Matrix bridge disabled. Messages will no longer be forwarded."
