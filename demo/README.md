# Recorded runs

`recorded_run.json` is a frame-by-frame capture of a **real** run — the issues
that were actually filed and the session objects the Devin API actually
returned, polled on the same cadence the reconciler uses.

`MODE=replay` feeds those frames to the same reconciler, store, and metrics code
that runs live. Nothing in the offline demo is synthetic: no outcome is invented
and no failure is engineered. If the recording is absent the dashboard renders
empty rather than fabricating a run.

Produce one with:

```bash
python -m scripts.record_run --minutes 60
```
