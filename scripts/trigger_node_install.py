import requests
import json
import sys

# Umbrel 1.x tRPC CLI client
BASE_URL = "http://localhost:3001/trpc"
PASSWORD = "p9ZF3iPcjiKYOwutzNM6"

def call_trpc(endpoint, input_data, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    url = f"{BASE_URL}/{endpoint}"
    # tRPC expects input as a JSON string in a query param for GET, or body for POST
    # 'batch=1' is standard for Umbrel's tRPC implementation
    payload = {"0": input_data}
    
    response = requests.post(f"{url}?batch=1", headers=headers, json=payload)
    if response.status_code != 200:
        print(f"Error: {response.status_code} - {response.text}")
        return None
    
    return response.json()[0]["result"]["data"]

def main():
    print("--- Umbrel CLI Install Trigger ---")
    
    # 1. Login
    print(f"Logging in...")
    login_res = call_trpc("user.login", {"password": PASSWORD})
    if not login_res:
        print("Failed to login.")
        return
    
    token = login_res
    print("Login successful. Token acquired.")
    
    # 2. Trigger Install
    print(f"Triggering install for 'tunnelsats'...")
    install_res = call_trpc("apps.install", {"appId": "tunnelsats"}, token=token)
    
    if install_res is True:
        print("SUCCESS: Installation triggered successfully.")
        print("Monitoring 'journalctl -u umbrel -f' for Docker pulls is recommended.")
    else:
        print(f"FAILURE: Install trigger returned: {install_res}")

if __name__ == "__main__":
    main()
