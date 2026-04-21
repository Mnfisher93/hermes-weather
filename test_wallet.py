from dotenv import load_dotenv
load_dotenv()
import os
from py_clob_client.client import ClobClient

c = ClobClient(
    os.getenv("CLOB_HOST"),
    key=os.getenv("POLYMARKET_PRIVATE_KEY"),
    chain_id=int(os.getenv("CHAIN_ID")),
    signature_type=1,
    funder=os.getenv("POLYMARKET_FUNDER"),
)
c.set_api_creds(c.create_or_derive_api_creds())
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
print("Wallet OK:", c.get_ok())
print("USDC Balance:", c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)))
