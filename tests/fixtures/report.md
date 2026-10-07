# Quarterly delivery review

A short **Markdown** report rendered by Hermes Artifacts.

| Stream | Status | Notes |
| --- | --- | --- |
| Service | Done | FastAPI + SQLite |
| Dashboard | Done | Native plugin page |
| Sharing | Done | Expiring links |

```python
def share(artifact, days=7):
    return grant(artifact, expires_in=days * 86400)
```

> Raw HTML is shown as text, not executed:

<script>document.body.innerHTML = "pwned"</script>

[Unsafe link](javascript:alert(1)) and a [normal link](https://hermes-agent.nousresearch.com/).
