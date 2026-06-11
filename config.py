import os

from dotenv import load_dotenv

load_dotenv()

# Supabase (supply-chain-service project hosts the `backorder` schema)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Shopify
SHOP_URL = os.getenv("SHOP_URL")  # store.myshopify.com
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION = os.getenv("API_VERSION", "2025-10")

# Webhook verification (either is sufficient; both optional in dev)
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
EXTERNAL_HMAC_SECRET = os.getenv("EXTERNAL_HMAC_SECRET", "")  # gateway X-Gateway-Signature

# Admin API
ADMIN_TOKEN = os.getenv("BACKORDER_ADMIN_TOKEN")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,https://admin.kitchenartsandletters.com",
    ).split(",")
    if o.strip()
]

# Classification
# Products carrying any of these tags are never treated as backorders
# (preorder lifecycle is pub-date driven and owned by preorder-service;
#  out-of-print titles are not expected to restock).
EXCLUDED_TAGS = {
    t.strip().lower()
    for t in os.getenv(
        "BACKORDER_EXCLUDED_TAGS", "preorder,out-of-print,oop"
    ).split(",")
    if t.strip()
}

# Order tagger
TAG_BACKORDER = os.getenv("BACKORDER_ORDER_TAG", "backorder")
TAGGER_VERSION = "1.0.0"
