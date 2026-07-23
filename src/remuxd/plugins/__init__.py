"""Optional resolver plugins.

A plugin turns some external identifier into a playable source URL that can be
handed to ``/start``. Each module exposes ``configured() -> bool`` and a resolver
callable; the server mounts it under ``/resolve`` only when configured, so the
core service never requires the plugin's dependencies or secrets.
"""
