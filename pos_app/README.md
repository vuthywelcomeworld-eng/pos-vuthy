# POS Vuthy — Point of Sale Web Application

A ready-to-run Python (Flask) POS web application backed by MongoDB Atlas.

## Features
- Dashboard (today's revenue/orders, low-stock alerts, 6-month trend chart, top-selling items)
- Items management (add / edit / delete, SKU, barcode, price, cost, category)
- Stock management (stock in / stock out / set exact value, full movement history log)
- Point of Sale screen: search or **scan barcode/QR with your webcam**, cart, discount, tax, cash/card/QR payment, change calculation
- Client registration (auto-generated client code, search)
- Delivery tracking (create at checkout, update status: pending → shipping → delivered/cancelled)
- Printable receipts (browser print) + downloadable PDF receipt
- QR code label generator for any item (for printing barcode/QR stickers)
- Reporting: date-range sales report with totals, export to **CSV** and **Excel**
- Export items / clients / deliveries to CSV / Excel ("share to file")
- Simple login system (default admin account auto-created on first run)

## Tech stack
- **Backend:** Python 3 + Flask
- **Database:** MongoDB Atlas (via `pymongo`)
- **Frontend:** Bootstrap 5, Chart.js, html5-qrcode (for camera-based barcode/QR scanning) — loaded from CDN, no build step needed
- **PDF:** reportlab · **Excel:** openpyxl · **QR generation:** qrcode

## Database
Database name: `my_data_vuthy` (as requested), collections:

| Collection          | Purpose                              |
|----------------------|---------------------------------------|
| `items`              | Products / stock items                |
| `client_data001`     | Clients (kept exactly as you specified) |
| `sales`              | Sale / invoice transactions            |
| `deliveries`         | Delivery tracking                      |
| `stock_movements`    | Stock in/out/adjust audit log          |
| `users`              | Login accounts                         |
| `counters`           | Auto-increment sequences (invoice #, client code) |

## Setup

1. **Install Python 3.10+**, then install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure the database connection.**
   The app already contains your connection string in `db.py` as a fallback, so it will work out of the box. However, for security, it's strongly recommended to move real credentials out of source code:
   ```bash
   cp .env.example .env
   # edit .env and put your real MONGO_URI, then change SECRET_KEY to a random string
   ```
   > ⚠️ The connection string contains a real username/password. Anyone with the code can read it. Consider rotating the MongoDB Atlas password after testing, and always keep `.env` out of git (see `.gitignore` note below).

3. **Run the app:**
   ```bash
   python app.py
   ```
   Open your browser at **http://localhost:5000**

4. **Log in** with the default seeded account:
   - Username: `admin`
   - Password: `admin123`

   Change this password immediately in production (you can update it directly in the `users` collection, or ask me to add a "change password" page).

## Using the barcode/QR scanner
The scanner buttons (in POS and Items pages) use your device/laptop camera through the browser — no extra hardware or drivers needed. It works with most 1D barcodes (EAN, UPC, Code128) and QR codes. Camera access requires **https** or **localhost** — browsers block camera access on plain http from a network IP, so for real deployment put the app behind HTTPS (e.g. via a reverse proxy like Nginx + Let's Encrypt, or a platform such as Render/Railway that provides HTTPS automatically).

## Deploying for real use
- For a shared/multi-cashier setup, deploy on a server (Render, Railway, a VPS, etc.) and access from any device's browser.
- Put `MONGO_URI` and `SECRET_KEY` as environment variables on the host instead of the fallback in `db.py`.
- Consider adding a proper `.gitignore` with `.env` excluded before pushing to any git repository.

## Folder structure
```
pos_app/
├── app.py                 # Flask routes (pages + JSON API)
├── db.py                  # MongoDB connection
├── utils.py                # Helpers: auth guard, JSON serializer, CSV/XLSX export
├── requirements.txt
├── .env.example
├── static/
│   └── css/style.css
└── templates/
    ├── base.html           # Sidebar layout
    ├── login.html
    ├── dashboard.html
    ├── items.html
    ├── stock.html
    ├── pos.html            # Main sales / checkout screen
    ├── clients.html
    ├── delivery.html
    ├── reports.html
    └── receipt.html        # Printable receipt
```

## Notes / things you may want to extend
- Multi-user roles are scaffolded (`role` field on users, `roles_required` decorator in `utils.py`) but only "admin" is seeded — add a user-management page if you need multiple cashiers with different permissions.
- Barcode **generation** (e.g. Code128 labels for new items) isn't wired into the UI yet — only QR label generation is. The `python-barcode` package is already in `requirements.txt` if you want to add that.
- Currency is shown as a plain number (no currency symbol) so you can adapt it to USD/KHR as needed — search for `money(` in the templates to adjust formatting.
