# Tools

This directory contains standalone Python scripts for various administrative, testing, and utility purposes. These scripts are not part of the main application runtime but provide essential tools for development, setup, and debugging.

## Scripts

-   **`add_user.py`**: A script to guide a new user through the Spotify authentication process.
-   **`generate_vapid_keys.py`**: Generates the `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` pair that enables Web Push (team and staff notifications). Run once per deployment.
-   **`get_spotify_devices.py`**: Fetches and lists available Spotify devices for a given user.
-   **`migrate_remove_legacy.py`**: A script to perform data migrations, such as cleaning up old data formats.
