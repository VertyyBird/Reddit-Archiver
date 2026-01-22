The following are a set of tasks set to the side for later. Keep it formatted as a numbered list with no priority given to the order.

1. Archive.today submissions that return 2xx without a URL are treated as success but later marked failed and never rechecked; this can create false negatives and permanently "bad" status.
2. Config parsing uses direct int()/float() without validation; a typo like interval=abc will crash the run.
3. Missing config file is silently ignored and the app falls back to ChatGPT, which can hide misconfigurations.
4. Wayback status labels show "pending" even when ok==0 and a check already happened, which reads like queued rather than failed.
