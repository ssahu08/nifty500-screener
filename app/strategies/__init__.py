"""Strategy framework: base class, metadata, registry and scheduling.

Implemented in Phase 3. Each concrete strategy lives in its own module and
declares metadata (name, description, category, universe, schedule). The engine
discovers strategies from the registry, so adding a new strategy requires only
a new module + metadata entry.
"""
