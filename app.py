import os
import io
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


# ----------------------------------------------------------------------------
# Bootstrap a default admin account on first run
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


seed_admin()


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


@app.route("/receipt/<sale_id>")
@login_required
def receipt_page(sale_id):
    oid = parse_object_id(sale_id)
    sale = db.sales.find_one({"_id": oid}) if oid else None
    if not sale:
        return "Sale not found", 404
    return render_template("receipt.html", sale=to_json_safe(sale))


# ----------------------------------------------------------------------------
# API : ITEMS
# ----------------------------------------------------------------------------
@app.route("/api/items", methods=["GET"])
@login_required
def api_items_list():
    q = request.args.get("q", "").strip()
    query = {}
    if q:
        query = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"sku": {"$regex": q, "$options": "i"}},
            {"barcode": {"$regex": q, "$options": "i"}},
            {"category": {"$regex": q, "$options": "i"}},
        ]}
    items = list(db.items.find(query).sort("name", 1))
    return jsonify(to_json_safe(items))


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
    doc = {
        "name": data.get("name", "").strip(),
        "sku": data.get("sku", "").strip(),
        "barcode": data.get("barcode", "").strip(),
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
    update = {
        "name": data.get("name", "").strip(),
        "sku": data.get("sku", "").strip(),
        "barcode": data.get("barcode", "").strip(),
        "category": data.get("category", "").strip() or "General",
        "unit": data.get("unit", "pcs"),
        "price": float(data.get("price", 0) or 0),
        "cost": float(data.get("cost", 0) or 0),
        "updated_at": datetime.utcnow(),
    }
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
    query = {}
    if q:
        query = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
            {"code": {"$regex": q, "$options": "i"}},
        ]}
    clients = list(db.clients.find(query).sort("name", 1))
    return jsonify(to_json_safe(clients))


@app.route("/api/clients", methods=["POST"])
@login_required
def api_clients_create():
    data = request.get_json(force=True)
    seq = db.get_next_sequence("client")
    doc = {
        "code": f"C{seq:05d}",
        "name": data.get("name", "").strip(),
        "phone": data.get("phone", "").strip(),
        "email": data.get("email", "").strip(),
        "address": data.get("address", "").strip(),
        "notes": data.get("notes", ""),
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
    update = {
        "name": data.get("name", "").strip(),
        "phone": data.get("phone", "").strip(),
        "email": data.get("email", "").strip(),
        "address": data.get("address", "").strip(),
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


@app.route("/api/sales", methods=["POST"])
@login_required
def api_sales_create():
    data = request.get_json(force=True)
    cart = data.get("items", [])
    if not cart:
        return jsonify({"error": "empty_cart"}), 400

    line_items = []
    subtotal = 0.0
    for line in cart:
        oid = parse_object_id(line["item_id"])
        item = db.items.find_one({"_id": oid})
        if not item:
            return jsonify({"error": f"item_not_found:{line['item_id']}"}), 400
        qty = float(line.get("qty", 1))
        if item["stock"] < qty:
            return jsonify({"error": f"insufficient_stock:{item['name']}"}), 400
        price = float(line.get("price", item["price"]))
        line_total = round(price * qty, 2)
        subtotal += line_total
        line_items.append({
            "item_id": item["_id"], "name": item["name"], "sku": item.get("sku", ""),
            "qty": qty, "price": price, "subtotal": line_total,
        })

    discount = float(data.get("discount", 0) or 0)
    tax_rate = float(data.get("tax_rate", 0) or 0)
    taxable = max(subtotal - discount, 0)
    tax = round(taxable * tax_rate / 100, 2)
    total = round(taxable + tax, 2)
    paid_amount = float(data.get("paid_amount", total) or total)
    change = round(paid_amount - total, 2)

    seq = db.get_next_sequence("invoice")
    sale_doc = {
        "invoice_no": invoice_number(seq),
        "client_id": parse_object_id(data.get("client_id")) if data.get("client_id") else None,
        "client_name": data.get("client_name", "Walk-in Customer"),
        "items": line_items,
        "subtotal": round(subtotal, 2),
        "discount": discount,
        "tax_rate": tax_rate,
        "tax": tax,
        "total": total,
        "payment_method": data.get("payment_method", "cash"),
        "paid_amount": paid_amount,
        "change": change,
        "status": "completed",
        "cashier": session.get("name"),
        "created_at": datetime.utcnow(),
    }
    result = db.sales.insert_one(sale_doc)
    sale_doc["_id"] = result.inserted_id

    # decrement stock + log movements
    for line in line_items:
        db.items.update_one({"_id": line["item_id"]}, {"$inc": {"stock": -line["qty"]}})
        db.stock_movements.insert_one({
            "item_id": line["item_id"], "item_name": line["name"], "type": "out",
            "qty": line["qty"], "reason": f"Sale {sale_doc['invoice_no']}",
            "created_at": datetime.utcnow(), "user": session.get("username"),
        })

    # optional delivery record
    if data.get("delivery"):
        d = data["delivery"]
        db.deliveries.insert_one({
            "sale_id": result.inserted_id,
            "invoice_no": sale_doc["invoice_no"],
            "client_name": sale_doc["client_name"],
            "phone": d.get("phone", ""),
            "address": d.get("address", ""),
            "courier": d.get("courier", ""),
            "status": "pending",
            "notes": d.get("notes", ""),
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
    query = {"status": status} if status else {}
    rows = list(db.deliveries.find(query).sort("created_at", -1))
    return jsonify(to_json_safe(rows))


@app.route("/api/deliveries/<delivery_id>", methods=["PUT"])
@login_required
def api_deliveries_update(delivery_id):
    oid = parse_object_id(delivery_id)
    if not oid:
        return jsonify({"error": "invalid_id"}), 400
    data = request.get_json(force=True)
    update = {"updated_at": datetime.utcnow()}
    for field in ("status", "courier", "notes", "address", "phone"):
        if field in data:
            update[field] = data[field]
    db.deliveries.update_one({"_id": oid}, {"$set": update})
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# API : DASHBOARD
# ----------------------------------------------------------------------------
@app.route("/api/dashboard/summary", methods=["GET"])
@login_required
def api_dashboard_summary():
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    today_sales = list(db.sales.find({"created_at": {"$gte": today_start}}))
    today_revenue = sum(s["total"] for s in today_sales)

    total_items = db.items.count_documents({})
    low_stock = list(db.items.find({"stock": {"$lte": LOW_STOCK_THRESHOLD}}).sort("stock", 1).limit(10))
    total_clients = db.clients.count_documents({})
    pending_deliveries = db.deliveries.count_documents({"status": "pending"})

    # last 6 months revenue trend
    months = []
    for i in range(5, -1, -1):
        ref = today_start.replace(day=1) - timedelta(days=30 * i)
        months.append(ref.strftime("%Y-%m"))
    months = sorted(set(months))

    trend = []
    for m in months:
        year, mon = map(int, m.split("-"))
        start = datetime(year, mon, 1)
        end = datetime(year + 1, 1, 1) if mon == 12 else datetime(year, mon + 1, 1)
        rev = sum(s["total"] for s in db.sales.find(
            {"created_at": {"$gte": start, "$lt": end}}, {"total": 1}
        ))
        trend.append({"month": m, "revenue": rev})

    top_items = {}
    for s in db.sales.find({}, {"items": 1}).sort("created_at", -1).limit(300):
        for line in s.get("items", []):
            top_items[line["name"]] = top_items.get(line["name"], 0) + line["qty"]
    top_items_sorted = sorted(top_items.items(), key=lambda x: x[1], reverse=True)[:5]

    return jsonify({
        "today_revenue": round(today_revenue, 2),
        "today_orders": len(today_sales),
        "total_items": total_items,
        "total_clients": total_clients,
        "pending_deliveries": pending_deliveries,
        "low_stock": to_json_safe(low_stock),
        "trend": trend,
        "top_items": [{"name": n, "qty": q} for n, q in top_items_sorted],
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
        headers = ["Code", "Name", "Phone", "Email", "Address"]
        rows = [[c.get("code"), c.get("name"), c.get("phone"), c.get("email"), c.get("address")]
                for c in db.clients.find()]
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
        headers = ["Invoice", "Client", "Phone", "Address", "Courier", "Status", "Date"]
        rows = [[d.get("invoice_no"), d.get("client_name"), d.get("phone"), d.get("address"),
                 d.get("courier"), d.get("status"), d.get("created_at").strftime("%Y-%m-%d %H:%M")]
                for d in db.deliveries.find()]
        filename = "deliveries_export"
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
    height = (150 + len(sale["items"]) * 6) * mm
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
