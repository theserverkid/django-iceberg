"""
Database Creation - Handles test database creation for Polars + Iceberg.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from django.db.backends.base.creation import BaseDatabaseCreation


class DatabaseCreation(BaseDatabaseCreation):
    """
    Database creation for the Polars + Iceberg backend.

    Handles creating and destroying test databases (Iceberg warehouses).
    """

    def _get_test_db_name(self) -> str:
        """Get the name of the test database."""
        return self.connection.settings_dict.get("TEST", {}).get(
            "NAME", self._get_default_test_db_name()
        )

    def _get_default_test_db_name(self) -> str:
        """Get the default test database name."""
        db_name = self.connection.settings_dict.get("NAME", "")
        if isinstance(db_name, Path):
            db_name = str(db_name)

        if db_name:
            # Add _test suffix to the warehouse path
            return db_name.replace("/warehouse", "/test_warehouse")

        return "./data/test_warehouse"

    def _create_test_db(
        self, verbosity: int = 1, autoclobber: bool = False, keepdb: bool = False
    ) -> str:
        """
        Create the test database (Iceberg warehouse).

        Args:
            verbosity: Verbosity level
            autoclobber: Whether to automatically destroy existing db
            keepdb: Whether to keep existing db

        Returns:
            The test database name
        """
        test_database_name = self._get_test_db_name()

        if verbosity >= 1:
            test_db_repr = f"test warehouse '{test_database_name}'"
            print(f"Creating {test_db_repr}...")

        # Create test warehouse directory
        test_path = Path(test_database_name)

        if test_path.exists() and not keepdb:
            if autoclobber:
                shutil.rmtree(test_path)
            else:
                # Ask for confirmation
                confirm = input(
                    f"Test warehouse {test_database_name} already exists. "
                    "Delete it? [y/N] "
                )
                if confirm.lower() == "y":
                    shutil.rmtree(test_path)

        test_path.mkdir(parents=True, exist_ok=True)

        # Update connection settings for test database
        self.connection.settings_dict["NAME"] = test_database_name
        self.connection.settings_dict["WAREHOUSE"] = test_database_name

        # Reset connection to use new settings
        self.connection.close()

        return test_database_name

    def _destroy_test_db(self, test_database_name: str, verbosity: int = 1) -> None:
        """
        Destroy the test database (Iceberg warehouse).

        Args:
            test_database_name: Name of the test database
            verbosity: Verbosity level
        """
        if verbosity >= 1:
            print(f"Destroying test warehouse '{test_database_name}'...")

        test_path = Path(test_database_name)
        if test_path.exists():
            shutil.rmtree(test_path)

    def create_test_db(
        self,
        verbosity: int = 1,
        autoclobber: bool = False,
        serialize: bool = True,
        keepdb: bool = False,
    ) -> str:
        """
        Create the test database.
        """
        return self._create_test_db(
            verbosity=verbosity, autoclobber=autoclobber, keepdb=keepdb
        )

    def destroy_test_db(
        self,
        old_database_name: str = None,
        verbosity: int = 1,
        keepdb: bool = False,
        suffix: str = None,
    ) -> None:
        """
        Destroy the test database.
        """
        if keepdb:
            return

        test_database_name = self._get_test_db_name()
        self._destroy_test_db(test_database_name, verbosity)

    def _clone_test_db(self, suffix: str, verbosity: int = 1, keepdb: bool = False):
        """
        Clone the test database for parallel testing.
        """
        source_path = Path(self._get_test_db_name())
        target_path = Path(f"{source_path}_{suffix}")

        if target_path.exists() and not keepdb:
            shutil.rmtree(target_path)

        if source_path.exists():
            shutil.copytree(source_path, target_path)
        else:
            target_path.mkdir(parents=True, exist_ok=True)

        return str(target_path)
