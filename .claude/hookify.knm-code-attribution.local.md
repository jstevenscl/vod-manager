---
name: knm-code-attribution
enabled: true
event: stop
pattern: .*
---

⚠️ **KNM attribution reminder**

If you made a non-obvious code change this session (a bug fix, a new guard/
limit, a behavior change prompted by a real incident or user request), verify
it carries a `# KNM:` comment right next to the change, stating what changed
and why (include the date). Example:

```python
# KNM: added 2026-09-08 -- pipe/colon detection alone missed real "FR -
# Title" provider naming, so FR/RU items using this format weren't being
# auto-archived and were showing up in the catalog/Duplicate Finder.
```

Don't over-apply this to every trivial edit — same bar as "when to write a
comment at all" (a hidden constraint, a subtle invariant, a workaround, a
non-obvious reason), just tagged `# KNM:` instead of left untagged. Purely
mechanical/refactor changes with no real reasoning behind them don't need one.
