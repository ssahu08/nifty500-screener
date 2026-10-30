"""SQLite persistence for strategy run results with rolling retention.

Implemented in Phase 3. Stores each run's signals and automatically deletes
rows older than the configured retention window (default 14 days, FR-11).
"""
