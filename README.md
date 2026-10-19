# core-dispatch-runtime

Asynchronous distributed task processing and queue coordinator.

## Overview

Modular orchestration runtime for asynchronous worker pools, task scheduling, and state synchronization.

### Modules

- `api/` — Service endpoints and API routing
- `bot/` — Worker notification interface
- `config/` — Environment and application configuration
- `db/` — Database schemas and migrations
- `executor/` — Task execution engine and runtime clients
- `services/` — Business logic and queue management
- `tests/` — Automated unit and integration tests

## Quick Start

### Installation

```bash
pip install -r requirements.txt
```

### Running

```bash
python run_server.py
```
