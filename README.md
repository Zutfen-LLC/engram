# Engram

> Shared structured memory for multi-agent AI teams.

Engram is a standalone memory service that gives teams of AI agents a shared, structured, durable brain. It's not a flat key-value memory store — it's an organized knowledge system with taxonomy, relationships, temporal validity, and per-agent scoping.

An **engram** is the physical trace a memory leaves in brain tissue — the literal substrate of stored memory.

## Why Engram?

Most agent memory layers (mem0, Letta, Zep) store flat facts per agent. Engram is built for **teams** of agents that need to share knowledge, organize it by abstraction layer, and reason about how it changes over time.

| Feature | Flat stores | Engram |
|---------|------------|--------|
| Memory model | Flat facts | Structured taxonomy (wings/rooms) |
| Relationships | None | Knowledge graph with temporal validity |
| Multi-agent | Per-agent silos | Workspaces with visibility levels |
| Audit trail | Overwrites | Append-first with supersession |
| Cross-links | None | Tunnels between domains |
| Self-hostable | Varies | Docker Compose, one command |

## Quickstart (self-hosted)

```bash
git clone https://github.com/Zutfen-LLC/engram.git
cd engram
docker compose up -d
```

This starts Postgres (with pgvector) and the Engram service. See `docs/design.md` for the full architecture.

## Status

**Phase 1 — Core service (in development)**

- [ ] Postgres schema + migrations
- [ ] REST API (remember, recall, search, KG, export)
- [ ] Python SDK
- [ ] Migration importers (dry-run)
- [ ] Docker Compose deployment

Phase 2 will integrate with Hermes. Phase 3 will prepare for open-source release.

## License

MIT
