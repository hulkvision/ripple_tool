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
RIPPLED_URL = "http://127.0.0.1:5005"

# File to store account credentials.
ACCOUNTS_FILE = ".accounts.env"

VAULTS_FILE = ".vaults.env"

LOANBROKER_FILE = ".loanbroker.env"

LOANS_FILE = ".loans.env"

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
        """Prints an error and raises a RuntimeError to allow for cleanup."""
        full_message = f"{Style.RED}[ERROR]{Style.RESET} {message}"
        print(full_message, file=sys.stderr)
        raise RuntimeError(message)


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


class LoanBrokerManager:
    """Handles loading and saving of loan broker mappings to LOANBROKER_FILE."""
    def __init__(self, filepath: str):
        self.filepath = filepath
        # map from_name -> list of (loan_broker_id, timestamp_iso)
        self.brokers: Dict[str, list] = {}
        self._ensure_file()
        self.load_brokers()

    def _ensure_file(self):
        """Ensure the loan brokers file exists."""
        if not os.path.exists(self.filepath):
            open(self.filepath, 'a').close()

    def load_brokers(self):
        """Loads loan broker mappings from file into memory."""
        self.brokers = {}
        try:
            with open(self.filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(',')
                    if len(parts) < 2:
                        continue
                    from_name = parts[0]
                    broker_id = parts[1]
                    ts = parts[2] if len(parts) >= 3 else ""
                    if from_name not in self.brokers:
                        self.brokers[from_name] = []
                    self.brokers[from_name].append((broker_id, ts))
        except Exception as e:
            Style.print_error(f"Failed to parse loan brokers file '{self.filepath}': {e}")

    def add_broker(self, from_name: str, broker_id: str):
        """Append a new loan broker mapping to the file and update in-memory mapping."""
        if not from_name or not broker_id:
            Style.print_error("Both from_name and broker_id are required to save the mapping.")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if from_name not in self.brokers:
            self.brokers[from_name] = []
        self.brokers[from_name].append((broker_id, ts))

        try:
            with open(self.filepath, 'a') as f:
                f.write(f"{from_name},{broker_id},{ts}\n")
        except Exception as e:
            Style.print_error(f"Failed to append to loan brokers file '{self.filepath}': {e}")

        Style.print_success(f"Saved loan broker mapping to '{self.filepath}': {from_name} -> {broker_id} (at {ts})")

    def get_latest_broker(self, from_name: str) -> Optional[str]:
        """Return the most recently added broker_id for from_name, or None."""
        entries = self.brokers.get(from_name)
        if not entries:
            return None
        return entries[-1][0]

    def list_brokers(self):
        """Print a formatted list of all loan broker entries."""
        Style.print_info(f"Displaying all loan broker mappings in '{self.filepath}':")
        print(f"{Style.BOLD}{'-'*80}{Style.RESET}")
        print(f"{Style.YELLOW}{'FROM':<20} {'LOANBROKER_ID':<48} {'TIMESTAMP'}{Style.RESET}")
        print(f"{Style.BOLD}{'-'*80}{Style.RESET}")
        for from_name, entries in self.brokers.items():
            for broker_id, ts in entries:
                print(f"{from_name:<20} {broker_id:<48} {ts}")
        print(f"{Style.BOLD}{'-'*80}{Style.RESET}")

class LoanManager:
    """Handles loading and saving of loan mappings to a file."""
    def __init__(self, filepath: str):
        self.filepath = filepath
        # map borrower_name -> list of (loan_id, timestamp_iso)
        self.loans: Dict[str, list] = {}
        self._ensure_file()
        self.load_loans()

    def _ensure_file(self):
        """Ensure the loans file exists."""
        if not os.path.exists(self.filepath):
            open(self.filepath, 'a').close()

    def load_loans(self):
        """Loads loan mappings from file into memory."""
        self.loans = {}
        try:
            with open(self.filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(',')
                    if len(parts) < 2:
                        continue
                    borrower_name = parts[0]
                    loan_id = parts[1]
                    ts = parts[2] if len(parts) >= 3 else ""
                    if borrower_name not in self.loans:
                        self.loans[borrower_name] = []
                    self.loans[borrower_name].append((loan_id, ts))
        except Exception as e:
            Style.print_error(f"Failed to parse loans file '{self.filepath}': {e}")

    def add_loan(self, borrower_name: str, loan_id: str):
        """Append a new loan mapping to the file and update in-memory mapping."""
        if not borrower_name or not loan_id:
            Style.print_error("Both borrower_name and loan_id are required to save the mapping.")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if borrower_name not in self.loans:
            self.loans[borrower_name] = []
        self.loans[borrower_name].append((loan_id, ts))

        try:
            with open(self.filepath, 'a') as f:
                f.write(f"{borrower_name},{loan_id},{ts}\n")
        except Exception as e:
            Style.print_error(f"Failed to append to loans file '{self.filepath}': {e}")

        Style.print_success(f"Saved loan mapping to '{self.filepath}': {borrower_name} -> {loan_id} (at {ts})")

    def list_loans(self):
        """Print a formatted list of all loan entries."""
        Style.print_info(f"Displaying all loan mappings in '{self.filepath}':")
        print(f"{Style.BOLD}{'-'*90}{Style.RESET}")
        print(f"{Style.YELLOW}{'BORROWER':<20} {'LOAN_ID':<64} {'TIMESTAMP'}{Style.RESET}")
        print(f"{Style.BOLD}{'-'*90}{Style.RESET}")
        for borrower_name, entries in self.loans.items():
            for loan_id, ts in entries:
                print(f"{borrower_name:<20} {loan_id:<64} {ts}")
        print(f"{Style.BOLD}{'-'*90}{Style.RESET}")


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

    def get_ledger_entry(self, index: str) -> dict:
        """Gets a ledger object by its index (hash)."""
        Style.print_info(f"Fetching ledger entry with index: {index}...")
        payload = {
            "method": "ledger_entry",
            "params": [
                {
                    "index": index,
                    "ledger_index": "validated"
                }
            ]
        }
        return self._api_request(payload)

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

    def set_loan_broker(self, from_account: dict, vault_id: str, **kwargs):
        """Constructs and submits a LoanBrokerSet transaction for creation or modification."""
        
        loan_broker_id = kwargs.get("loan_broker_id")
        mode = "Modifying" if loan_broker_id else "Creating"
        Style.print_info(f"{mode} a loan broker for vault '{vault_id}'...")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanBrokerSet",
            "Account": from_account['address'],
            "VaultID": vault_id,
            "Fee": "15",
            "Sequence": sequence
        }

        if loan_broker_id:
            # Modification Mode
            tx_json["LoanBrokerID"] = loan_broker_id
        else:
            # Creation Mode - Required Fields
            tx_json["ManagementFeeRate"] = kwargs["management_fee_rate"]
            tx_json["CoverRateMinimum"] = kwargs["cover_rate_minimum"]
            tx_json["CoverRateLiquidation"] = kwargs["cover_rate_liquidation"]

        # Optional / Modifiable Fields
        if kwargs.get("debt_maximum") is not None:
            tx_json["DebtMaximum"] = kwargs["debt_maximum"]
        if kwargs.get("data") is not None:
            tx_json["Data"] = kwargs["data"]

        return self._sign_and_submit(tx_json, from_account['secret'])

    def delete_loan_broker(self, from_account: dict, loan_broker_id: str):
        """Constructs and submits a LoanBrokerDelete transaction."""
        Style.print_info(f"Deleting loan broker '{loan_broker_id}' owned by '{from_account['name']}'...")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanBrokerDelete",
            "Account": from_account['address'],
            "LoanBrokerID": loan_broker_id,
            "Fee": "12",
            "Sequence": sequence
        }

        return self._sign_and_submit(tx_json, from_account['secret'])

    def deposit_loan_broker_cover(self, from_account: dict, loan_broker_id: str, amount_json: [dict, str]):
        """Constructs and submits a LoanBrokerCoverDeposit transaction."""
        Style.print_info(f"Depositing cover into loan broker '{loan_broker_id}' from account '{from_account['name']}'...")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanBrokerCoverDeposit",
            "Account": from_account['address'],
            "LoanBrokerID": loan_broker_id,
            "Amount": amount_json,
            "Fee": "12",
            "Sequence": sequence
        }

        return self._sign_and_submit(tx_json, from_account['secret'])

    def withdraw_loan_broker_cover(self, from_account: dict, loan_broker_id: str, amount_json: [dict, str], destination_address: str = None):
        """Constructs and submits a LoanBrokerCoverWithdraw transaction."""
        Style.print_info(f"Withdrawing cover from loan broker '{loan_broker_id}' by account '{from_account['name']}'...")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanBrokerCoverWithdraw",
            "Account": from_account['address'],
            "LoanBrokerID": loan_broker_id,
            "Amount": amount_json,
            "Fee": "12",
            "Sequence": sequence
        }

        if destination_address:
            tx_json["Destination"] = destination_address
            Style.print_info(f"Assets will be sent to destination: {destination_address}")

        return self._sign_and_submit(tx_json, from_account['secret'])

    def clawback_loan_broker_cover(self, from_account: dict, loan_broker_id: str, amount_json: dict):
        """Constructs and submits a LoanBrokerCoverClawback transaction."""
        Style.print_info(f"Clawing back cover from loan broker '{loan_broker_id}' by issuer '{from_account['name']}'...")

        # A clawback can only be performed by the issuer of the asset.
        if from_account['address'] != amount_json['issuer']:
            Style.print_error("Clawback error: The 'from' account must be the issuer of the asset being clawed back.")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanBrokerCoverClawback",
            "Account": from_account['address'],
            "LoanBrokerID": loan_broker_id,
            "Amount": amount_json,
            "Fee": "12",
            "Sequence": sequence
        }

        return self._sign_and_submit(tx_json, from_account['secret'])

    def set_loan_x(self, borrower_account: dict, broker_owner_account: dict, loan_broker_id: str, principal_amount: dict, **kwargs):
        Style.print_info(f"Initiating a new loan between borrower '{borrower_account['name']}' and broker owner '{broker_owner_account['name']}'...")

        try:
            account_info = self.get_account_info(borrower_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for borrower '{borrower_account['name']}'. Is the account funded?")

        base_tx_json = {
            "TransactionType": "LoanSet",
            "Account": borrower_account['address'],
            "Counterparty": broker_owner_account['address'],
            "Flags":1073741824,
            "LoanBrokerID": loan_broker_id,
            "PrincipalRequested": principal_amount,
            "Fee": "0",  # Higher fee for multi-signature
            "Sequence": sequence,
            "SigningPubKey": ""
        }

        if kwargs.get("interest_rate") is not None:
            base_tx_json["InterestRate"] = kwargs["interest_rate"]
        if kwargs.get("payment_interval") is not None:
            base_tx_json["PaymentInterval"] = kwargs["payment_interval"]
        if kwargs.get("payment_total") is not None:
            base_tx_json["PaymentTotal"] = kwargs["payment_total"]
        if kwargs.get("origination_fee") is not None:
            base_tx_json["LoanOriginationFee"] = kwargs["origination_fee"]

        base_tx_json_2 = base_tx_json.copy()
        base_tx_json_2["Sequence"] = sequence+1

        account_info = self.get_account_info(borrower_account['address'])
        sequence = account_info['account_data']['Sequence']
        outer_txn = {
            "TransactionType": "Batch",
            "Account": borrower_account['address'],
            "Flags": 65536,
            "RawTransactions": [{"RawTransaction":base_tx_json},{"RawTransaction":base_tx_json_2}],
            "Sequence":sequence,
            "Fee":40
        }

        final_sign_payload = {
            "method": "sign",
            "params": [{
                "secret": borrower_account['secret'],
                "tx_json": outer_txn
            }]
        }
        final_sign_result = self._api_request(final_sign_payload)
        return self._submit_blob(final_sign_result['tx_blob'])
        


    def set_loan(self, borrower_account: dict, broker_owner_account: dict, loan_broker_id: str, principal_amount: dict, **kwargs):
        """Constructs and submits a two-party signed LoanSet transaction."""
        Style.print_info(f"Initiating a new loan between borrower '{borrower_account['name']}' and broker owner '{broker_owner_account['name']}'...")

        try:
            account_info = self.get_account_info(broker_owner_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for borrower '{borrower_account['name']}'. Is the account funded?")

        # Step 1: Construct the base transaction JSON (unsigned)
        base_tx_json = {
            "TransactionType": "LoanSet",
            "Account": broker_owner_account['address'],
            #"Counterparty": borrower_account['address'],
            "LoanBrokerID": loan_broker_id,
            "PrincipalRequested": principal_amount,
            "Fee": "20",  # Higher fee for multi-signature
            "Sequence": sequence
        }

        # Add optional fields from kwargs
        if kwargs.get("interest_rate") is not None:
            base_tx_json["InterestRate"] = kwargs["interest_rate"]
        if kwargs.get("late_interest_rate") is not None:
            base_tx_json["LateInterestRate"] = kwargs["late_interest_rate"]
        if kwargs.get("close_interest_rate") is not None:
            base_tx_json["CloseInterestRate"] = kwargs["close_interest_rate"]
        if kwargs.get("overpayment_interest_rate") is not None:
            base_tx_json["OverpaymentInterestRate"] = kwargs["overpayment_interest_rate"]
        
        if kwargs.get("payment_interval") is not None:
            base_tx_json["PaymentInterval"] = kwargs["payment_interval"]
        if kwargs.get("payment_total") is not None:
            base_tx_json["PaymentTotal"] = kwargs["payment_total"]
        
        if kwargs.get("origination_fee") is not None:
            base_tx_json["LoanOriginationFee"] = kwargs["origination_fee"]
        if kwargs.get("service_fee") is not None:
            base_tx_json["LoanServiceFee"] = kwargs["service_fee"]
        if kwargs.get("late_fee") is not None:
            base_tx_json["LatePaymentFee"] = kwargs["late_fee"]
        if kwargs.get("close_fee") is not None:
            base_tx_json["ClosePaymentFee"] = kwargs["close_fee"]
        if kwargs.get("overpayment_fee") is not None:
            base_tx_json["OverpaymentFee"] = kwargs["overpayment_fee"]
        
        if kwargs.get("grace_period") is not None:
            base_tx_json["GracePeriod"] = kwargs["grace_period"]

        sign_base_tx = {
            "method": "sign",
            "params": [{
                "secret": broker_owner_account['secret'],
                "tx_json": base_tx_json
            }]
        }

        sign_base_result = self._api_request(sign_base_tx)


        
        # Step 2: Sign the transaction as the counterparty (broker owner)
        Style.print_info("Step 1/3: Signing transaction as the broker owner (counterparty)...")
        broker_tx_json = sign_base_result['tx_json']
        broker_tx_json["Account"] = broker_owner_account['address']
        counterparty_sign_payload = {
            "method": "sign",
            "params": [{
                "secret": broker_owner_account['secret'],
                "tx_json": broker_tx_json
            }]
        }
        counterparty_sign_result = self._api_request(counterparty_sign_payload)
        
        # Extract the counterparty's signature details
        signed_tx_by_counterparty = counterparty_sign_result['tx_json']
        counterparty_pub_key = signed_tx_by_counterparty.get('SigningPubKey')
        counterparty_signature = signed_tx_by_counterparty.get('TxnSignature')

        if not all([counterparty_pub_key, counterparty_signature]):
            Style.print_error("Failed to retrieve signature from the counterparty signing step.")

        # Step 3: Add the counterparty's signature to the base transaction
        Style.print_info("Step 2/3: Adding counterparty signature to the transaction...")
        broker_tx_json["CounterpartySignature"] = {
            "SigningPubKey": counterparty_pub_key,
            "TxnSignature": counterparty_signature
        }
        #broker_tx_json["Account"] = borrower_account['address']
        print(f"loanset transaction {broker_tx_json}")
        # Step 4: Sign the complete transaction (with counterparty signature included) as the primary account (borrower)
        Style.print_info("Step 3/3: Signing the complete transaction as the borrower...")
        final_sign_payload = {
            "method": "sign",
            "params": [{
                "secret": broker_owner_account['secret'],
                "tx_json": broker_tx_json
            }]
        }
        final_sign_result = self._api_request(final_sign_payload)

        # Step 5: Submit the final, dual-signed transaction blob
        return self._submit_blob(final_sign_result['tx_blob'])

    def delete_loan(self, from_account: dict, loan_id: str):
        """Constructs and submits a LoanDelete transaction."""
        Style.print_info(f"Deleting loan '{loan_id}' owned by '{from_account['name']}'...")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanDelete",
            "Account": from_account['address'],
            "LoanID": loan_id,
            "Fee": "12",
            "Sequence": sequence
        }

        return self._sign_and_submit(tx_json, from_account['secret'])

    def manage_loan(self, from_account: dict, loan_id: str, flag: int):
        """Constructs and submits a LoanManage transaction."""
        action_map = {131072: "Impair", 65536: "Default", 262144: "Unimpair"}
        action_name = action_map.get(flag, "Unknown Action")

        Style.print_info(f"Performing '{action_name}' on loan '{loan_id}' for account '{from_account['name']}'...")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanManage",
            "Account": from_account['address'],
            "LoanID": loan_id,
            "Fee": "15",
            "Sequence": sequence,
            "Flags": flag
        }

        return self._sign_and_submit(tx_json, from_account['secret'])

    # In class RippleTool:

    def pay_loan(self, from_account: dict, loan_id: str, amount: str, flags: int = None):
        """Constructs and submits a LoanPay transaction."""
        Style.print_info(f"Submitting payment for loan '{loan_id}' from account '{from_account['name']}'...")

        try:
            account_info = self.get_account_info(from_account['address'])
            sequence = account_info['account_data']['Sequence']
        except Exception:
            Style.print_error(f"Could not get account info for '{from_account['name']}'. Is the account funded?")

        tx_json = {
            "TransactionType": "LoanPay",
            "Account": from_account['address'],
            "LoanID": loan_id,
            "Amount": amount,
            "Fee": "15",
            "Sequence": sequence
        }

        if flags is not None:
            tx_json["Flags"] = flags
            Style.print_info(f"Applying flags: {flags}")

        print(f"pay loan transaction json {tx_json}")

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

def extract_loan_broker_id(tx_json: dict) -> str | None:
    """
    Extracts LoanBrokerID from a LoanBrokerSet transaction's metadata.
    Returns the ID string if found, otherwise None.
    """
    try:
        affected_nodes = tx_json.get("meta", {}).get("AffectedNodes", [])
        for node in affected_nodes:
            created = node.get("CreatedNode")
            if created and created.get("LedgerEntryType") == "AccountRoot":
                new_fields = created.get("NewFields", {})
                if "LoanBrokerID" in new_fields:
                    return new_fields["LoanBrokerID"]
        return None
    except Exception:
        return None

def extract_loan_id(tx_json: dict) -> str | None:
    """
    Extracts the LoanID (LedgerIndex of the created Loan object) from a LoanSet
    transaction's metadata. Returns the ID string if found, otherwise None.
    """
    try:
        affected_nodes = tx_json.get("meta", {}).get("AffectedNodes", [])
        for node in affected_nodes:
            created = node.get("CreatedNode")
            if created and created.get("LedgerEntryType") == "Loan":
                return created.get("LedgerIndex")
        return None
    except Exception:
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
    subparsers.add_parser("ledger-accept", help="Advance the ledger")

    parser_ledger_entry = subparsers.add_parser("ledger-entry", help="Get a raw ledger object by its index/hash.")
    parser_ledger_entry.add_argument("--index", help="The 256-bit index (hash) of the ledger object.")

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

    # --- Loan Broker Commands ---
    parser_lb_set = subparsers.add_parser("loanbroker-set", help="Create or modify a LoanBroker for a vault.")
    parser_lb_set.add_argument("--from", dest="from_name", required=True, help="The vault owner's account.")
    parser_lb_set.add_argument("--vault-id", required=True, help="The ID of the vault this broker is associated with.")
    
    # Modification-specific
    parser_lb_set.add_argument("--loanbroker-id", help="The ID of an existing LoanBroker to modify. If omitted, a new one is created.")
    
    # Creation-specific
    parser_lb_set.add_argument("--fee-rate", type=int, help="[Create] Annual management fee rate (e.g., 500000000 for 5%%).")
    parser_lb_set.add_argument("--cover-min", type=int, help="[Create] Minimum collateralization ratio for new loans (e.g., 1200000000 for 120%%).")
    parser_lb_set.add_argument("--cover-liq", type=int, help="[Create] Liquidation collateralization ratio (e.g., 1050000000 for 105%%).")

    # Optional / Modifiable
    parser_lb_set.add_argument("--debt-max-amount", help="Optional: Maximum debt amount.")
    parser_lb_set.add_argument("--debt-max-currency", help="Optional: Currency for the maximum debt.")
    parser_lb_set.add_argument("--debt-max-issuer", help="Optional: Issuer for the maximum debt currency.")
    parser_lb_set.add_argument("--data", help="Optional: Hex-encoded arbitrary data.")

    subparsers.add_parser("loanbroker-list", help="List all saved loan broker IDs.")

    parser_lb_delete = subparsers.add_parser("loanbroker-delete", help="Delete an existing LoanBroker object.")
    parser_lb_delete.add_argument("--from", dest="from_name", required=True, help="The vault owner's account that created the broker.")
    parser_lb_delete.add_argument("--loanbroker-id", required=True, help="The ID of the LoanBroker to delete.")

    parser_lb_cover_deposit = subparsers.add_parser("loanbroker-cover-deposit", help="Deposit cover assets to a LoanBroker.")
    parser_lb_cover_deposit.add_argument("--from", dest="from_name", required=True, help="The account making the deposit.")
    parser_lb_cover_deposit.add_argument("--loanbroker-id", required=True, help="The ID of the LoanBroker to deposit into.")
    parser_lb_cover_deposit.add_argument("--amount", required=True, help="The amount of the currency to deposit.")
    parser_lb_cover_deposit.add_argument("--currency", required=True, help="The currency to deposit (e.g., 'XRP', 'USD').")
    parser_lb_cover_deposit.add_argument("--issuer", dest="issuer_name", help="The issuer of the asset (required for non-XRP currencies).")

    parser_lb_cover_withdraw = subparsers.add_parser("loanbroker-cover-withdraw", help="Withdraw cover assets from a LoanBroker.")
    parser_lb_cover_withdraw.add_argument("--from", dest="from_name", required=True, help="The account initiating the withdrawal.")
    parser_lb_cover_withdraw.add_argument("--loanbroker-id", required=True, help="The ID of the LoanBroker to withdraw from.")
    parser_lb_cover_withdraw.add_argument("--amount", required=True, help="The amount of the currency to withdraw.")
    parser_lb_cover_withdraw.add_argument("--currency", required=True, help="The currency to withdraw (e.g., 'XRP', 'USD').")
    parser_lb_cover_withdraw.add_argument("--issuer", dest="issuer_name", help="The issuer of the asset (required for non-XRP currencies).")
    parser_lb_cover_withdraw.add_argument("--destination", dest="destination_name", help="Optional: A different account name to receive the withdrawn assets.")

    parser_lb_cover_clawback = subparsers.add_parser("loanbroker-cover-clawback", help="Clawback cover assets from a LoanBroker (issuer only).")
    parser_lb_cover_clawback.add_argument("--from", dest="from_name", required=True, help="The account initiating the clawback (must be the asset issuer).")
    parser_lb_cover_clawback.add_argument("--loanbroker-id", required=True, help="The ID of the LoanBroker to clawback from.")
    parser_lb_cover_clawback.add_argument("--amount", required=True, help="The amount of the currency to clawback.")
    parser_lb_cover_clawback.add_argument("--currency", required=True, help="The IOU currency to clawback (e.g., 'USD').")

    # --- LoanSet Command ---
    parser_loanset = subparsers.add_parser("loanset", help="Create a new two-party loan agreement.")
    parser_loanset.add_argument("--borrower", required=True, help="The name of the borrower's account.")
    parser_loanset.add_argument("--broker-owner", required=True, help="The name of the loan broker's owner account.")
    parser_loanset.add_argument("--loanbroker-id", required=True, help="The ID of the LoanBroker facilitating the loan.")
    # Principal
    parser_loanset.add_argument("--principal-amount", required=True, help="The principal amount of the loan (as an IOU).")
    
    # Rates
    parser_loanset.add_argument("--interest-rate", type=int, help="Optional: Normal interest rate.")
    parser_loanset.add_argument("--late-interest-rate", type=int, help="Optional: Interest rate for late payments.")
    parser_loanset.add_argument("--close-interest-rate", type=int, help="Optional: Interest rate for closing the loan.")
    parser_loanset.add_argument("--overpayment-interest-rate", type=int, help="Optional: Interest rate for overpayments.")
    # Fees
    parser_loanset.add_argument("--origination-fee", help="Optional: Fee paid to the broker upon loan creation.")
    parser_loanset.add_argument("--service-fee", help="Optional: Periodic service fee.")
    parser_loanset.add_argument("--late-fee", help="Optional: Fee for late payments.")
    parser_loanset.add_argument("--close-fee", help="Optional: Fee for closing the loan.")
    parser_loanset.add_argument("--overpayment-fee", help="Optional: Fee for making an overpayment.")
    # Terms
    parser_loanset.add_argument("--payment-interval", type=int, help="Optional: Seconds between payments.")
    parser_loanset.add_argument("--payment-total", type=int, help="Optional: Total number of payments.")
    parser_loanset.add_argument("--grace-period", type=int, help="Optional: Seconds after due date before a payment is considered late.")

    # --- LoanDelete Command ---
    parser_loan_delete = subparsers.add_parser("loan-delete", help="Delete an existing Loan object.")
    parser_loan_delete.add_argument("--from", dest="from_name", required=True, help="The account that owns the loan.")
    parser_loan_delete.add_argument("--loan-id", required=True, help="The ID of the Loan to delete.")

    # --- LoanManage Command ----
    parser_loan_manage = subparsers.add_parser("loan-manage", help="Perform management actions on an existing loan (impair, default, unimpair).")
    parser_loan_manage.add_argument("--from", dest="from_name", required=True, help="The loan broker owner's account.")
    parser_loan_manage.add_argument("--loan-id", required=True, help="The 256-bit ID (hash) of the loan to manage.")
    parser_loan_manage.add_argument("--action", choices=['impair', 'default', 'unimpair'], help="The action to perform: 'impair' (paper loss), 'default' (realize loss), or 'unimpair' (reverse impairment).")

    # --- LoanPay Command ---
    parser_loan_pay = subparsers.add_parser("loan-pay", help="Make a payment on an existing loan.")
    parser_loan_pay.add_argument("--from", dest="from_name", required=True, help="The borrower's account making the payment.")
    parser_loan_pay.add_argument("--loan-id", required=True, help="The ID of the loan to pay.")
    parser_loan_pay.add_argument("--amount", required=True, help="The amount to pay.")
    parser_loan_pay.add_argument("--flags", type=int, help="Optional flags (e.g., 1048576 for tfLoanPayPartial).")


    args = parser.parse_args()
    
    accounts = AccountManager(ACCOUNTS_FILE)
    vaults = VaultsManager(VAULTS_FILE)
    loan_brokers = LoanBrokerManager(LOANBROKER_FILE)
    tool = RippleTool(RIPPLED_URL)
    loans = LoanManager(LOANS_FILE)

    if args.command == "list":
        accounts.list_accounts()
    elif args.command == "ledger-accept":
        tool.close_ledger()
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
        try:
            tool.send_payment(from_account, to_account['address'], args.amount)
        finally:
            tool.close_ledger()
        print("\nVerifying balances...")
        run_balance_check(accounts, tool, from_account['name'])
        run_balance_check(accounts, tool, to_account['name'])
    elif args.command == "send":
        from_account = accounts.get_account(args.from_name)
        to_account = accounts.get_account(args.to_name)
        try:
            tool.send_payment(from_account, to_account['address'], args.amount)
        finally:
            tool.close_ledger()
        print("\nVerifying balances...")
        run_balance_check(accounts, tool, from_account['name'])
        run_balance_check(accounts, tool, to_account['name'])
    elif args.command == "trustset":
        from_account = accounts.get_account(args.from_name)
        issuer_account = accounts.get_account(args.issuer_name)
        try:
            tool.set_trust_line(from_account, issuer_account['address'], args.currency, args.limit)
        finally:
            tool.close_ledger()
        Style.print_success(f"Trust line from '{from_account['name']}' to '{issuer_account['name']}' should now be active.")
    elif args.command == "iou-send":
        from_account = accounts.get_account(args.from_name) # Issuer
        to_account = accounts.get_account(args.to_name)     # Receiver
        try:
            tool.send_iou(from_account, to_account['address'], args.amount, args.currency)
        finally:
            tool.close_ledger()
        print("\nVerifying IOU balance...")
        run_iou_balance_check(accounts, tool, to_account['name'])
    elif args.command == "iou-balance":
        run_iou_balance_check(accounts, tool, args.name)
    elif args.command == "delegate-set":
        from_account = accounts.get_account(args.from_name)
        to_account = accounts.get_account(args.to_name)
        try:
            tool.set_delegation(from_account, to_account['address'], args.permission_code)
        finally:
            tool.close_ledger()
        Style.print_success(f"Delegation permission should now be active from '{from_account['name']}' to '{to_account['name']}'.")
    elif args.command == "delegated-send":
        main_account = accounts.get_account(args.main_account)
        delegate_account = accounts.get_account(args.delegate_account)
        to_account = accounts.get_account(args.to_name)
        try:
            tool.send_delegated_payment(main_account, delegate_account, to_account['address'], args.amount)
        finally:
            tool.close_ledger()
        print("\nVerifying balances...")
        run_balance_check(accounts, tool, main_account['name'])
        run_balance_check(accounts, tool, to_account['name'])
        print("---")
        run_balance_check(accounts, tool, delegate_account['name'])
    elif args.command == "vault-create":
        from_account = accounts.get_account(args.from_name)
        submit_result = None
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
        
        try:
            submit_result = tool.create_vault(from_account, asset_json, **optional_args)
        finally:
            tool.close_ledger()

        Style.print_success(f"Vault creation transaction submitted for account '{from_account['name']}'.")
        
        if not submit_result or 'tx_json' not in submit_result or 'hash' not in submit_result['tx_json']:
            Style.print_error("Submission did not return transaction hash. Can't fetch transaction.")
        
        txn_hash_local = submit_result['tx_json']['hash']
        txn_result = tool.get_transaction(txn_hash_local)
        vault_id = extract_vault_id(txn_result)
        print(f"Vault ID: {vault_id}")

        if vault_id:
            vaults.add_vault(from_account['name'], vault_id)
        else:
            Style.print_info("VaultID not found in transaction result. It may not have been created or transaction format differs.")

    elif args.command == "tx":
        tx_result = tool.get_transaction(args.hash)
        print(json.dumps(tx_result, indent=4))
        
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

    elif args.command == "vault-set":
        from_account = accounts.get_account(args.from_name)
        
        optional_args = {
            "assets_maximum": args.max_assets,
            "domain_id": args.domain_id,
            "data": args.data
        }
        
        try:
            tool.set_vault(from_account, args.vault_id, **optional_args)
        finally:
            tool.close_ledger()
        Style.print_success(f"VaultSet transaction submitted for vault '{args.vault_id}'.")

    elif args.command == "vault-info":
        vault_info_result = tool.get_vault_info(args.vault_id)
        print(json.dumps(vault_info_result, indent=4))

    elif args.command == "vault-deposit":
        from_account = accounts.get_account(args.from_name)
        amount_to_deposit = None

        if args.currency.upper() == 'XRP':
            amount_to_deposit = xrp_to_drops(args.amount)
        else:
            if not args.issuer_name:
                Style.print_error("The --issuer flag is required for non-XRP currencies.")
            issuer_account = accounts.get_account(args.issuer_name)
            amount_to_deposit = {
                "currency": args.currency,
                "issuer": issuer_account['address'],
                "value": args.amount
            }
        
        try:
            tool.deposit_to_vault(from_account, args.vault_id, amount_to_deposit)
        finally:
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
            if not args.issuer_name:
                    Style.print_error("The --issuer flag is required when withdrawing a non-XRP asset.")
            issuer_account = accounts.get_account(args.issuer_name)
            amount_to_withdraw = {
                "currency": args.currency,
                "issuer": issuer_account['address'],
                "value": args.amount
            }
        else:
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
        
        try:
            tool.withdraw_from_vault(from_account, args.vault_id, amount_to_withdraw, destination_address)
        finally:
            tool.close_ledger()
        
        Style.print_success(f"VaultWithdraw transaction submitted for vault '{args.vault_id}'.")
        print("Use 'balance' and 'vault-info' commands to verify changes.")

    elif args.command == "account-set":
        from_account = accounts.get_account(args.from_name)
        try:
            tool.account_set(from_account, set_flag=args.set_flag, clear_flag=args.clear_flag)
        finally:
            tool.close_ledger()
        Style.print_success(f"AccountSet transaction submitted for '{from_account['name']}'.")

    elif args.command == "loanbroker-set":
        from_account = accounts.get_account(args.from_name)
        submit_result = None
        
        optional_args = {
            "loan_broker_id": args.loanbroker_id,
            "data": args.data
        }

        debt_maximum = None
        if args.debt_max_amount and args.debt_max_currency:
            if args.debt_max_currency.upper() == 'XRP':
                debt_maximum = xrp_to_drops(args.debt_max_amount)
            else:
                if not args.debt_max_issuer:
                    Style.print_error("The --debt-max-issuer is required for non-XRP debt maximums.")
                issuer_account = accounts.get_account(args.debt_max_issuer)
                debt_maximum = args.debt_max_amount
        optional_args["debt_maximum"] = debt_maximum

        if args.loanbroker_id:
            if debt_maximum is None and args.data is None:
                Style.print_error("For modification, at least one optional field (--debt-max-amount or --data) must be provided.")
        else:
            if not all([args.fee_rate is not None, args.cover_min is not None, args.cover_liq is not None]):
                Style.print_error("For creation, --fee-rate, --cover-min, and --cover-liq are all required.")
            optional_args["management_fee_rate"] = args.fee_rate
            optional_args["cover_rate_minimum"] = args.cover_min
            optional_args["cover_rate_liquidation"] = args.cover_liq
            
        try:
            submit_result = tool.set_loan_broker(from_account, args.vault_id, **optional_args)
        finally:
            tool.close_ledger()
        Style.print_success(f"LoanBrokerSet transaction submitted for vault '{args.vault_id}'.")
        
        if not submit_result or 'tx_json' not in submit_result or 'hash' not in submit_result['tx_json']:
            Style.print_error("Submission did not return transaction hash. Can't fetch transaction.")
        
        txn_hash_local = submit_result['tx_json']['hash']
        txn_result = tool.get_transaction(txn_hash_local)
        loan_broker_id = extract_loan_broker_id(txn_result)

        if loan_broker_id:
            loan_brokers.add_broker(from_account['name'], loan_broker_id)
        else:
            Style.print_info("loan_broker_id not found in transaction result. It may not have been created or transaction format differs.")

    elif args.command == "loanbroker-list":
        loan_brokers.list_brokers()

    elif args.command == "loanbroker-delete":
        from_account = accounts.get_account(args.from_name)
        try:
            tool.delete_loan_broker(from_account, args.loanbroker_id)
        finally:
            tool.close_ledger()
        Style.print_success(f"LoanBrokerDelete transaction submitted for ID '{args.loanbroker_id}'.")

    elif args.command == "loanbroker-cover-deposit":
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
        
        try:
            tool.deposit_loan_broker_cover(from_account, args.loanbroker_id, amount_to_deposit)
        finally:
            tool.close_ledger()
        
        Style.print_success(f"LoanBrokerCoverDeposit transaction submitted for LoanBroker '{args.loanbroker_id}'.")

    elif args.command == "loanbroker-cover-withdraw":
        from_account = accounts.get_account(args.from_name)
        amount_to_withdraw = None
        destination_address = None

        if args.destination_name:
            destination_account = accounts.get_account(args.destination_name)
            destination_address = destination_account['address']

        if args.currency.upper() == 'XRP':
            amount_to_withdraw = xrp_to_drops(args.amount)
        else:
            if not args.issuer_name:
                Style.print_error("The --issuer flag is required for non-XRP currencies.")
            issuer_account = accounts.get_account(args.issuer_name)
            amount_to_withdraw = {
                "currency": args.currency,
                "issuer": issuer_account['address'],
                "value": args.amount
            }
        
        try:
            tool.withdraw_loan_broker_cover(from_account, args.loanbroker_id, amount_to_withdraw, destination_address)
        finally:
            tool.close_ledger()
        
        Style.print_success(f"LoanBrokerCoverWithdraw transaction submitted for LoanBroker '{args.loanbroker_id}'.")

    elif args.command == "loanbroker-cover-clawback":
        from_account = accounts.get_account(args.from_name)

        if args.currency.upper() == 'XRP':
            Style.print_error("Cannot clawback XRP. Clawback is only for issued currencies.")

        # For a clawback, the from_account is the issuer.
        amount_to_clawback = {
            "currency": args.currency,
            "issuer": from_account['address'],
            "value": args.amount
        }
        
        try:
            tool.clawback_loan_broker_cover(from_account, args.loanbroker_id, amount_to_clawback)
        finally:
            tool.close_ledger()
        
        Style.print_success(f"LoanBrokerCoverClawback transaction submitted for LoanBroker '{args.loan_broker_id}'.")
    
    elif args.command == "loanset":
        borrower_account = accounts.get_account(args.borrower)
        broker_owner_account = accounts.get_account(args.broker_owner)
        

        principal_amount = args.principal_amount
        

        optional_args = {
            "interest_rate": args.interest_rate,
            "late_interest_rate": args.late_interest_rate,
            "close_interest_rate": args.close_interest_rate,
            "overpayment_interest_rate": args.overpayment_interest_rate,
            "payment_interval": args.payment_interval,
            "payment_total": args.payment_total,
            "origination_fee": args.origination_fee,
            "service_fee": args.service_fee,
            "late_fee": args.late_fee,
            "close_fee": args.close_fee,
            "overpayment_fee": args.overpayment_fee,
            "grace_period": args.grace_period,
        }

        if args.origination_fee:
            optional_args["origination_fee"] =  args.origination_fee
            

        try:
            submit_result = tool.set_loan(
                borrower_account,
                broker_owner_account,
                args.loanbroker_id,
                principal_amount,
                **optional_args
            )
        finally:
            tool.close_ledger()
        
        Style.print_success(f"LoanSet transaction submitted successfully.")
        

        if not submit_result or 'tx_json' not in submit_result or 'hash' not in submit_result['tx_json']:
            Style.print_error("Submission did not return transaction hash. Can't fetch transaction.")
        
        txn_hash = submit_result['tx_json']['hash']
        txn_result = tool.get_transaction(txn_hash)
        loan_id = extract_loan_id(txn_result)

        if loan_id:
            loans.add_loan(borrower_account['name'], loan_id)
        else:
            Style.print_info("LoanID not found in transaction result. A loan may not have been created.")

    elif args.command == "loan-delete":
        from_account = accounts.get_account(args.from_name)
        try:
            tool.delete_loan(from_account, args.loan_id)
        finally:
            tool.close_ledger()
        Style.print_success(f"LoanDelete transaction submitted for ID '{args.loan_id}'.")

    elif args.command == "loan-list":
        loans.list_loans()
    
    elif args.command == "loan-manage":
        from_account = accounts.get_account(args.from_name)

        # Map the action string to the corresponding flag integer
        action_flags = {
            "impair": 131072,    # tfLoanImpair
            "default": 65536,     # tfLoanDefault
            "unimpair": 262144   # tfLoanUnimpair
        }
        flag_to_set = action_flags[args.action]
        
        try:
            tool.manage_loan(from_account, args.loan_id, flag_to_set)
        finally:
            tool.close_ledger()
        
        Style.print_success(f"LoanManage transaction ('{args.action}') submitted for loan '{args.loan_id}'.")

    # In the main() function's command-handling block:

    elif args.command == "loan-pay":
        from_account = accounts.get_account(args.from_name)
        amount_to_pay =  args.amount
        try:
            tool.pay_loan(from_account, args.loan_id, amount_to_pay, flags=args.flags)
        finally:
            tool.close_ledger()
            
        
        Style.print_success(f"LoanPay transaction submitted for loan '{args.loan_id}'.")

    elif args.command == "ledger-entry":
        try:
            entry_result = tool.get_ledger_entry(args.index)
            # Pretty-print the full JSON response
            print(json.dumps(entry_result, indent=4))
        except Exception as e:
            Style.print_error(f"Could not retrieve ledger entry: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # The Style.print_error method has already printed the detailed error.
        # This block ensures the script exits with a non-zero status code
        # to indicate failure, which is standard practice for CLI tools.
        sys.exit(1)
