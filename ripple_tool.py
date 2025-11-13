#!/usr/bin/env python3

import json
import os
import sys
import time
import argparse
from decimal import Decimal, getcontext
from typing import Any, Dict, Optional

# Set precision for Decimal calculations (XRP has 6 decimal places)
getcontext().prec = 18 

try:
    import requests
except ImportError:
    print("Error: The 'requests' library is required. Please install it using 'pip install requests'")
    sys.exit(1)


# --- Configuration ---

# The JSON-RPC endpoint for your local rippled server.
RIPPLED_URL = "{{RPC_ENDPOINT}}"

# File to store account credentials.
ACCOUNTS_FILE = ".accounts.env"

VAULTS_FILE = ".vaults.env"

# Genesis account details (for standalone mode ONLY).
GENESIS_ACCOUNT = {
    "name": "genesis",
    "address": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
    "secret": "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"
}

def drops_to_xrp(drops: str) -> Decimal:
    """Converts a value in drops to XRP."""
    return Decimal(drops) / Decimal('1000000')

def xrp_to_drops(xrp: str) -> str:
    """Converts a value in XRP to drops."""
    return str(int(Decimal(xrp) * Decimal('1000000')))

# --- Style and Color Definitions ---

class Style:
    """Console colors for better output."""
    RESET = '\033[0m'
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    BLUE = '\033[0;34m'
    YELLOW = '\033[0;33m'
    CYAN = "\033[36m"
    BOLD = '\033[1m'

    @staticmethod
    def print_success(message):
        print(f"{Style.GREEN}[SUCCESS]{Style.RESET} {message}")

    @staticmethod
    def print_info(message):
        print(f"{Style.CYAN}[INFO]{Style.RESET} {message}")

    @staticmethod
    def print_error(message):
        print(f"{Style.RED}[ERROR]{Style.RESET} {message}", file=sys.stderr)
        sys.exit(1)


class AccountManager:
    """Handles loading from and saving accounts to the accounts file."""
    def __init__(self, filepath):
        self.filepath = filepath
        self.accounts = {}
        self._initialize_file()
        self.load_accounts()

    def _initialize_file(self):
        """Ensures the accounts file exists and contains the genesis account."""
        if not os.path.exists(self.filepath):
            self.accounts = {"genesis": GENESIS_ACCOUNT}
            self.save_accounts()
            Style.print_info(f"Created accounts file at '{self.filepath}' with 'genesis' account.")
        else:
            self.load_accounts()
            if "genesis" not in self.accounts:
                self.add_account(
                    GENESIS_ACCOUNT["name"], 
                    GENESIS_ACCOUNT["address"], 
                    GENESIS_ACCOUNT["secret"]
                )
                Style.print_info("Added missing 'genesis' account to accounts file.")

    def load_accounts(self):
        """Loads all accounts from the file into memory."""
        try:
            with open(self.filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    name, address, secret = line.split(',')
                    self.accounts[name] = {"name": name, "address": address, "secret": secret}
        except FileNotFoundError:
            pass
        except Exception as e:
            Style.print_error(f"Failed to parse accounts file '{self.filepath}': {e}")
            
    def save_accounts(self):
        """Saves all accounts from memory to the file."""
        with open(self.filepath, 'w') as f:
            for name, data in self.accounts.items():
                f.write(f"{name},{data['address']},{data['secret']}\n")

    def get_account(self, name: str) -> dict:
        """Retrieves a single account by name."""
        if name not in self.accounts:
            Style.print_error(f"Account '{name}' not found in '{self.filepath}'. Use the 'list' command to see available accounts.")
        return self.accounts[name]

    def add_account(self, name: str, address: str, secret: str):
        """Adds a new account and saves to file."""
        if name in self.accounts:
            Style.print_error(f"Account with name '{name}' already exists.")
        self.accounts[name] = {"name": name, "address": address, "secret": secret}
        self.save_accounts()
        
    def list_accounts(self):
        """Prints a formatted list of all accounts."""
        Style.print_info(f"Displaying all accounts in '{self.filepath}':")
        print(f"{Style.BOLD}{'-'*65}{Style.RESET}")
        print(f"{Style.YELLOW}{'ACCOUNT NAME':<15} {'ADDRESS'}{Style.RESET}")
        print(f"{Style.BOLD}{'-'*65}{Style.RESET}")
        for name, data in self.accounts.items():
            print(f"{name:<15} {data['address']}")
        print(f"{Style.BOLD}{'-'*65}{Style.RESET}")

class VaultsManager:
    """Handles loading and saving of vault mappings to VAULTS_FILE.
    File format: each line is `from_name,vault_id,ISO_TIMESTAMP`
    Multiple lines for the same from_name are allowed (history preserved).
    In-memory structure: Dict[from_name, List[Tuple[vault_id, timestamp_iso]]]
    """
    def __init__(self, filepath: str):
        self.filepath = filepath
        # map from_name -> list of (vault_id, timestamp_iso)
        self.vaults: Dict[str, list] = {}
        self._ensure_file()
        self.load_vaults()

    def _ensure_file(self):
        """Ensure the vaults file exists."""
        if not os.path.exists(self.filepath):
            # create empty file
            open(self.filepath, 'a').close()

    def load_vaults(self):
        """Loads vault mappings from file into memory. Allows multiple entries per from_name."""
        self.vaults = {}
        try:
            with open(self.filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    # accept 2 or 3 columns; if timestamp missing, store empty string
                    parts = line.split(',')
                    if len(parts) < 2:
                        continue
                    from_name = parts[0]
                    vault_id = parts[1]
                    ts = parts[2] if len(parts) >= 3 else ""
                    if from_name not in self.vaults:
                        self.vaults[from_name] = []
                    self.vaults[from_name].append((vault_id, ts))
        except Exception as e:
            Style.print_error(f"Failed to parse vaults file '{self.filepath}': {e}")

    def save_vaults(self):
        """Write the entire in-memory vaults structure to disk.
        This method is kept for cases where you want a full rewrite; normal add_vault() will append.
        """
        try:
            with open(self.filepath, 'w') as f:
                for from_name, entries in self.vaults.items():
                    for vault_id, ts in entries:
                        if ts:
                            f.write(f"{from_name},{vault_id},{ts}\n")
                        else:
                            f.write(f"{from_name},{vault_id}\n")
        except Exception as e:
            Style.print_error(f"Failed to write vaults file '{self.filepath}': {e}")

    def add_vault(self, from_name: str, vault_id: str):
        """Append a new vault mapping to the file and update in-memory mapping.
        This does NOT overwrite existing entries for the same from_name.
        """
        if not from_name or not vault_id:
            Style.print_error("Both from_name and vault_id are required to save vault mapping.")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Append to in-memory structure
        if from_name not in self.vaults:
            self.vaults[from_name] = []
        self.vaults[from_name].append((vault_id, ts))

        # Append to file so we don't rewrite the whole file (preserve history)
        try:
            with open(self.filepath, 'a') as f:
                f.write(f"{from_name},{vault_id},{ts}\n")
        except Exception as e:
            Style.print_error(f"Failed to append to vaults file '{self.filepath}': {e}")

        Style.print_success(f"Saved vault mapping to '{self.filepath}': {from_name} -> {vault_id} (at {ts})")

    def get_vaults(self, from_name: str) -> Optional[list]:
        """Return list of (vault_id, timestamp) tuples for a given from_name, or None."""
        return self.vaults.get(from_name)

    def get_latest_vault(self, from_name: str) -> Optional[str]:
        """Return the most recently added vault_id for from_name, or None."""
        entries = self.vaults.get(from_name)
        if not entries:
            return None
        return entries[-1][0]  # last appended

    def list_vaults(self):
        """Print a formatted list of all vault entries (history preserved)."""
        Style.print_info(f"Displaying all vault mappings in '{self.filepath}':")
        print(f"{Style.BOLD}{'-'*80}{Style.RESET}")
        print(f"{Style.YELLOW}{'FROM':<20} {'VAULTID':<48} {'TIMESTAMP'}{Style.RESET}")
        print(f"{Style.BOLD}{'-'*80}{Style.RESET}")
        for from_name, entries in self.vaults.items():
            for vault_id, ts in entries:
                print(f"{from_name:<20} {vault_id:<48} {ts}")
        print(f"{Style.BOLD}{'-'*80}{Style.RESET}")




class RippleTool:
    """A tool for interacting with a rippled server in standalone mode."""
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()

    def _api_request(self, payload: dict) -> dict:
        """Sends a JSON-RPC request to the rippled server."""
        try:
            response = self.session.post(self.url, json=payload)
            response.raise_for_status()
            data = response.json()
            if 'error' in data.get('result', {}):
                raise Exception(f"Rippled error: {data['result']['error_message']}")
            return data['result']
        except requests.exceptions.RequestException as e:
            Style.print_error(f"Network error connecting to {self.url}. Is rippled running? Details: {e}")
        except json.JSONDecodeError:
            Style.print_error(f"Failed to parse JSON response from server. Response: {response.text}")
        except Exception as e:
            Style.print_error(f"An API error occurred: {e}")

    def _submit_blob(self, signed_blob: str) -> dict:
        """Submits a signed transaction blob to the ledger."""
        Style.print_info("Submitting the signed transaction...")
        submit_payload = {"method": "submit", "params": [{"tx_blob": signed_blob}]}
        submit_result = self._api_request(submit_payload)
        
        engine_result = submit_result['engine_result']
        if engine_result == "tesSUCCESS":
            Style.print_success(f"Transaction submitted successfully! Result: {engine_result}")
            Style.print_success(f"Transaction hash: {submit_result['tx_json']['hash']}")
        else:
            Style.print_error(f"Transaction submission failed. Result: {engine_result}\nDetails: {submit_result}")
        
        return submit_result

    def _sign_and_submit(self, tx_json: dict, secret: str) -> dict:
        """Signs (as self) and submits a transaction."""
        Style.print_info("Signing the transaction...")
        sign_payload = {"method": "sign", "params": [{"secret": secret, "tx_json": tx_json}]}
        sign_result = self._api_request(sign_payload)
        return self._submit_blob(sign_result['tx_blob'])

    def _sign_for_and_submit(self, tx_json: dict, signer_account: dict) -> dict:
        """Signs (as a delegate/multi-signer) and submits a transaction."""
        Style.print_info(f"Signing transaction for account '{tx_json['Account']}' as signer '{signer_account['address']}'...")
        sign_payload = {
            "method": "sign_for",
            "params": [
                {
                    "account": signer_account['address'],
                    "secret": signer_account['secret'],
                    "tx_json": tx_json
                }
            ]
        }
        sign_result = self._api_request(sign_payload)
        return self._submit_blob(sign_result['tx_blob'])

    def create_account(self) -> dict:
        """Calls the wallet_propose method to create a new account."""
        Style.print_info("Requesting a new account from the server...")
        payload = {"method": "wallet_propose", "params": [{}]}
        result = self._api_request(payload)
        return {"address": result.get("account_id"), "secret": result.get("master_seed")}

    def get_account_info(self, address: str) -> dict:
        """Gets ledger information for a given account."""
        return self._api_request({"method": "account_info", "params": [{"account": address}]})

    def get_account_lines(self, address: str) -> dict:
        """Gets all trust lines and IOU balances for an account."""
        Style.print_info(f"Fetching IOU balances for account {address}...")
        return self._api_request({"method": "account_lines", "params": [{"account": address}]})

    def close_ledger(self):
        """Closes the current ledger to process transactions (standalone mode)."""
        Style.print_info("Closing the ledger to process transactions...")
        self._api_request({"method": "ledger_accept"})
        time.sleep(2)
        Style.print_success("Ledger close command sent.")
        
    def send_payment(self, from_account: dict, to_address: str, amount_xrp: str):
        """Constructs and submits an XRP payment transaction."""
        Style.print_info(f"Preparing to send {amount_xrp} XRP from '{from_account['name']}' to '{to_address}'...")
        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "Payment",
            "Account": from_account['address'],
            "Destination": to_address,
            "Amount": xrp_to_drops(amount_xrp),
            "Fee": "12",
            "Sequence": sequence
        }
        return self._sign_and_submit(tx_json, from_account['secret'])

    def set_trust_line(self, from_account: dict, issuer_address: str, currency: str, limit: str):
        """Constructs and submits a TrustSet transaction."""
        Style.print_info(f"Setting trust line from '{from_account['name']}' to issuer '{issuer_address}' for {currency}...")
        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "TrustSet",
            "Account": from_account['address'],
            "Fee": "15",
            "Flags": "262144",
            "Sequence": sequence,
            "LimitAmount": {
                "currency": currency,
                "issuer": issuer_address,
                "value": limit
            }
        }
        return self._sign_and_submit(tx_json, from_account['secret'])

    def send_iou(self, from_account: dict, to_address: str, amount: str, currency: str):
        """Constructs and submits an IOU Payment transaction."""
        issuer_address = from_account['address']
        Style.print_info(f"Preparing to send {amount} {currency} from issuer '{from_account['name']}' to '{to_address}'...")
        try:
            account_info = self.get_account_info(issuer_address)
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for issuer '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "Payment",
            "Account": issuer_address,
            "Destination": to_address,
            "Amount": {
                "currency": currency,
                "issuer": issuer_address,
                "value": amount
            },
            "Fee": "15",
            "Sequence": sequence
        }
        return self._sign_and_submit(tx_json, from_account['secret'])

    def set_delegation(self, from_account: dict, delegate_address: str, permission_code: int):
        """Constructs and submits a DelegateSet transaction."""
        Style.print_info(f"Granting permission from '{from_account['name']}' to delegate '{delegate_address}'...")
        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "DelegateSet",
            "Account": from_account['address'],
            "Authorize": delegate_address,
            "Fee": "15",
            "Sequence": sequence,
            "Permissions": [
                {
                    "Permission": {
                        "PermissionValue": permission_code
                    }
                }
            ]
        }
        return self._sign_and_submit(tx_json, from_account['secret'])

    def send_delegated_payment(self, main_account: dict, delegate_account: dict, to_address: str, amount_xrp: str):
        """Constructs and submits a delegated Payment transaction."""
        Style.print_info(f"Delegate '{delegate_account['name']}' is sending {amount_xrp} XRP from main account '{main_account['name']}' to '{to_address}'...")
        
        try:
            account_info = self.get_account_info(main_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for main account '{main_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "Payment",
            "Account": main_account['address'],
            "Destination": to_address,
            "Amount": xrp_to_drops(amount_xrp),
            "Fee": "12",
            "Sequence": sequence
        }
        
        return self._sign_for_and_submit(tx_json, delegate_account)

    def create_vault(self, from_account: dict, asset_json: dict, **kwargs):
        """Constructs and submits a VaultCreate transaction."""
        Style.print_info(f"Creating a vault for account '{from_account['name']}'...")
        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "VaultCreate",
            "Account": from_account['address'],
            "Asset": asset_json,
            "Fee": "2000000",
            "Sequence": sequence
        }

        # Add optional fields from kwargs if they exist
        if kwargs.get("flags") is not None:
            tx_json["Flags"] = kwargs["flags"]
        if kwargs.get("assets_maximum") is not None:
            tx_json["AssetsMaximum"] = kwargs["assets_maximum"]
        if kwargs.get("withdrawal_policy") is not None:
            tx_json["WithdrawalPolicy"] = kwargs["withdrawal_policy"]
        if kwargs.get("scale") is not None:
            tx_json["Scale"] = kwargs["scale"]
        if kwargs.get("mpt_metadata") is not None:
            tx_json["MPTokenMetadata"] = kwargs["mpt_metadata"]
        if kwargs.get("data") is not None:
            tx_json["Data"] = kwargs["data"]
        if kwargs.get("domain_id") is not None:
            tx_json["DomainID"] = kwargs["domain_id"]

        return self._sign_and_submit(tx_json, from_account['secret'])

    def get_transaction(self, tx_hash: str) -> dict:
        """Gets a transaction's details from the ledger by its hash."""
        Style.print_info(f"Fetching transaction with hash: {tx_hash}...")
        payload = {
            "method": "tx",
            "params": [
                {
                    "transaction": tx_hash,
                    "binary": False
                }
            ]
        }
        # The _api_request method already handles errors and returns the 'result' object
        return self._api_request(payload)

    def set_vault(self, from_account: dict, vault_id: str, **kwargs):
        """Constructs and submits a VaultSet transaction."""
        Style.print_info(f"Updating vault '{vault_id}' for account '{from_account['name']}'...")
        
        # Validate that at least one optional field to update is provided
        updatable_fields = ["assets_maximum", "domain_id", "data"]
        if not any(kwargs.get(field) is not None for field in updatable_fields):
            Style.print_error("At least one field to update (--max-assets, --domain-id, or --data) is required for a VaultSet transaction.")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "VaultSet",
            "Account": from_account['address'],
            "VaultID": vault_id,
            "Fee": "15",
            "Sequence": sequence
        }

        # Add optional fields from kwargs if they exist
        if kwargs.get("assets_maximum") is not None:
            tx_json["AssetsMaximum"] = kwargs["assets_maximum"]
        if kwargs.get("domain_id") is not None:
            tx_json["DomainID"] = kwargs["domain_id"]
        if kwargs.get("data") is not None:
            tx_json["Data"] = kwargs["data"]

        return self._sign_and_submit(tx_json, from_account['secret'])

    def get_vault_info(self, vault_id: str) -> dict:
        """Gets an existing vault's details from the ledger by its ID."""
        Style.print_info(f"Fetching info for vault with ID: {vault_id}...")
        payload = {
            "method": "vault_info",
            "params": [
                {
                    "vault_id": vault_id
                }
            ]
        }
        return self._api_request(payload)

    def deposit_to_vault(self, from_account: dict, vault_id: str, amount_json: [dict, str]):
        """Constructs and submits a VaultDeposit transaction."""
        Style.print_info(f"Depositing into vault '{vault_id}' from account '{from_account['name']}'...")
        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "VaultDeposit",
            "Account": from_account['address'],
            "VaultID": vault_id,
            "Amount": amount_json,
            "Fee": "15",
            "Sequence": sequence
        }

        return self._sign_and_submit(tx_json, from_account['secret'])

    def withdraw_from_vault(self, from_account: dict, vault_id: str, amount_json: [dict, str], destination_address: str = None):
        """Constructs and submits a VaultWithdraw transaction."""
        Style.print_info(f"Withdrawing from vault '{vault_id}' by account '{from_account['name']}'...")
        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "VaultWithdraw",
            "Account": from_account['address'],
            "VaultID": vault_id,
            "Amount": amount_json,
            "Fee": "15",
            "Sequence": sequence
        }

        if destination_address:
            tx_json["Destination"] = destination_address
            Style.print_info(f"Assets will be sent to destination: {destination_address}")

        return self._sign_and_submit(tx_json, from_account['secret'])

    def account_set(self, from_account: dict, set_flag: int = None, clear_flag: int = None):
        """Constructs and submits an AccountSet transaction."""
        Style.print_info(f"Modifying settings for account '{from_account['name']}'...")

        if set_flag is None and clear_flag is None:
            Style.print_error("Either --set-flag or --clear-flag must be provided for an AccountSet transaction.")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "AccountSet",
            "Account": from_account['address'],
            "Fee": "12",
            "Sequence": sequence
        }

        if set_flag is not None:
            tx_json["SetFlag"] = set_flag
            Style.print_info(f"Setting flag: {set_flag}")
        if clear_flag is not None:
            tx_json["ClearFlag"] = clear_flag
            Style.print_info(f"Clearing flag: {clear_flag}")

        return self._sign_and_submit(tx_json, from_account['secret'])


def run_balance_check(accounts: AccountManager, tool: RippleTool, account_name: str):
    """Helper function to run the XRP balance check for a single account."""
    account = accounts.get_account(account_name)
    try:
        info = tool.get_account_info(account['address'])
        balance_drops = info['account_data']['Balance']
        balance_xrp = drops_to_xrp(balance_drops)
        print(f"  {Style.YELLOW}{'Account':<10}:{Style.RESET} {Style.BOLD}{account_name:<15}{Style.RESET} | {Style.YELLOW}{'XRP Balance':<12}:{Style.RESET} {balance_xrp} XRP")
    except Exception:
        Style.print_info(f"Account '{account_name}' ({account['address']}) is not funded on the ledger yet.")

def run_iou_balance_check(accounts: AccountManager, tool: RippleTool, account_name: str):
    """Helper function to check all IOU balances for a single account."""
    account = accounts.get_account(account_name)
    try:
        lines_result = tool.get_account_lines(account['address'])
        lines = lines_result.get('lines', [])
        print(f"  {Style.YELLOW}IOU Balances for account:{Style.RESET} {Style.BOLD}{account_name}{Style.RESET}")
        if not lines:
            print("    No IOU balances found.")
            return

        for line in lines:
            print(f"    - {Style.BOLD}{line['balance']} {line['currency']}{Style.RESET} (Issuer: {line['account']})")
    except Exception as e:
        Style.print_error(f"Could not retrieve IOU balances for '{account_name}': {e}")

def extract_vault_id(data: Dict[str, Any]) -> Optional[str]:
    
    meta = data.get("meta")
    if not meta:
        return None

    affected_nodes = meta.get("AffectedNodes", [])
    for node in affected_nodes:
        created = node.get("CreatedNode")
        if created and created.get("LedgerEntryType") == "AccountRoot":
            new_fields = created.get("NewFields", {})
            vault_id = new_fields.get("VaultID")
            if vault_id:
                return vault_id
    return None




def main():
    """Main function to parse arguments and execute commands."""
    parser = argparse.ArgumentParser(
        description="A command-line tool to interact with a rippled server in standalone mode.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # --- Account Management ---
    parser_create = subparsers.add_parser("create", help="Create a new Ripple account and save it.")
    parser_create.add_argument("name", help="A local name to assign to the new account (e.g., 'alice').")
    subparsers.add_parser("list", help="List all saved accounts.")

    # --- XRP Commands ---
    parser_balance = subparsers.add_parser("balance", help="Check the native XRP balance of an account.")
    parser_balance.add_argument("name", help="The local name of the account to check.")
    parser_fund = subparsers.add_parser("fund", help="Fund an account with XRP from the genesis account.")
    parser_fund.add_argument("name", help="The local name of the account to fund.")
    parser_fund.add_argument("amount", type=str, help="The amount of XRP to send.")
    parser_send = subparsers.add_parser("send", help="Send XRP from one account to another.")
    parser_send.add_argument("--from", dest="from_name", required=True, help="The name of the source account.")
    parser_send.add_argument("--to", dest="to_name", required=True, help="The name of the destination account.")
    parser_send.add_argument("--amount", type=str, required=True, help="The amount of XRP to send.")

    # --- IOU Commands ---
    parser_trust = subparsers.add_parser("trustset", help="Create a trust line to an IOU issuer.")
    parser_trust.add_argument("--from", dest="from_name", required=True, help="The account creating the trust line (receiver).")
    parser_trust.add_argument("--issuer", dest="issuer_name", required=True, help="The account that will issue the IOU.")
    parser_trust.add_argument("--currency", required=True, help="The 3-character currency code (e.g., USD).")
    parser_trust.add_argument("--limit", required=True, help="The maximum amount of the IOU to trust.")
    parser_iou_send = subparsers.add_parser("iou-send", help="Send/issue an IOU to a trusted account.")
    parser_iou_send.add_argument("--from", dest="from_name", required=True, help="The issuer's account name.")
    parser_iou_send.add_argument("--to", dest="to_name", required=True, help="The receiver's account name.")
    parser_iou_send.add_argument("--amount", required=True, help="The amount of the IOU to send.")
    parser_iou_send.add_argument("--currency", required=True, help="The 3-character currency code.")
    parser_iou_balance = subparsers.add_parser("iou-balance", help="Check the IOU (issued currency) balances of an account.")
    parser_iou_balance.add_argument("name", help="The local name of the account to check.")

    # --- Delegation Commands ---
    parser_delegate_set = subparsers.add_parser("delegate-set", help="Grant another account permission to send transactions on your behalf.")
    parser_delegate_set.add_argument("--from", dest="from_name", required=True, help="The main account GRANTING permission.")
    parser_delegate_set.add_argument("--to", dest="to_name", required=True, help="The delegate account being AUTHORIZED.")
    parser_delegate_set.add_argument("--permission-code", type=int, required=True, help="The integer code for the transaction type to delegate (e.g., 6 for Payment).")
    parser_delegated_send = subparsers.add_parser("delegated-send", help="Execute a delegated XRP payment.")
    parser_delegated_send.add_argument("--main-account", required=True, help="The account whose funds are being sent (and pays the fee).")
    parser_delegated_send.add_argument("--delegate-account", required=True, help="The account SIGNING and authorizing the transaction.")
    parser_delegated_send.add_argument("--to", dest="to_name", required=True, help="The final destination account name.")
    parser_delegated_send.add_argument("--amount", type=str, required=True, help="The amount of XRP to send.")

    # --- Vault Commands ---
    parser_vault_create = subparsers.add_parser("vault-create", help="Create a new vault for holding assets.")
    parser_vault_create.add_argument("--from", dest="from_name", required=True, help="The account creating the vault.")
    parser_vault_create.add_argument("--currency", required=True, help="The currency of the asset the vault will hold (e.g., 'XRP', 'USD').")
    parser_vault_create.add_argument("--issuer", dest="issuer_name", help="The issuer of the asset (required for non-XRP currencies).")
    parser_vault_create.add_argument("--flags", type=int, help="Optional flags (e.g., 1 for tfVaultPrivate).")
    parser_vault_create.add_argument("--max-assets", help="Optional maximum amount of assets the vault can hold.")
    parser_vault_create.add_argument("--policy", type=int, help="Optional withdrawal policy code (e.g., 1 for First-Come-First-Serve).")
    parser_vault_create.add_argument("--scale", type=int, help="Optional scaling factor for the vault's shares (IOUs only).")
    parser_vault_create.add_argument("--metadata", help="Optional hex-encoded MPT metadata.")
    parser_vault_create.add_argument("--data", help="Optional hex-encoded arbitrary data.")
    parser_vault_create.add_argument("--domain-id", help="Optional hex-encoded Domain ID (requires tfVaultPrivate flag).")

    parser_vault_set = subparsers.add_parser("vault-set", help="Modify the properties of an existing vault.")
    parser_vault_set.add_argument("--from", dest="from_name", required=True, help="The account that owns the vault.")
    parser_vault_set.add_argument("--vault-id", required=True, help="The 256-bit ID (hash) of the vault to modify.")
    parser_vault_set.add_argument("--max-assets", help="Optional: Update the maximum assets the vault can hold.")
    parser_vault_set.add_argument("--domain-id", help="Optional: Update the Domain ID for a private vault.")
    parser_vault_set.add_argument("--data", help="Optional: Update the hex-encoded arbitrary data.")

    parser_vault_info = subparsers.add_parser("vault-info", help="Get the ledger details of an existing vault by its ID.")
    parser_vault_info.add_argument("vault_id", help="The 256-bit ID (hash) of the vault to look up.")

    parser_vault_deposit = subparsers.add_parser("vault-deposit", help="Deposit assets into an existing vault.")
    parser_vault_deposit.add_argument("--from", dest="from_name", required=True, help="The account making the deposit.")
    parser_vault_deposit.add_argument("--vault-id", required=True, help="The 256-bit ID (hash) of the vault to deposit into.")
    parser_vault_deposit.add_argument("--amount", required=True, help="The amount of the currency to deposit.")
    parser_vault_deposit.add_argument("--currency", required=True, help="The currency to deposit (e.g., 'XRP', 'USD').")
    parser_vault_deposit.add_argument("--issuer", dest="issuer_name", help="The issuer of the asset (required for non-XRP currencies).")

    parser_vault_withdraw = subparsers.add_parser("vault-withdraw", help="Withdraw assets from an existing vault.")
    parser_vault_withdraw.add_argument("--from", dest="from_name", required=True, help="The account initiating the withdrawal (must hold vault shares).")
    parser_vault_withdraw.add_argument("--vault-id", required=True, help="The 256-bit ID (hash) of the vault to withdraw from.")
    parser_vault_withdraw.add_argument("--amount", required=True, help="The amount to withdraw (asset) or redeem (shares).")
    parser_vault_withdraw.add_argument("--currency", required=True, help="The currency of the asset or shares (e.g., 'XRP', 'USD', or MPT code).")
    parser_vault_withdraw.add_argument("--issuer", dest="issuer_name", help="The issuer of the asset (for non-XRP IOUs). Not needed when redeeming shares.")
    parser_vault_withdraw.add_argument("--destination", dest="destination_name", help="Optional: A different account name to receive the withdrawn assets.")
    parser_vault_withdraw.add_argument("--redeem-shares", action="store_true", help="Specify this flag to redeem vault shares instead of withdrawing an asset amount.")

    # --- Utility Commands ---
    parser_tx = subparsers.add_parser("tx", help="Get the result of a transaction by its hash.")
    parser_tx.add_argument("hash", help="The transaction hash to look up.")

    # --- Account Settings ---
    parser_account_set = subparsers.add_parser("account-set", help="Modify the properties of an account (e.g., set flags).")
    parser_account_set.add_argument("--from", dest="from_name", required=True, help="The account to modify.")
    parser_account_set.add_argument("--set-flag", type=int, help="The integer code for a flag to enable (e.g., 8 for asfDefaultRipple).")
    parser_account_set.add_argument("--clear-flag", type=int, help="The integer code for a flag to disable.")

    args = parser.parse_args()
    
    accounts = AccountManager(ACCOUNTS_FILE)
    vaults = VaultsManager(VAULTS_FILE)
    tool = RippleTool(RIPPLED_URL)

    if args.command == "list":
        accounts.list_accounts()
    elif args.command == "create":
        new_account = tool.create_account()
        if not new_account.get("address"):
            Style.print_error("Failed to create account. The server did not return a valid address.")
        accounts.add_account(args.name, new_account["address"], new_account["secret"])
        Style.print_success(f"Account '{Style.BOLD}{args.name}{Style.RESET}' created and saved.")
        print(f"  {Style.YELLOW}{'Address':<10}:{Style.RESET} {new_account['address']}")
        print(f"  {Style.YELLOW}{'Secret':<10}:{Style.RESET} {new_account['secret']}")
        print("\nFund this account with XRP using the 'fund' command.")
    elif args.command == "balance":
        run_balance_check(accounts, tool, args.name)
    elif args.command == "fund":
        from_account = accounts.get_account("genesis")
        to_account = accounts.get_account(args.name)
        tool.send_payment(from_account, to_account['address'], args.amount)
        tool.close_ledger()
        print("\nVerifying balances...")
        run_balance_check(accounts, tool, from_account['name'])
        run_balance_check(accounts, tool, to_account['name'])
    elif args.command == "send":
        from_account = accounts.get_account(args.from_name)
        to_account = accounts.get_account(args.to_name)
        tool.send_payment(from_account, to_account['address'], args.amount)
        tool.close_ledger()
        print("\nVerifying balances...")
        run_balance_check(accounts, tool, from_account['name'])
        run_balance_check(accounts, tool, to_account['name'])
    elif args.command == "trustset":
        from_account = accounts.get_account(args.from_name)
        issuer_account = accounts.get_account(args.issuer_name)
        tool.set_trust_line(from_account, issuer_account['address'], args.currency, args.limit)
        tool.close_ledger()
        Style.print_success(f"Trust line from '{from_account['name']}' to '{issuer_account['name']}' should now be active.")
    elif args.command == "iou-send":
        from_account = accounts.get_account(args.from_name) # Issuer
        to_account = accounts.get_account(args.to_name)     # Receiver
        tool.send_iou(from_account, to_account['address'], args.amount, args.currency)
        tool.close_ledger()
        print("\nVerifying IOU balance...")
        run_iou_balance_check(accounts, tool, to_account['name'])
    elif args.command == "iou-balance":
        run_iou_balance_check(accounts, tool, args.name)
    elif args.command == "delegate-set":
        from_account = accounts.get_account(args.from_name)
        to_account = accounts.get_account(args.to_name)
        tool.set_delegation(from_account, to_account['address'], args.permission_code)
        tool.close_ledger()
        Style.print_success(f"Delegation permission should now be active from '{from_account['name']}' to '{to_account['name']}'.")
    elif args.command == "delegated-send":
        main_account = accounts.get_account(args.main_account)
        delegate_account = accounts.get_account(args.delegate_account)
        to_account = accounts.get_account(args.to_name)
        tool.send_delegated_payment(main_account, delegate_account, to_account['address'], args.amount)
        tool.close_ledger()
        print("\nVerifying balances...")
        run_balance_check(accounts, tool, main_account['name'])
        run_balance_check(accounts, tool, to_account['name'])
        print("---")
        run_balance_check(accounts, tool, delegate_account['name'])
    elif args.command == "vault-create":
        from_account = accounts.get_account(args.from_name)
        asset_json = {}
        if args.currency.upper() == 'XRP':
            asset_json = {"currency": "XRP"}
        else:
            if not args.issuer_name:
                Style.print_error("The --issuer flag is required for non-XRP currencies.")
            issuer_account = accounts.get_account(args.issuer_name)
            asset_json = {"currency": args.currency, "issuer": issuer_account['address']}
        
        optional_args = {
            "flags": args.flags,
            "assets_maximum": args.max_assets,
            "withdrawal_policy": args.policy,
            "scale": args.scale,
            "mpt_metadata": args.metadata,
            "data": args.data,
            "domain_id": args.domain_id
        }
        
        submit_result = tool.create_vault(from_account, asset_json, **optional_args)
        tool.close_ledger()
        Style.print_success(f"Vault creation transaction submitted for account '{from_account['name']}'.")
        
        try:
            if not submit_result or 'tx_json' not in submit_result or 'hash' not in submit_result['tx_json']:
                Style.print_error("Submission did not return transaction hash. Can't fetch transaction.")
            txn_hash_local = submit_result['tx_json']['hash']

            # fetch transaction by explicit local variable
            txn_result = tool.get_transaction(txn_hash_local)
            vault_id = extract_vault_id(txn_result)
            print(f"vault Id : {vault_id}")

            if vault_id:
                vaults.add_vault(from_account['name'], vault_id)
            else:
                Style.print_info("VaultID not found in transaction result. It may not have been created or transaction format differs.")
        except Exception as e:
            Style.print_error(f"Failed to fetch transaction or save vault: {e}")

    elif args.command == "tx":
        try:
            tx_result = tool.get_transaction(args.hash)
            # Pretty-print the full JSON response
            print(json.dumps(tx_result, indent=4))
            
            # Also print a quick, color-coded summary of the result
            engine_result = tx_result.get('meta', {}).get('TransactionResult')
            validated = tx_result.get('validated', False)
            
            print("\n--- Summary ---")
            if validated:
                print(f"Validated: {Style.GREEN}{validated}{Style.RESET}")
            else:
                print(f"Validated: {Style.RED}{validated}{Style.RESET}")

            if engine_result:
                if engine_result == "tesSUCCESS":
                    Style.print_success(f"Transaction Result: {engine_result}")
                else:
                    Style.print_error(f"Transaction Result: {engine_result}")
        except Exception as e:
            # The _api_request already handles most API errors, but this is a fallback.
            Style.print_error(f"Could not retrieve transaction: {e}")
    elif args.command == "vault-set":
        from_account = accounts.get_account(args.from_name)
        
        # Pack optional args into a dictionary to pass to the tool
        optional_args = {
            "assets_maximum": args.max_assets,
            "domain_id": args.domain_id,
            "data": args.data
        }
        
        tool.set_vault(from_account, args.vault_id, **optional_args)
        tool.close_ledger()
        Style.print_success(f"VaultSet transaction submitted for vault '{args.vault_id}'.")

    elif args.command == "vault-info":
        try:
            vault_info_result = tool.get_vault_info(args.vault_id)
            # Pretty-print the full JSON response
            print(json.dumps(vault_info_result, indent=4))
        except Exception as e:
            Style.print_error(f"Could not retrieve vault info: {e}")

    elif args.command == "vault-deposit":
        from_account = accounts.get_account(args.from_name)
        amount_to_deposit = None

        if args.currency.upper() == 'XRP':
            # For XRP, the amount is a string of drops
            amount_to_deposit = xrp_to_drops(args.amount)
        else:
            # For IOUs, the amount is a JSON object
            if not args.issuer_name:
                Style.print_error("The --issuer flag is required for non-XRP currencies.")
            issuer_account = accounts.get_account(args.issuer_name)
            amount_to_deposit = {
                "currency": args.currency,
                "issuer": issuer_account['address'],
                "value": args.amount
            }
        
        tool.deposit_to_vault(from_account, args.vault_id, amount_to_deposit)
        tool.close_ledger()
        Style.print_success(f"VaultDeposit transaction submitted for vault '{args.vault_id}'.")
        print("Use the 'vault-info' command to verify the new balance.")

    elif args.command == "vault-withdraw":
        from_account = accounts.get_account(args.from_name)
        destination_address = None
        if args.destination_name:
            destination_account = accounts.get_account(args.destination_name)
            destination_address = destination_account['address']

        amount_to_withdraw = None

        if args.redeem_shares:
            # Mode 2: Redeeming a specific amount of vault shares
            if not args.issuer_name:
                    Style.print_error("The --issuer flag is required when withdrawing a non-XRP asset.")
            issuer_account = accounts.get_account(args.issuer_name)
            amount_to_withdraw = {
                "currency": args.currency, # This should be the MPT currency code
                "issuer": issuer_account['address'],
                "value": args.amount
            }
        else:
            # Mode 1: Withdrawing a specific amount of the underlying asset
            if args.currency.upper() == 'XRP':
                amount_to_withdraw = xrp_to_drops(args.amount)
            else:
                if not args.issuer_name:
                    Style.print_error("The --issuer flag is required when withdrawing a non-XRP asset.")
                issuer_account = accounts.get_account(args.issuer_name)
                amount_to_withdraw = {
                    "currency": args.currency,
                    "issuer": issuer_account['address'],
                    "value": args.amount
                }

        tool.withdraw_from_vault(from_account, args.vault_id, amount_to_withdraw, destination_address)
        tool.close_ledger()
        Style.print_success(f"VaultWithdraw transaction submitted for vault '{args.vault_id}'.")
        print("Use 'balance' and 'vault-info' commands to verify changes.")

    elif args.command == "account-set":
        from_account = accounts.get_account(args.from_name)
        tool.account_set(from_account, set_flag=args.set_flag, clear_flag=args.clear_flag)
        tool.close_ledger()
        Style.print_success(f"AccountSet transaction submitted for '{from_account['name']}'.")


if __name__ == "__main__":
    main()
