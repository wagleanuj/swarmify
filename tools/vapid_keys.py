"""Print a new VAPID key pair for Web Push, as the env lines the server reads (issue 459).

    uv run python tools/vapid_keys.py >> ~/.swarmify/.env

Without these the server makes a pair on first start and keeps it in `<data_dir>/vapid.json`.
Keep the pair once browsers have subscribed: a new one silently ends every subscription.
"""

from swarmify_notifications.push import generate_keys

if __name__ == "__main__":
    public, private = generate_keys()
    print(f"SWARMIFY_VAPID_PUBLIC_KEY={public}")
    print(f"SWARMIFY_VAPID_PRIVATE_KEY={private}")
    print("SWARMIFY_VAPID_SUBJECT=mailto:you@example.com")
