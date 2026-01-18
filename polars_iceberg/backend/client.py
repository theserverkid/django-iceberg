"""
Database Client - CLI interface for the Polars + Iceberg backend.
"""

from django.db.backends.base.client import BaseDatabaseClient


class DatabaseClient(BaseDatabaseClient):
    """
    Database client for the Polars + Iceberg backend.

    Since this is a file-based backend without a traditional CLI,
    this is mostly a stub.
    """

    executable_name = "python"

    def runshell(self, parameters=None):
        """
        Run an interactive shell.

        Opens a Python shell with the Iceberg connection available.
        """
        import code
        import sys

        # Get the connection
        connection = self.connection

        # Set up the namespace
        namespace = {
            "connection": connection,
            "iceberg": connection.iceberg,
            "pl": __import__("polars"),
        }

        # Print instructions
        print("Polars + Iceberg Interactive Shell")
        print("=" * 40)
        print("Available objects:")
        print("  - connection: Django database connection")
        print("  - iceberg: IcebergManager instance")
        print("  - pl: Polars module")
        print()
        print("Examples:")
        print("  # List tables")
        print("  list(iceberg.catalog.list_tables('default'))")
        print()
        print("  # Read a table")
        print("  df = iceberg.scan('myapp_task')")
        print("  print(df)")
        print()
        print("  # Time travel query")
        print("  df = iceberg.scan('myapp_task', snapshot_id=123)")
        print()

        # Start interactive console
        code.interact(local=namespace)
