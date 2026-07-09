"""
MongoDB connection layer for the POS system.
All collections used by the app live inside the "my_data_vuthy" database.
The client / customer collection is kept as "client_data001", exactly as requested.
"""
import os
from pymongo import MongoClient, ReturnDocument
from dotenv import load_dotenv

load_dotenv()


class Database:
    def __init__(self):
        connection_string = os.environ.get(
            "MONGO_URI",
            "mongodb+srv://vuthywelcomeworld_db_user:SsHSp5qbVx6OgN6R@cluster0.sjrgymz.mongodb.net/?appName=Cluster0",
        )
        self.client = MongoClient(connection_string)

        # --- database ---
        self.db = self.client["my_data_vuthy"]

        # --- collections ---
        self.items = self.db["items"]                    # products / stock items
        self.collection = self.db["client_data001"]       # <-- clients, name kept as specified
        self.clients = self.collection                    # convenience alias
        self.sales = self.db["sales"]                      # sale / invoice records
        self.deliveries = self.db["deliveries"]            # delivery tracking
        self.stock_movements = self.db["stock_movements"]  # stock in / out / adjust log
        self.users = self.db["users"]                      # login accounts
        self.counters = self.db["counters"]                # auto-increment helper

        self._ensure_indexes()

    def _ensure_indexes(self):
        self.items.create_index("barcode", unique=False)
        self.items.create_index("sku", unique=False)
        self.clients.create_index("phone", unique=False)
        self.sales.create_index("invoice_no", unique=True)
        self.deliveries.create_index("sale_id")
        self.users.create_index("username", unique=True)

    def get_next_sequence(self, name: str) -> int:
        """Atomic auto-increment counter, used for invoice numbers, etc."""
        result = self.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return result["seq"]


db = Database()
