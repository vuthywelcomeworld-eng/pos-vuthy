import os
import io
import re
import base64
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, send_file, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

from db import db
from utils import (
    to_json_safe, parse_object_id, login_required, roles_required,
    rows_to_csv_bytes, rows_to_xlsx_bytes, invoice_number
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
LOW_STOCK_THRESHOLD = int(os.environ.get("LOW_STOCK_THRESHOLD", 5))

DEFAULT_CITIES = [
    "Phnom Penh", "Kandal", "Siem Reap", "Battambang", "Kampong Cham",
    "Kampong Thom", "Kampong Chhnang", "Kampong Speu", "Kampot", "Kep",
    "Koh Kong", "Kratie", "Mondulkiri", "Oddar Meanchey", "Pailin",
    "Preah Vihear", "Prey Veng", "Pursat", "Ratanakiri", "Sihanoukville",
    "Stung Treng", "Svay Rieng", "Takeo", "Banteay Meanchey", "Tboung Khmum",
]


# ----------------------------------------------------------------------------
# Bootstrap defaults on first run
# ----------------------------------------------------------------------------
def seed_admin():
    if db.users.count_documents({}) == 0:
        db.users.insert_one({
            "username": "admin",
            "password": generate_password_hash("admin123"),
            "name": "Administrator",
            "role": "admin",
            "created_at": datetime.utcnow(),
        })


def seed_cities():
    if db.cities.count_documents({}) == 0:
        db.cities.insert_many([{"name": c, "created_at": datetime.utcnow()} for c in DEFAULT_CITIES])


seed_admin()
seed_cities()


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _item_duplicate_exists(name, barcode, sku, exclude_id=None):
    conditions = []
    if name:
        conditions.append({"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}})
    if barcode:
        conditions.append({"barcode": barcode})
    if sku:
        conditions.append({"sku": sku})
    if not conditions:
        return False
    query = {"$or": conditions}
    if exclude_id:
        query["_id"] = {"$ne": exclude_id}
    return db.items.find_one(query) is not None


def _client_phone_exists(phone, exclude_id=None):
    if not phone:
        return False
    query = {"phone": phone}
    if exclude_id:
        query["_id"] = {"$ne": exclude_id}
    return db.clients.find_one(query) is not None


def _sale_cost_and_profit(sale):
    """Returns (total_cost, gross_profit) for a sale doc, falling back to current
    item cost for older sales created before cost-tracking was added."""
    if "total_cost" in sale and "gross_profit" in sale:
        return sale["total_cost"], sale["gross_profit"]
    total_cost = 0.0
    for line in sale.get("items", []):
        if "cost_subtotal" in line:
            total_cost += line["cost_subtotal"]
        else:
            item = db.items.find_one({"_id": line.get("item_id")}, {"cost": 1})
            cost = float(item.get("cost", 0)) if item else 0.0
            total_cost += cost * line.get("qty", 0)
    taxable = max(sale.get("subtotal", 0) - sale.get("discount", 0), 0)
    gross_profit = round(taxable - total_cost, 2)
    return round(total_cost, 2), gross_profit


def _date_range_args(default_today=True):
    """Parses ?from=&to= query params into (start, end) datetimes.
    If neither is given and default_today is True, defaults to today's range."""
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    if not date_from and not date_to and default_today:
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start, end
    start = datetime.strptime(date_from, "%Y-%m-%d") if date_from else datetime(2000, 1, 1)
    end = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)) if date_to else datetime.utcnow() + timedelta(days=1)
    return start, end


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.users.find_one({"username": username})
        if user and check_password_hash(user["password"], password):
            session["user_id"] = str(user["_id"])
            session["username"] = user["username"]
            session["name"] = user.get("name", user["username"])
            session["role"] = user.get("role", "cashier")
            return redirect(url_for("dashboard"))
        flash("ឈ្មោះអ្នកប្រើ ឬពាក្យសម្ងាត់មិនត្រឹមត្រូវ / Invalid username or password", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/pos")
@login_required
def pos():
    return render_template("pos.html")


@app.route("/items")
@login_required
def items_page():
    return render_template("items.html")


@app.route("/stock")
@login_required
def stock_page():
    return render_template("stock.html")


@app.route("/clients")
@login_required
def clients_page():
    return render_template("clients.html")


@app.route("/delivery")
@login_required
def delivery_page():
    return render_template("delivery.html")


@app.route("/reports")
@login_required
def reports_page():
    return render_template("reports.html")


@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html")


@app.route("/receipt/<sale_id>")
@login_required
def receipt_page(sale_id):
    oid = parse_object_id(sale_id)
    sale = db.sales.find_one({"_id": oid}) if oid else None
    if not sale:
        return "Sale not found", 404
    store = db.settings.find_one({"_id": "store"}) or {}
    return render_template("receipt.html", sale=to_json_safe(sale), store=to_json_safe(store))


# ----------------------------------------------------------------------------
# API : SETTINGS (store logo / name)
# ----------------------------------------------------------------------------
@app.route("/api/settings", methods=["GET"])
@login_required
def api_settings_get():
    s = db.settings.find_one({"_id": "store"}) or {}
    return jsonify(to_json_safe(s))


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_update():
    data = request.get_json(force=True)
    update = {}
    if "logo" in data:
        update["logo"] = data["logo"]
    if "store_name" in data:
        update["store_name"] = data.get("store_name", "").strip()
    if "phone" in data:
        update["phone"] = data.get("phone", "").strip()
    if "address" in data:
        update["address"] = data.get("address", "").strip()
    db.settings.update_one({"_id": "store"}, {"$set": update}, upsert=True)
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# API : CITIES / PROVINCES
# ----------------------------------------------------------------------------
@app.route("/api/cities", methods=["GET"])
@login_required
def api_cities_list():
    cities = list(db.cities.find().sort("name", 1))
    return jsonify([c["name"] for c in cities])


@app.route("/api/cities", methods=["POST"])
@login_required
def api_cities_create():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name_required"}), 400
    existing = db.cities.find_one({"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}})
    if existing:
        return jsonify({"error": "duplicate", "message": "City already exists"}), 409
    db.cities.insert_one({"name": name, "created_at": datetime.utcnow()})
    return jsonify({"ok": True, "name": name}), 201


# ----------------------------------------------------------------------------
# API : DELIVERY AGENTS
# ----------------------------------------------------------------------------
@app.route("/api/delivery-agents", methods=["GET"])
@login_required
def api_agents_list():
    agents = list(db.delivery_agents.find().sort("name", 1))
    return jsonify(to_json_safe(agents))


@app.route("/api/delivery-agents", methods=["POST"])
@login_required
def api_agents_create():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name_required"}), 400
    doc = {
        "name": name,
        "phone": data.get("phone", "").strip(),
        "vehicle": data.get("vehicle", "").strip(),
        "active": True,
        "created_at": datetime.utcnow(),
    }
    result = db.delivery_agents.insert_one(doc)
    doc["_id"] = result.inserted_id
    return jsonify(to_json_safe(doc)), 201


@app.route("/api/delivery-agents/<agent_id>", methods=["PUT"])
@login_required
def api_agents_update(agent_id):
    oid = parse_object_id(agent_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    data = request.get_json(force=True)
    update = {}
    for f in ("name", "phone", "vehicle", "active"):
        if f in data:
            update[f] = data[f]
    db.delivery_agents.update_one({"_id": oid}, {"$set": update})
    return jsonify({"ok": True})


@app.route("/api/delivery-agents/<agent_id>", methods=["DELETE"])
@login_required
def api_agents_delete(agent_id):
    oid = parse_object_id(agent_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    db.delivery_agents.delete_one({"_id": oid})
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# API : ITEMS
# ----------------------------------------------------------------------------
@app.route("/api/items", methods=["GET"])
@login_required
def api_items_list():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    sort_by = request.args.get("sort_by", "name")
    sort_dir = -1 if request.args.get("sort_dir", "asc") == "desc" else 1

    conditions = []
    if q:
        conditions.append({"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"sku": {"$regex": q, "$options": "i"}},
            {"barcode": {"$regex": q, "$options": "i"}},
            {"category": {"$regex": q, "$options": "i"}},
        ]})
    if category and category != "All":
        conditions.append({"category": category})
    query = {}
    if len(conditions) == 1:
        query = conditions[0]
    elif len(conditions) > 1:
        query = {"$and": conditions}

    allowed_sort = {"name", "price", "cost", "stock", "category", "sku"}
    if sort_by not in allowed_sort:
        sort_by = "name"
    items = list(db.items.find(query).sort(sort_by, sort_dir))
    return jsonify(to_json_safe(items))


@app.route("/api/items/categories", methods=["GET"])
@login_required
def api_items_categories():
    cats = db.items.distinct("category")
    return jsonify(sorted([c for c in cats if c]))


@app.route("/api/items/lookup/<code>", methods=["GET"])
@login_required
def api_items_lookup(code):
    """Used by the POS barcode / QR scanner to find an item instantly."""
    item = db.items.find_one({"$or": [{"barcode": code}, {"sku": code}]})
    if not item:
        return jsonify({"error": "not_found"}), 404
    return jsonify(to_json_safe(item))


@app.route("/api/items", methods=["POST"])
@login_required
def api_items_create():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    barcode = data.get("barcode", "").strip()
    sku = data.get("sku", "").strip()

    if _item_duplicate_exists(name, barcode, sku):
        return jsonify({"error": "duplicate_item",
                         "message": "An item with this name, SKU, or barcode already exists"}), 409

    doc = {
        "name": name,
        "sku": sku,
        "barcode": barcode,
        "category": data.get("category", "").strip() or "General",
        "unit": data.get("unit", "pcs"),
        "price": float(data.get("price", 0) or 0),
        "cost": float(data.get("cost", 0) or 0),
        "stock": float(data.get("stock", 0) or 0),
        "image": data.get("image", ""),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    result = db.items.insert_one(doc)
    if doc["stock"] > 0:
        db.stock_movements.insert_one({
            "item_id": result.inserted_id,
            "item_name": doc["name"],
            "type": "in",
            "qty": doc["stock"],
            "reason": "Initial stock",
            "created_at": datetime.utcnow(),
            "user": session.get("username"),
        })
    doc["_id"] = result.inserted_id
    return jsonify(to_json_safe(doc)), 201


@app.route("/api/items/<item_id>", methods=["PUT"])
@login_required
def api_items_update(item_id):
    oid = parse_object_id(item_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    barcode = data.get("barcode", "").strip()
    sku = data.get("sku", "").strip()

    if _item_duplicate_exists(name, barcode, sku, exclude_id=oid):
        return jsonify({"error": "duplicate_item",
                         "message": "Another item with this name, SKU, or barcode already exists"}), 409

    update = {
        "name": name,
        "sku": sku,
        "barcode": barcode,
        "category": data.get("category", "").strip() or "General",
        "unit": data.get("unit", "pcs"),
        "price": float(data.get("price", 0) or 0),
        "cost": float(data.get("cost", 0) or 0),
        "updated_at": datetime.utcnow(),
    }
    if "image" in data:
        update["image"] = data["image"]
    db.items.update_one({"_id": oid}, {"$set": update})
    return jsonify({"ok": True})


@app.route("/api/items/<item_id>", methods=["DELETE"])
@login_required
def api_items_delete(item_id):
    oid = parse_object_id(item_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    db.items.delete_one({"_id": oid})
    return jsonify({"ok": True})


@app.route("/api/items/<item_id>/qrcode", methods=["GET"])
@login_required
def api_item_qrcode(item_id):
    """Generates a QR code image (base64 PNG) encoding the item's barcode/sku, for label printing."""
    import qrcode
    oid = parse_object_id(item_id)
    item = db.items.find_one({"_id": oid}) if oid else None
    if not item:
        return jsonify({"error": "not_found"}), 404
    payload = item.get("barcode") or item.get("sku") or str(item["_id"])
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return jsonify({"image": f"data:image/png;base64,{b64}", "payload": payload})


# ----------------------------------------------------------------------------
# API : STOCK
# ----------------------------------------------------------------------------
@app.route("/api/stock/adjust", methods=["POST"])
@login_required
def api_stock_adjust():
    data = request.get_json(force=True)
    item_id = parse_object_id(data.get("item_id"))
    move_type = data.get("type")  # in | out | adjust
    qty = float(data.get("qty", 0) or 0)
    reason = data.get("reason", "")

    item = db.items.find_one({"_id": item_id})
    if not item:
        return jsonify({"error": "item_not_found"}), 404

    if move_type == "in":
        new_stock = item["stock"] + qty
    elif move_type == "out":
        new_stock = item["stock"] - qty
        if new_stock < 0:
            return jsonify({"error": "insufficient_stock"}), 400
    else:  # direct adjustment to an exact value
        new_stock = qty

    db.items.update_one({"_id": item_id}, {"$set": {"stock": new_stock, "updated_at": datetime.utcnow()}})
    db.stock_movements.insert_one({
        "item_id": item_id,
        "item_name": item["name"],
        "type": move_type,
        "qty": qty,
        "reason": reason,
        "created_at": datetime.utcnow(),
        "user": session.get("username"),
    })
    return jsonify({"ok": True, "new_stock": new_stock})


@app.route("/api/stock/movements", methods=["GET"])
@login_required
def api_stock_movements():
    moves = list(db.stock_movements.find().sort("created_at", -1).limit(200))
    return jsonify(to_json_safe(moves))


# ----------------------------------------------------------------------------
# API : CLIENTS  (collection name kept as "client_data001")
# ----------------------------------------------------------------------------
@app.route("/api/clients", methods=["GET"])
@login_required
def api_clients_list():
    q = request.args.get("q", "").strip()
    sort_by = request.args.get("sort_by", "name")
    sort_dir = request.args.get("sort_dir", "asc")
    query = {}
    if q:
        query = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
            {"code": {"$regex": q, "$options": "i"}},
            {"city": {"$regex": q, "$options": "i"}},
        ]}
    clients = list(db.clients.find(query))

    # attach purchase stats
    counts = db.sales.aggregate([
        {"$match": {"client_id": {"$ne": None}}},
        {"$group": {"_id": "$client_id", "count": {"$sum": 1}, "total_spent": {"$sum": "$total"}}},
    ])
    counts_map = {str(c["_id"]): c for c in counts}
    for c in clients:
        stat = counts_map.get(str(c["_id"]), {})
        c["purchase_count"] = stat.get("count", 0)
        c["total_spent"] = round(stat.get("total_spent", 0), 2)
        c["favorite"] = c.get("favorite", False)

    allowed_sort = {"name", "purchase_count", "total_spent", "created_at", "city"}
    if sort_by not in allowed_sort:
        sort_by = "name"
    reverse = sort_dir == "desc"
    clients.sort(key=lambda c: (not c["favorite"], c.get(sort_by) or ""), reverse=False)
    if sort_by != "name" or reverse:
        # secondary stable sort by requested field/direction, favorites still pinned first
        favorites = [c for c in clients if c["favorite"]]
        others = [c for c in clients if not c["favorite"]]
        key_fn = lambda c: (c.get(sort_by) if c.get(sort_by) is not None else "")
        favorites.sort(key=key_fn, reverse=reverse)
        others.sort(key=key_fn, reverse=reverse)
        clients = favorites + others

    return jsonify(to_json_safe(clients))


@app.route("/api/clients", methods=["POST"])
@login_required
def api_clients_create():
    data = request.get_json(force=True)
    phone = data.get("phone", "").strip()

    if _client_phone_exists(phone):
        return jsonify({"error": "duplicate_phone",
                         "message": "A client with this phone number already exists"}), 409

    seq = db.get_next_sequence("client")
    doc = {
        "code": f"C{seq:05d}",
        "name": data.get("name", "").strip(),
        "phone": phone,
        "email": data.get("email", "").strip(),
        "address": data.get("address", "").strip(),
        "city": data.get("city", "").strip(),
        "notes": data.get("notes", ""),
        "favorite": False,
        "created_at": datetime.utcnow(),
    }
    result = db.clients.insert_one(doc)
    doc["_id"] = result.inserted_id
    return jsonify(to_json_safe(doc)), 201


@app.route("/api/clients/<client_id>", methods=["PUT"])
@login_required
def api_clients_update(client_id):
    oid = parse_object_id(client_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    data = request.get_json(force=True)
    phone = data.get("phone", "").strip()

    if _client_phone_exists(phone, exclude_id=oid):
        return jsonify({"error": "duplicate_phone",
                         "message": "Another client with this phone number already exists"}), 409

    update = {
        "name": data.get("name", "").strip(),
        "phone": phone,
        "email": data.get("email", "").strip(),
        "address": data.get("address", "").strip(),
        "city": data.get("city", "").strip(),
        "notes": data.get("notes", ""),
    }
    db.clients.update_one({"_id": oid}, {"$set": update})
    return jsonify({"ok": True})


@app.route("/api/clients/<client_id>", methods=["DELETE"])
@login_required
def api_clients_delete(client_id):
    oid = parse_object_id(client_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    db.clients.delete_one({"_id": oid})
    return jsonify({"ok": True})


@app.route("/api/clients/<client_id>/favorite", methods=["POST"])
@login_required
def api_clients_toggle_favorite(client_id):
    oid = parse_object_id(client_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    client = db.clients.find_one({"_id": oid})
    if not client:
        return jsonify({"error": "not_found"}), 404
    new_val = not client.get("favorite", False)
    db.clients.update_one({"_id": oid}, {"$set": {"favorite": new_val}})
    return jsonify({"ok": True, "favorite": new_val})


@app.route("/api/clients/<client_id>/purchases", methods=["GET"])
@login_required
def api_clients_purchases(client_id):
    oid = parse_object_id(client_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    sales = list(db.sales.find({"client_id": oid}).sort("created_at", -1))
    total_spent = round(sum(s.get("total", 0) for s in sales), 2)
    return jsonify({
        "sales": to_json_safe(sales),
        "summary": {"total_orders": len(sales), "total_spent": total_spent},
    })


# ----------------------------------------------------------------------------
# API : SALES  (POS checkout)
# ----------------------------------------------------------------------------
@app.route("/api/sales", methods=["GET"])
@login_required
def api_sales_list():
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    query = {}
    if date_from or date_to:
        query["created_at"] = {}
        if date_from:
            query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:
            query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
    sales = list(db.sales.find(query).sort("created_at", -1).limit(500))
    return jsonify(to_json_safe(sales))


@app.route("/api/sales/<sale_id>", methods=["GET"])
@login_required
def api_sales_get(sale_id):
    oid = parse_object_id(sale_id)
    sale = db.sales.find_one({"_id": oid}) if oid else None
    if not sale:
        return jsonify({"error": "not_found"}), 404
    return jsonify(to_json_safe(sale))


@app.route("/api/sales/<sale_id>", methods=["PUT"])
@login_required
@roles_required("admin")
def api_sales_update(sale_id):
    """Admin-only: edit payment/discount/tax details of an existing sale and recalculate totals."""
    oid = parse_object_id(sale_id)
    sale = db.sales.find_one({"_id": oid}) if oid else None
    if not sale:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(force=True)

    subtotal = sale["subtotal"]
    total_cost = sale.get("total_cost", 0)
    discount = float(data.get("discount", sale.get("discount", 0)) or 0)
    tax_rate = float(data.get("tax_rate", sale.get("tax_rate", 0)) or 0)
    taxable = max(subtotal - discount, 0)
    tax = round(taxable * tax_rate / 100, 2)
    total = round(taxable + tax, 2)
    paid_amount = float(data.get("paid_amount", sale.get("paid_amount", total)) or total)
    change = round(paid_amount - total, 2)
    gross_profit = round(taxable - total_cost, 2)

    update = {
        "discount": discount,
        "tax_rate": tax_rate,
        "tax": tax,
        "total": total,
        "paid_amount": paid_amount,
        "change": change,
        "gross_profit": gross_profit,
        "payment_method": data.get("payment_method", sale.get("payment_method")),
        "client_name": data.get("client_name", sale.get("client_name")),
        "updated_at": datetime.utcnow(),
    }
    db.sales.update_one({"_id": oid}, {"$set": update})

    # keep the linked delivery record's subtotal/payment_method in sync, if any
    db.deliveries.update_one(
        {"sale_id": oid},
        {"$set": {"subtotal": total, "payment_method": update["payment_method"], "updated_at": datetime.utcnow()}}
    )
    return jsonify({"ok": True})


@app.route("/api/sales/<sale_id>", methods=["DELETE"])
@login_required
@roles_required("admin")
def api_sales_delete(sale_id):
    """Admin-only: delete a sale, restore stock for its items, and remove the linked delivery record."""
    oid = parse_object_id(sale_id)
    sale = db.sales.find_one({"_id": oid}) if oid else None
    if not sale:
        return jsonify({"error": "not_found"}), 404

    for line in sale.get("items", []):
        db.items.update_one({"_id": line["item_id"]}, {"$inc": {"stock": line["qty"]}})
    for line in sale.get("complimentary_items", []):
        db.items.update_one({"_id": line["item_id"]}, {"$inc": {"stock": line["qty"]}})

    restored_lines = sale.get("items", []) + sale.get("complimentary_items", [])
    if restored_lines:
        db.stock_movements.insert_many([{
            "item_id": line["item_id"], "item_name": line["name"], "type": "in",
            "qty": line["qty"], "reason": f"Sale {sale['invoice_no']} deleted - stock restored",
            "created_at": datetime.utcnow(), "user": session.get("username"),
        } for line in restored_lines])

    db.deliveries.delete_many({"sale_id": oid})
    db.sales.delete_one({"_id": oid})
    return jsonify({"ok": True})


@app.route("/api/sales", methods=["POST"])
@login_required
def api_sales_create():
    data = request.get_json(force=True)
    cart = data.get("items", [])
    if not cart:
        return jsonify({"error": "empty_cart"}), 400

    line_items = []
    subtotal = 0.0
    total_cost = 0.0
    for line in cart:
        oid = parse_object_id(line["item_id"])
        item = db.items.find_one({"_id": oid})
        if not item:
            return jsonify({"error": f"item_not_found:{line['item_id']}"}), 400
        qty = float(line.get("qty", 1))
        if item["stock"] < qty:
            return jsonify({"error": f"insufficient_stock:{item['name']}"}), 400
        # allow the cashier to override the unit price at checkout time
        price = float(line.get("price", item["price"]))
        cost = float(item.get("cost", 0) or 0)
        line_total = round(price * qty, 2)
        cost_subtotal = round(cost * qty, 2)
        subtotal += line_total
        total_cost += cost_subtotal
        line_items.append({
            "item_id": item["_id"], "name": item["name"], "sku": item.get("sku", ""),
            "qty": qty, "price": price, "subtotal": line_total,
            "cost": cost, "cost_subtotal": cost_subtotal,
        })

    # complimentary / free gift items: deduct stock only, excluded from revenue & profit
    comp_items = []
    for line in data.get("complimentary_items", []):
        oid = parse_object_id(line["item_id"])
        item = db.items.find_one({"_id": oid})
        if not item:
            continue
        qty = float(line.get("qty", 1))
        if item["stock"] < qty:
            return jsonify({"error": f"insufficient_stock:{item['name']} (complimentary)"}), 400
        comp_items.append({"item_id": item["_id"], "name": item["name"], "qty": qty})

    discount = float(data.get("discount", 0) or 0)
    tax_rate = float(data.get("tax_rate", 0) or 0)
    taxable = max(subtotal - discount, 0)
    tax = round(taxable * tax_rate / 100, 2)
    total = round(taxable + tax, 2)
    paid_amount = float(data.get("paid_amount", total) or total)
    change = round(paid_amount - total, 2)
    gross_profit = round(taxable - total_cost, 2)  # revenue after discount, before tax, minus cost of goods

    delivery_data = data.get("delivery") or {}
    delivery_fee_amount = float(delivery_data.get("fee_amount", 0) or 0)
    delivery_fee_currency = delivery_data.get("fee_currency", "USD")

    seq = db.get_next_sequence("invoice")
    sale_doc = {
        "invoice_no": invoice_number(seq),
        "client_id": parse_object_id(data.get("client_id")) if data.get("client_id") else None,
        "client_name": data.get("client_name", "Walk-in Customer"),
        "items": line_items,
        "complimentary_items": comp_items,
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "tax_rate": tax_rate,
        "tax": tax,
        "total": total,
        "total_cost": round(total_cost, 2),
        "gross_profit": gross_profit,
        "payment_method": data.get("payment_method", "cash"),
        "paid_amount": paid_amount,
        "change": change,
        "delivery_fee_amount": delivery_fee_amount,
        "delivery_fee_currency": delivery_fee_currency,
        "status": "completed",
        "cashier": session.get("name"),
        "created_at": datetime.utcnow(),
    }
    result = db.sales.insert_one(sale_doc)
    sale_doc["_id"] = result.inserted_id

    # decrement stock + log movements for paid items
    for line in line_items:
        db.items.update_one({"_id": line["item_id"]}, {"$inc": {"stock": -line["qty"]}})
        db.stock_movements.insert_one({
            "item_id": line["item_id"], "item_name": line["name"], "type": "out",
            "qty": line["qty"], "reason": f"Sale {sale_doc['invoice_no']}",
            "created_at": datetime.utcnow(), "user": session.get("username"),
        })

    # decrement stock for complimentary items (no revenue/profit impact)
    for line in comp_items:
        db.items.update_one({"_id": line["item_id"]}, {"$inc": {"stock": -line["qty"]}})
        db.stock_movements.insert_one({
            "item_id": line["item_id"], "item_name": line["name"], "type": "out",
            "qty": line["qty"], "reason": f"Complimentary gift - Sale {sale_doc['invoice_no']}",
            "created_at": datetime.utcnow(), "user": session.get("username"),
        })

    # optional delivery record
    if delivery_data:
        agent_id = parse_object_id(delivery_data.get("agent_id")) if delivery_data.get("agent_id") else None
        db.deliveries.insert_one({
            "sale_id": result.inserted_id,
            "invoice_no": sale_doc["invoice_no"],
            "client_name": sale_doc["client_name"],
            "phone": delivery_data.get("phone", ""),
            "address": delivery_data.get("address", ""),
            "agent_id": agent_id,
            "agent_name": delivery_data.get("agent_name", ""),
            "payment_method": sale_doc["payment_method"],
            "subtotal": total,
            "fee_amount": delivery_fee_amount,
            "fee_currency": delivery_fee_currency,
            "status": "pending",
            "notes": delivery_data.get("notes", ""),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        })

    return jsonify(to_json_safe(sale_doc)), 201


# ----------------------------------------------------------------------------
# API : DELIVERY TRACKING
# ----------------------------------------------------------------------------
@app.route("/api/deliveries", methods=["GET"])
@login_required
def api_deliveries_list():
    status = request.args.get("status")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    query = {}
    if status:
        query["status"] = status
    if date_from or date_to:
        query["created_at"] = {}
        if date_from:
            query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:
            query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
    rows = list(db.deliveries.find(query).sort("created_at", -1))
    return jsonify(to_json_safe(rows))


@app.route("/api/deliveries/counts", methods=["GET"])
@login_required
def api_deliveries_counts():
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    base_query = {}
    if date_from or date_to:
        base_query["created_at"] = {}
        if date_from:
            base_query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:
            base_query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)

    counts = {"all": db.deliveries.count_documents(base_query)}
    for s in ("pending", "shipping", "delivered", "cancelled"):
        q = dict(base_query)
        q["status"] = s
        counts[s] = db.deliveries.count_documents(q)
    return jsonify(counts)


@app.route("/api/deliveries/summary", methods=["GET"])
@login_required
def api_deliveries_summary():
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    query = {}
    if date_from or date_to:
        query["created_at"] = {}
        if date_from:
            query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:
            query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)

    rows = list(db.deliveries.find(query))
    total_usd = sum(r.get("fee_amount", 0) for r in rows if r.get("fee_currency", "USD") == "USD")
    total_khr = sum(r.get("fee_amount", 0) for r in rows if r.get("fee_currency") == "KHR")
    distinct_clients = len(set(r.get("client_name") for r in rows if r.get("client_name")))

    # breakdown by payment method: total money collected (product subtotal + USD delivery fee)
    by_payment = {}
    for r in rows:
        pm = r.get("payment_method") or "unknown"
        entry = by_payment.setdefault(pm, {"clients": set(), "total_usd": 0.0, "total_khr": 0.0})
        entry["clients"].add(r.get("client_name"))
        subtotal = r.get("subtotal", 0) or 0
        fee = r.get("fee_amount", 0) or 0
        if r.get("fee_currency", "USD") == "USD":
            entry["total_usd"] += subtotal + fee
        else:
            entry["total_usd"] += subtotal
            entry["total_khr"] += fee

    payment_summary = [
        {"method": k, "clients": len(v["clients"]),
         "total_usd": round(v["total_usd"], 2), "total_khr": round(v["total_khr"], 0)}
        for k, v in by_payment.items()
    ]

    return jsonify({
        "total_deliveries": len(rows),
        "total_clients": distinct_clients,
        "total_fee_usd": round(total_usd, 2),
        "total_fee_khr": round(total_khr, 0),
        "by_payment_method": payment_summary,
    })


@app.route("/api/deliveries/<delivery_id>", methods=["PUT"])
@login_required
def api_deliveries_update(delivery_id):
    oid = parse_object_id(delivery_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    data = request.get_json(force=True)
    update = {"updated_at": datetime.utcnow()}
    for field in ("status", "notes", "address", "phone", "agent_name", "fee_amount",
                  "fee_currency", "payment_method", "subtotal"):
        if field in data:
            update[field] = data[field]
    if "agent_id" in data:
        update["agent_id"] = parse_object_id(data["agent_id"]) if data["agent_id"] else None
    db.deliveries.update_one({"_id": oid}, {"$set": update})
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# API : DASHBOARD
# ----------------------------------------------------------------------------
@app.route("/api/dashboard/summary", methods=["GET"])
@login_required
def api_dashboard_summary():
    start, end = _date_range_args(default_today=True)

    period_sales = list(db.sales.find({"created_at": {"$gte": start, "$lt": end}}))
    period_revenue = sum(s["total"] for s in period_sales)

    period_cost = period_profit = 0.0
    for s in period_sales:
        c, p = _sale_cost_and_profit(s)
        period_cost += c
        period_profit += p
    margin_pct = round((period_profit / (period_revenue or 1) * 100), 1) if period_revenue else 0

    total_items = db.items.count_documents({})
    low_stock = list(db.items.find({"stock": {"$lte": LOW_STOCK_THRESHOLD}}).sort("stock", 1).limit(10))
    total_clients = db.clients.count_documents({})
    pending_deliveries = db.deliveries.count_documents({"status": "pending"})

    # last 6 months revenue trend (independent of the selected date range)
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    months = []
    for i in range(5, -1, -1):
        ref = today_start.replace(day=1) - timedelta(days=30 * i)
        months.append(ref.strftime("%Y-%m"))
    months = sorted(set(months))

    trend = []
    for m in months:
        year, mon = map(int, m.split("-"))
        mstart = datetime(year, mon, 1)
        mend = datetime(year + 1, 1, 1) if mon == 12 else datetime(year, mon + 1, 1)
        rev = sum(s["total"] for s in db.sales.find(
            {"created_at": {"$gte": mstart, "$lt": mend}}, {"total": 1}
        ))
        trend.append({"month": m, "revenue": rev})

    top_items = {}
    for s in db.sales.find({}, {"items": 1}).sort("created_at", -1).limit(300):
        for line in s.get("items", []):
            top_items[line["name"]] = top_items.get(line["name"], 0) + line["qty"]
    top_items_sorted = sorted(top_items.items(), key=lambda x: x[1], reverse=True)[:5]

    # client distribution by city / province
    city_stats = list(db.clients.aggregate([
        {"$group": {"_id": "$city", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]))
    city_data = [{"city": (c["_id"] or "Unknown"), "count": c["count"]} for c in city_stats]

    return jsonify({
        "period_revenue": round(period_revenue, 2),
        "period_orders": len(period_sales),
        "period_cost": round(period_cost, 2),
        "period_profit": round(period_profit, 2),
        "margin_pct": margin_pct,
        "total_items": total_items,
        "total_clients": total_clients,
        "pending_deliveries": pending_deliveries,
        "low_stock": to_json_safe(low_stock),
        "trend": trend,
        "top_items": [{"name": n, "qty": q} for n, q in top_items_sorted],
        "city_data": city_data,
    })


# ----------------------------------------------------------------------------
# API : REPORTS + EXPORT
# ----------------------------------------------------------------------------
@app.route("/api/reports/sales", methods=["GET"])
@login_required
def api_reports_sales():
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    query = {}
    if date_from or date_to:
        query["created_at"] = {}
        if date_from:
            query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:
            query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)

    sales = list(db.sales.find(query).sort("created_at", -1))
    total_revenue = sum(s["total"] for s in sales)
    total_orders = len(sales)
    total_items_sold = sum(sum(l["qty"] for l in s["items"]) for s in sales)

    return jsonify({
        "sales": to_json_safe(sales),
        "summary": {
            "total_revenue": round(total_revenue, 2),
            "total_orders": total_orders,
            "total_items_sold": total_items_sold,
        },
    })


@app.route("/api/reports/profit", methods=["GET"])
@login_required
def api_reports_profit():
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    query = {}
    if date_from or date_to:
        query["created_at"] = {}
        if date_from:
            query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:
            query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)

    sales = list(db.sales.find(query).sort("created_at", 1))

    daily = {}
    total_revenue = total_cost_all = total_profit = 0.0
    for s in sales:
        day = s["created_at"].strftime("%Y-%m-%d")
        cost, profit = _sale_cost_and_profit(s)
        revenue = round(s.get("subtotal", 0) - s.get("discount", 0), 2)  # net revenue excl. tax
        row = daily.setdefault(day, {"date": day, "revenue": 0.0, "cost": 0.0, "profit": 0.0, "orders": 0})
        row["revenue"] += revenue
        row["cost"] += cost
        row["profit"] += profit
        row["orders"] += 1
        total_revenue += revenue
        total_cost_all += cost
        total_profit += profit

    days = sorted(daily.values(), key=lambda r: r["date"])
    for d in days:
        d["revenue"] = round(d["revenue"], 2)
        d["cost"] = round(d["cost"], 2)
        d["profit"] = round(d["profit"], 2)
        d["margin_pct"] = round((d["profit"] / d["revenue"] * 100), 1) if d["revenue"] else 0

    margin_pct = round((total_profit / total_revenue * 100), 1) if total_revenue else 0

    return jsonify({
        "days": days,
        "summary": {
            "total_revenue": round(total_revenue, 2),
            "total_cost": round(total_cost_all, 2),
            "total_profit": round(total_profit, 2),
            "margin_pct": margin_pct,
            "total_orders": len(sales),
        },
    })


@app.route("/api/export/<kind>", methods=["GET"])
@login_required
def api_export(kind):
    fmt = request.args.get("format", "csv")
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    if kind == "items":
        headers = ["Name", "SKU", "Barcode", "Category", "Unit", "Price", "Cost", "Stock"]
        rows = [[i.get("name"), i.get("sku"), i.get("barcode"), i.get("category"),
                 i.get("unit"), i.get("price"), i.get("cost"), i.get("stock")]
                for i in db.items.find()]
        filename = "items_export"

    elif kind == "clients":
        headers = ["Code", "Name", "Phone", "City", "Email", "Address", "Purchases", "Total Spent"]
        counts = {str(c["_id"]): c for c in db.sales.aggregate([
            {"$match": {"client_id": {"$ne": None}}},
            {"$group": {"_id": "$client_id", "count": {"$sum": 1}, "total_spent": {"$sum": "$total"}}},
        ])}
        rows = []
        for c in db.clients.find():
            stat = counts.get(str(c["_id"]), {})
            rows.append([c.get("code"), c.get("name"), c.get("phone"), c.get("city", ""),
                         c.get("email"), c.get("address"), stat.get("count", 0),
                         round(stat.get("total_spent", 0), 2)])
        filename = "clients_export"

    elif kind == "sales":
        query = {}
        if date_from or date_to:
            query["created_at"] = {}
            if date_from:
                query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
            if date_to:
                query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        headers = ["Invoice", "Date", "Client", "Subtotal", "Discount", "Tax", "Total", "Payment", "Cashier"]
        rows = [[s["invoice_no"], s["created_at"].strftime("%Y-%m-%d %H:%M"), s["client_name"],
                 s["subtotal"], s["discount"], s["tax"], s["total"], s["payment_method"], s.get("cashier", "")]
                for s in db.sales.find(query).sort("created_at", -1)]
        filename = "sales_report"

    elif kind == "deliveries":
        query = {}
        if date_from or date_to:
            query["created_at"] = {}
            if date_from:
                query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
            if date_to:
                query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        headers = ["Invoice", "Client", "Phone", "Address", "Agent", "Fee", "Currency", "Status", "Date"]
        rows = [[d.get("invoice_no"), d.get("client_name"), d.get("phone"), d.get("address"),
                 d.get("agent_name", ""), d.get("fee_amount", 0), d.get("fee_currency", "USD"),
                 d.get("status"), d.get("created_at").strftime("%Y-%m-%d %H:%M")]
                for d in db.deliveries.find(query)]
        filename = "deliveries_export"

    elif kind == "profit":
        query = {}
        if date_from or date_to:
            query["created_at"] = {}
            if date_from:
                query["created_at"]["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
            if date_to:
                query["created_at"]["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        daily = {}
        for s in db.sales.find(query).sort("created_at", 1):
            day = s["created_at"].strftime("%Y-%m-%d")
            cost, profit = _sale_cost_and_profit(s)
            revenue = round(s.get("subtotal", 0) - s.get("discount", 0), 2)
            row = daily.setdefault(day, {"revenue": 0.0, "cost": 0.0, "profit": 0.0, "orders": 0})
            row["revenue"] += revenue
            row["cost"] += cost
            row["profit"] += profit
            row["orders"] += 1
        headers = ["Date", "Orders", "Revenue", "Cost", "Profit", "Margin %"]
        rows = []
        for day in sorted(daily.keys()):
            r = daily[day]
            margin = round((r["profit"] / r["revenue"] * 100), 1) if r["revenue"] else 0
            rows.append([day, r["orders"], round(r["revenue"], 2), round(r["cost"], 2), round(r["profit"], 2), margin])
        filename = "profit_loss_report"
    else:
        return jsonify({"error": "unknown_export_type"}), 400

    if fmt == "xlsx":
        buf = rows_to_xlsx_bytes(headers, rows, sheet_title=filename)
        return send_file(buf, as_attachment=True, download_name=f"{filename}.xlsx",
                          mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        buf = rows_to_csv_bytes(headers, rows)
        return send_file(buf, as_attachment=True, download_name=f"{filename}.csv", mimetype="text/csv")


# ----------------------------------------------------------------------------
# RECEIPT PDF
# ----------------------------------------------------------------------------
@app.route("/receipt/<sale_id>/pdf", methods=["GET"])
@login_required
def receipt_pdf(sale_id):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm

    oid = parse_object_id(sale_id)
    sale = db.sales.find_one({"_id": oid}) if oid else None
    if not sale:
        return "Sale not found", 404

    buf = io.BytesIO()
    width = 80 * mm
    height = (160 + len(sale["items"]) * 6 + len(sale.get("complimentary_items", [])) * 5) * mm
    c = canvas.Canvas(buf, pagesize=(width, height))

    y = height - 10 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(width / 2, y, "POS RECEIPT")
    y -= 6 * mm
    c.setFont("Helvetica", 8)
    c.drawCentredString(width / 2, y, f"Invoice: {sale['invoice_no']}")
    y -= 5 * mm
    c.drawCentredString(width / 2, y, sale["created_at"].strftime("%Y-%m-%d %H:%M"))
    y -= 5 * mm
    c.drawCentredString(width / 2, y, f"Client: {sale.get('client_name', 'Walk-in')}")
    y -= 6 * mm
    c.line(4 * mm, y, width - 4 * mm, y)
    y -= 5 * mm

    for line in sale["items"]:
        c.drawString(4 * mm, y, f"{line['name'][:18]}")
        y -= 4 * mm
        c.drawString(6 * mm, y, f"{line['qty']} x {line['price']:.2f} = {line['subtotal']:.2f}")
        y -= 5 * mm

    if sale.get("complimentary_items"):
        c.line(4 * mm, y, width - 4 * mm, y)
        y -= 5 * mm
        c.setFont("Helvetica-Bold", 8)
        c.drawString(4 * mm, y, "FREE / Complimentary:")
        y -= 4 * mm
        c.setFont("Helvetica", 8)
        for line in sale["complimentary_items"]:
            c.drawString(6 * mm, y, f"{line['name'][:18]} x {line['qty']}")
            y -= 4 * mm

    c.line(4 * mm, y, width - 4 * mm, y)
    y -= 5 * mm
    c.drawString(4 * mm, y, f"Subtotal: {sale['subtotal']:.2f}")
    y -= 4 * mm
    c.drawString(4 * mm, y, f"Discount: {sale['discount']:.2f}")
    y -= 4 * mm
    c.drawString(4 * mm, y, f"Tax: {sale['tax']:.2f}")
    y -= 5 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(4 * mm, y, f"TOTAL: {sale['total']:.2f}")
    y -= 6 * mm
    c.setFont("Helvetica", 8)
    c.drawString(4 * mm, y, f"Paid ({sale['payment_method']}): {sale['paid_amount']:.2f}")
    y -= 4 * mm
    c.drawString(4 * mm, y, f"Change: {sale['change']:.2f}")
    if sale.get("delivery_fee_amount"):
        y -= 4 * mm
        c.drawString(4 * mm, y, f"Delivery Fee: {sale['delivery_fee_amount']:.2f} {sale.get('delivery_fee_currency','USD')}")
        y -= 5 * mm
        c.setFont("Helvetica-Bold", 10)
        if sale.get("delivery_fee_currency", "USD") == "USD":
            c.drawString(4 * mm, y, f"GRAND TOTAL: {sale['total'] + sale['delivery_fee_amount']:.2f}")
        else:
            c.drawString(4 * mm, y, f"GRAND TOTAL: {sale['total']:.2f} USD + {sale['delivery_fee_amount']:.0f} KHR")
    y -= 8 * mm
    c.setFont("Helvetica-Oblique", 8)
    c.drawCentredString(width / 2, y, "Thank you! / សូមអរគុណ")

    c.save()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{sale['invoice_no']}.pdf", mimetype="application/pdf")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
