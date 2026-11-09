---
paths:
  - "**/*.py"
---

# Long-running cloud jobs

Long Earth Engine exports and Batch jobs run independently of the chat session.

Immediately after submitting an authorized job, record and report:

1. The Earth Engine or GCP task id.
2. The Cloud Storage output path.
3. The command or URL that checks its status.
4. How to cancel it.

Then return control. Do not poll, sleep, or wait for the job in the chat.
