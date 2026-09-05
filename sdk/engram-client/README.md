
## Structured extraction

```python
from engram_client import EngramClient, ExtractRequest, ExtractionMessage

async with EngramClient(base_url, api_key) as client:
    result = await client.extract(ExtractRequest(
        messages=[ExtractionMessage(
            message_id="turn-12-user", role="user",
            content="I no longer prefer dark mode.",
        )],
        source_type="sync_turn",
        mode="write_proposed",
        idempotency_key="session-8-turn-12",
    ))
    stored = await client.get_extraction(result.receipt.run_id)
```

Use `preview` to inspect extraction without memory mutation. Write mode
requires a stable retry key. Candidates report individual outcomes. New items
start as proposed. Attribution and taxonomy do not grant admission authority.
The configured extraction provider must be available. Provider failure returns
`503`; it does not fall back to sentence splitting on the server.
