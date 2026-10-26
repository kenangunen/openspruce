"""One-time login: asks for your Roborock e-mail, sends a code, stores the token.

Usage:  python -m openspruce.login
"""

import asyncio
import json
import os
import sys

from roborock.exceptions import RoborockException
from roborock.web_api import RoborockApiClient

from .config import CACHE_FILE, USER_FILE


async def main() -> int:
    email = input("Roborock account e-mail: ").strip()
    if not email:
        print("An e-mail address is required.")
        return 1

    client = RoborockApiClient(email)
    print("Sending a verification code...")
    try:
        await client.request_code_v4()
        login = client.code_login_v4
    except RoborockException as err:
        print(f"Could not send the code: {err}")
        if "too many" in str(err).lower():
            print("Too many codes requested. Wait 30-60 minutes and try again.")
        return 1

    # Same code can be retried; never request a new one automatically.
    user_data = None
    for attempt in range(3):
        code = input("Enter the 6-digit code from the e-mail: ").strip()
        if not code.isdigit():
            print("The code is digits only, try again.")
            continue
        try:
            user_data = await login(code)
            break
        except RoborockException as err:
            print(f"Code rejected: {err}")
    if user_data is None:
        print("Login failed. Wait a bit and run './install.sh --relogin'.")
        return 1

    USER_FILE.write_text(json.dumps({"email": email, "user_data": user_data.as_dict()}, indent=2))
    os.chmod(USER_FILE, 0o600)
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()  # force fresh device discovery

    # Show which devices the account has, as a sanity check.
    try:
        home = await client.get_home_data_v3(user_data)
        print("\nDevices on this account:")
        products = {p.id: p for p in home.products}
        for dev in home.devices + home.received_devices:
            prod = products.get(dev.product_id)
            print(f"  • {dev.name}  ({prod.model if prod else '?'})")
    except Exception as err:  # noqa: BLE001 - informational only
        print(f"(Could not list devices: {err})")

    print(f"\nSigned in. Credentials saved to: {USER_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
