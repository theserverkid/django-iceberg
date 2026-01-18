"""
Time Travel API - Query historical data from Iceberg tables.

This module provides Django model managers and mixins for time travel queries.

Usage:
    from polars_iceberg.timetravel import TimeTravelMixin

    class Task(TimeTravelMixin, models.Model):
        title = models.CharField(max_length=200)

    # Query current data
    Task.objects.all()

    # Query data as it existed at a specific time
    Task.objects.as_of(datetime(2024, 1, 15, 10, 30))

    # Get complete history of a record
    Task.objects.history(pk=123)

    # List all snapshots
    Task.objects.snapshots()
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from django.db import models
from django.db.models import Manager
from django.db.models.query import QuerySet

if TYPE_CHECKING:
    from .backend.iceberg_manager import IcebergManager

T = TypeVar("T", bound=models.Model)


class TimeTravelQuerySet(QuerySet, Generic[T]):
    """
    QuerySet with time travel capabilities.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._snapshot_id: int | None = None
        self._as_of_timestamp: datetime | None = None

    def _clone(self):
        c = super()._clone()
        c._snapshot_id = self._snapshot_id
        c._as_of_timestamp = self._as_of_timestamp
        return c

    def as_of(self, timestamp: datetime) -> "TimeTravelQuerySet[T]":
        """
        Return a queryset representing the state at a specific point in time.

        Args:
            timestamp: The point in time to query

        Returns:
            QuerySet filtered to that point in time
        """
        clone = self._clone()
        clone._as_of_timestamp = timestamp
        return clone

    def at_snapshot(self, snapshot_id: int) -> "TimeTravelQuerySet[T]":
        """
        Return a queryset at a specific snapshot.

        Args:
            snapshot_id: The Iceberg snapshot ID

        Returns:
            QuerySet at that snapshot
        """
        clone = self._clone()
        clone._snapshot_id = snapshot_id
        return clone


class TimeTravelManager(Manager):
    """
    Model manager with time travel capabilities.

    Provides methods to query historical data from Iceberg tables.
    """

    def get_queryset(self) -> TimeTravelQuerySet:
        """Return a TimeTravelQuerySet."""
        return TimeTravelQuerySet(self.model, using=self._db)

    @property
    def _iceberg(self) -> "IcebergManager":
        """Get the Iceberg manager from the database connection."""
        from django.db import connection
        return connection.iceberg

    def as_of(self, timestamp: datetime) -> list[Any]:
        """
        Query data as it existed at a specific point in time.

        Args:
            timestamp: The point in time to query

        Returns:
            List of model instances representing the historical state

        Example:
            # Get all tasks as they existed yesterday
            tasks = Task.objects.as_of(datetime(2024, 1, 14, 12, 0, 0))
        """
        table_name = self.model._meta.db_table

        try:
            df = self._iceberg.scan(table_name, timestamp=timestamp)
            return self._dataframe_to_instances(df)
        except Exception:
            return []

    def at_snapshot(self, snapshot_id: int) -> list[Any]:
        """
        Query data at a specific Iceberg snapshot.

        Args:
            snapshot_id: The Iceberg snapshot ID

        Returns:
            List of model instances at that snapshot
        """
        table_name = self.model._meta.db_table

        try:
            df = self._iceberg.scan(table_name, snapshot_id=snapshot_id)
            return self._dataframe_to_instances(df)
        except Exception:
            return []

    def history(self, pk: Any) -> list[dict[str, Any]]:
        """
        Get the complete history of a specific record.

        This returns all versions of the record across all snapshots.

        Args:
            pk: Primary key of the record

        Returns:
            List of dicts with snapshot info and record data

        Example:
            history = Task.objects.history(pk=123)
            for version in history:
                print(f"{version['timestamp']}: {version['data']}")
        """
        table_name = self.model._meta.db_table
        pk_column = self.model._meta.pk.column

        history = []
        snapshots = self._iceberg.get_snapshots(table_name)

        for snapshot in snapshots:
            try:
                df = self._iceberg.scan(
                    table_name,
                    snapshot_id=snapshot["snapshot_id"]
                )

                # Filter for the specific record
                if pk_column in df.columns:
                    import polars as pl
                    record = df.filter(pl.col(pk_column) == pk)

                    if len(record) > 0:
                        history.append({
                            "snapshot_id": snapshot["snapshot_id"],
                            "timestamp": snapshot["timestamp"],
                            "summary": snapshot["summary"],
                            "data": record.to_dicts()[0],
                        })

            except Exception:
                continue

        return history

    def snapshots(self) -> list[dict[str, Any]]:
        """
        List all available snapshots for this table.

        Returns:
            List of snapshot metadata dicts

        Example:
            for snapshot in Task.objects.snapshots():
                print(f"Snapshot {snapshot['snapshot_id']} at {snapshot['timestamp']}")
        """
        table_name = self.model._meta.db_table

        try:
            return self._iceberg.get_snapshots(table_name)
        except Exception:
            return []

    def rollback_to(self, snapshot_id: int) -> None:
        """
        Rollback the table to a specific snapshot.

        WARNING: This will overwrite current data with historical data.

        Args:
            snapshot_id: The snapshot ID to rollback to
        """
        table_name = self.model._meta.db_table

        # Read data from the snapshot
        df = self._iceberg.scan(table_name, snapshot_id=snapshot_id)

        # Overwrite current data
        self._iceberg.overwrite(table_name, df)

    def diff(
        self,
        from_snapshot: int | datetime,
        to_snapshot: int | datetime | None = None,
    ) -> dict[str, list[Any]]:
        """
        Get the difference between two snapshots.

        Args:
            from_snapshot: Starting snapshot (ID or timestamp)
            to_snapshot: Ending snapshot (ID, timestamp, or None for current)

        Returns:
            Dict with 'added', 'removed', and 'modified' lists
        """
        import polars as pl

        table_name = self.model._meta.db_table
        pk_column = self.model._meta.pk.column

        # Get 'from' data
        if isinstance(from_snapshot, datetime):
            from_df = self._iceberg.scan(table_name, timestamp=from_snapshot)
        else:
            from_df = self._iceberg.scan(table_name, snapshot_id=from_snapshot)

        # Get 'to' data
        if to_snapshot is None:
            to_df = self._iceberg.scan(table_name)
        elif isinstance(to_snapshot, datetime):
            to_df = self._iceberg.scan(table_name, timestamp=to_snapshot)
        else:
            to_df = self._iceberg.scan(table_name, snapshot_id=to_snapshot)

        # Calculate diff
        from_ids = set(from_df[pk_column].to_list()) if pk_column in from_df.columns else set()
        to_ids = set(to_df[pk_column].to_list()) if pk_column in to_df.columns else set()

        added_ids = to_ids - from_ids
        removed_ids = from_ids - to_ids
        common_ids = from_ids & to_ids

        # Find modified records
        modified_ids = set()
        for pk in common_ids:
            from_row = from_df.filter(pl.col(pk_column) == pk).to_dicts()
            to_row = to_df.filter(pl.col(pk_column) == pk).to_dicts()
            if from_row != to_row:
                modified_ids.add(pk)

        return {
            "added": [self._dataframe_to_instances(to_df.filter(pl.col(pk_column).is_in(list(added_ids))))],
            "removed": [self._dataframe_to_instances(from_df.filter(pl.col(pk_column).is_in(list(removed_ids))))],
            "modified": [self._dataframe_to_instances(to_df.filter(pl.col(pk_column).is_in(list(modified_ids))))],
        }

    def _dataframe_to_instances(self, df) -> list[Any]:
        """
        Convert a Polars DataFrame to model instances.

        Args:
            df: Polars DataFrame

        Returns:
            List of model instances
        """
        instances = []

        for row in df.to_dicts():
            # Create instance without saving
            instance = self.model(**row)
            instance._state.adding = False
            instance._is_historical = True
            instances.append(instance)

        return instances


class TimeTravelMixin(models.Model):
    """
    Mixin that adds time travel capabilities to a Django model.

    Usage:
        class Task(TimeTravelMixin, models.Model):
            title = models.CharField(max_length=200)
            completed = models.BooleanField(default=False)

        # Current data
        Task.objects.all()

        # Historical data
        Task.objects.as_of(datetime(2024, 1, 15))

        # Record history
        Task.objects.history(pk=123)
    """

    objects = TimeTravelManager()

    class Meta:
        abstract = True

    def get_history(self) -> list[dict[str, Any]]:
        """
        Get the complete history of this instance.

        Returns:
            List of historical versions
        """
        if self.pk is None:
            return []
        return self.__class__.objects.history(pk=self.pk)

    def as_of(self, timestamp: datetime) -> "TimeTravelMixin | None":
        """
        Get this instance as it existed at a specific time.

        Args:
            timestamp: The point in time to query

        Returns:
            The historical instance, or None if it didn't exist
        """
        if self.pk is None:
            return None

        historical = self.__class__.objects.as_of(timestamp)
        for instance in historical:
            if instance.pk == self.pk:
                return instance
        return None
