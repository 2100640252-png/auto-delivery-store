#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-service ordering + auto-delivery store (Flask + SQLite).
No hardcoded passwords/secrets/merchant credentials.
Payment verification is DISABLED by default: orders are '待核实', never '付款成功'.
"""
import os
import csv
import hashlib
import io
import random
import sqlite3
import secrets
import string
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from flask import Flask, request, redirect, url_for, session, send_from_directory, send_file, jsonify, abort, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FILES_DIR = DATA_DIR / "files"
QR_DIR = DATA_DIR / "qr"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
DB_PATH = DATA_DIR / "store.db"
SECRET_FILE = DATA_DIR / "secret.key"
for d in (DATA_DIR, FILES_DIR, QR_DIR, SCREENSHOTS_DIR):
	d.mkdir(parents=True, exist_ok=True)
def _secret_key():
	env = os.environ.get("SECRET_KEY")
	if env:
		return env
	if SECRET_FILE.exists():
		return SECRET_FILE.read_text().strip()
	key = secrets.token_hex(32)
	SECRET_FILE.write_text(key)
	try:
		os.chmod(SECRET_FILE, 0o600)
	except Exception:
		pass
	return key
app = Flask(__name__)
app.secret_key = _secret_key()
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
PAY_METHODS = {"wechat": "微信", "alipay": "支付宝", "paypal": "PayPal"}
RUSH_FEE = 30
# 自建监听（手机到账通知监控）配置
MONITOR_MATCH_WINDOW_MINUTES = 60  # 到账通知只匹配最近60分钟内创建的待支付订单
MONITOR_TYPE_MAP = {"1": "wechat", "2": "alipay"}  # V免签App的type：1微信 / 2支付宝 / 4数字人民币（暂不支持）
def get_db():
	conn = sqlite3.connect(str(DB_PATH))
	conn.row_factory = sqlite3.Row
	return conn
def init_db():
	conn = get_db()
	cur = conn.cursor()
	cur.executescript("""
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price_yuan INTEGER NOT NULL, description TEXT DEFAULT '', delivery_type TEXT DEFAULT '数字文件', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS codes(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, code TEXT NOT NULL, used INTEGER DEFAULT 0, order_no TEXT DEFAULT '', created_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS delivery_files(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, filename TEXT NOT NULL, stored_name TEXT NOT NULL, created_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT UNIQUE NOT NULL, product_id INTEGER, product_name TEXT, amount_yuan INTEGER, rush INTEGER DEFAULT 0, pay_method TEXT, customer_name TEXT, contact TEXT, pay_time TEXT DEFAULT '', txn_last4 TEXT DEFAULT '', screenshot_name TEXT DEFAULT '', status TEXT DEFAULT '待核实', delivery_code TEXT DEFAULT '', delivery_file TEXT DEFAULT '', created_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS payment_config(method TEXT PRIMARY KEY, qr_file TEXT DEFAULT '', address TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS pay_notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT DEFAULT '', amount_cents INTEGER DEFAULT 0, price_str TEXT DEFAULT '', pid TEXT DEFAULT '', notify_time TEXT DEFAULT '', payer TEXT DEFAULT '', raw TEXT DEFAULT '', matched_order_no TEXT DEFAULT '', received_at TEXT DEFAULT '');
""")
	conn.commit()
	_migrate(conn)
	n = cur.execute("SELECT COUNT(*) FROM products").fetchone()[0]
	if n == 0:
		seed(cur, conn)
	conn.close()
def _setting(cur, key, default=""):
	row = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
	return row[0] if row else default
def get_setting(key, default=""):
	conn = get_db()
	v = _setting(conn.cursor(), key, default)
	conn.close()
	return v
def set_setting(key, value):
	conn = get_db()
	conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
	conn.commit()
	conn.close()
def seed(cur, conn):
	now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('shop_name','Muse AI 影像商城')")
	cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('announcement','自助下单 · 下单后按订单页显示的精确金额付款（带零头，用于自动识别到账），手机监听到账后自动发货；也可付完后在订单页提交付款凭证')")
	cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('admin_password_hash','')")
	cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('pay_verify_enabled','0')")
	cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('pay_verify_mode','disabled')")
	products = [
		("AI写真", 29, "AI写真 6张 · 写实真人风 · 24小时内交付 · 免费改1次", "数字文件"),
		("跳舞视频", 49, "跳舞视频 10秒 · 写实真人风 · 24小时内交付 · 免费改1次", "数字视频"),
		("短剧", 99, "短剧 10-15秒 · 写实真人风 · 24-48小时交付 · 免费改1次", "数字视频"),
		("套餐", 69, "写真 + 跳舞视频套餐 · 写实真人风 · 24小时内交付", "数字文件"),
	]
	pids = []
	for name, price, desc, dtype in products:
		cur.execute("INSERT INTO products(name,price_yuan,description,delivery_type) VALUES(?,?,?,?)", (name, price, desc, dtype))
		pids.append(cur.lastrowid)
	for m in PAY_METHODS:
		cur.execute("INSERT OR IGNORE INTO payment_config(method,qr_file,address) VALUES(?,?,?)", (m, "", ""))
	s1 = FILES_DIR / "ai-photo-sample.txt"
	if not s1.exists():
		s1.write_text("AI写真订单示例\n这是种子示例文件，店主请在后台为每个商品上传真实发货文件。\n", encoding="utf-8")
	s4 = FILES_DIR / "bundle-sample.txt"
	if not s4.exists():
		s4.write_text("套餐订单示例\n写真 + 跳舞视频套餐示例文件，店主请在后台替换为真实发货文件。\n", encoding="utf-8")
	cur.execute("INSERT INTO delivery_files(product_id,filename,stored_name,created_at) VALUES(?,?,?,?)", (pids[0], "ai-photo-sample.txt", "ai-photo-sample.txt", now))
	cur.execute("INSERT INTO delivery_files(product_id,filename,stored_name,created_at) VALUES(?,?,?,?)", (pids[3], "bundle-sample.txt", "bundle-sample.txt", now))
	for code in ("MUSE-PHOTO-A1B2", "MUSE-PHOTO-C3D4"):
		cur.execute("INSERT INTO codes(product_id,code,created_at) VALUES(?,?,?)", (pids[0], code, now))
	cur.execute("INSERT INTO codes(product_id,code,created_at) VALUES(?,?,?)", (pids[1], "MUSE-DANCE-E5F6", now))
	conn.commit()
def _migrate(conn):
	cur = conn.cursor()
	cols = [r[1] for r in cur.execute("PRAGMA table_info(orders)").fetchall()]
	for col, ddl in (("pay_amount_cents", "INTEGER DEFAULT 0"), ("paid_at", "TEXT DEFAULT ''"), ("pay_note", "TEXT DEFAULT ''")):
		if col not in cols:
			cur.execute("ALTER TABLE orders ADD COLUMN " + col + " " + ddl)
	cur.execute("UPDATE orders SET pay_amount_cents = amount_yuan * 100 WHERE pay_amount_cents = 0 OR pay_amount_cents IS NULL")
	conn.commit()
def _fmt_cents(cents):
	try:
		return "%.2f" % (int(cents) / 100)
	except Exception:
		return "0.00"
def _now_str():
	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def _md5(s):
	return hashlib.md5(s.encode("utf-8")).hexdigest()
def get_notify_key():
	key = get_setting("notify_key", "")
	if not key:
		key = secrets.token_hex(16)
		set_setting("notify_key", key)
	return key
def _unique_pay_amount_cents(conn, base_cents):
	"""给待支付订单分配一个带零头的应付金额（分），60分钟内的待支付订单不重样，用于到账通知按金额匹配订单。"""
	since = (datetime.now() - timedelta(minutes=MONITOR_MATCH_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
	taken = {r[0] for r in conn.execute("SELECT pay_amount_cents FROM orders WHERE status='待支付' AND created_at>=? AND pay_amount_cents>0", (since,)).fetchall()}
	for _ in range(50):
		cents = base_cents + random.randint(1, 99)
		if cents not in taken:
			return cents
	return base_cents
def _assign_delivery(cur, pid, order_no):
	"""取一份发货文件（最新）+ 一个未用兑换码（先取先得并标记已用），返回 (delivery_file, delivery_code)。"""
	frow = cur.execute("SELECT * FROM delivery_files WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
	crow = cur.execute("SELECT * FROM codes WHERE product_id=? AND used=0 ORDER BY id LIMIT 1", (pid,)).fetchone()
	delivery_file = frow["stored_name"] if frow else ""
	delivery_code = ""
	if crow:
		delivery_code = crow["code"]
		cur.execute("UPDATE codes SET used=1, order_no=? WHERE id=?", (order_no, crow["id"]))
	return delivery_file, delivery_code
def gen_order_no():
	return "PO" + datetime.now().strftime("%Y%m%d%H%M%S") + "".join(secrets.choice(string.digits) for _ in range(4))
def admin_logged_in():
	return bool(session.get("admin_logged_in"))
BASE_CSS = "<style>body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;max-width:900px;margin:0 auto;padding:20px;color:#222}a{color:#0b6efa}.card{border:1px solid #ddd;border-radius:10px;padding:16px;margin:12px 0}.warn{background:#fff4d6;border:1px solid #e8b400;border-radius:8px;padding:12px;margin:12px 0}.okbox{background:#eef7ee;border:1px solid #7fbf7f;border-radius:8px;padding:12px;margin:12px 0}input,select,textarea{padding:8px;margin:4px 0;width:100%;box-sizing:border-box}button{padding:10px 18px;background:#0b6efa;color:#fff;border:0;border-radius:8px;cursor:pointer}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;text-align:left}.small{color:#666;font-size:13px}</style>"
def page(title, body):
	shop = get_setting("shop_name", "Muse AI 影像商城")
	ann = get_setting("announcement", "")
	html = "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>" + title + " - " + shop + "</title>" + BASE_CSS + "</head><body>"
	html += "<h1><a href=\"/\" style=\"color:#222;text-decoration:none\">" + shop + "</a></h1><p class=\"small\">" + ann + "</p><hr><h2>" + title + "</h2>"
	html += body
	html += "<p class=\"small\"><a href=\"/\">返回首页</a> · 商家邮箱：2100640252@qq.com</p></body></html>"
	return html
@app.route("/")
def index():
	conn = get_db()
	products = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
	conn.close()
	cards = ""
	for p in products:
		cards += "<div class=\"card\"><h3>" + p["name"] + " — ¥" + str(p["price_yuan"]) + "</h3><p>" + p["description"] + "</p><p class=\"small\">交付方式：" + p["delivery_type"] + " · 加急 +¥" + str(RUSH_FEE) + "</p><a href=\"/product/" + str(p["id"]) + "\"><button>查看 / 下单</button></a></div>"
	body = "<div class=\"warn\"><b>自助下单说明：</b>下单后请按页面提示扫码付款，再提交付款凭证，系统会立即自动发货（给下载链接和/或兑换码）。订单状态为“待核实”，<b>不是“付款成功”</b>——本站未接通微信/支付宝/PayPal 的实时到账核验，商家需事后在收款后台核对到账后再确认订单。</div>" + (cards or "<p>暂无商品</p>")
	return page("商品列表", body)
@app.route("/product/<int:pid>")
def product_detail(pid):
	conn = get_db()
	p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
	pay = {r["method"]: dict(r) for r in conn.execute("SELECT * FROM payment_config").fetchall()}
	conn.close()
	if not p:
		abort(404)
	opts = ""
	for m, label in PAY_METHODS.items():
		c = pay.get(m, {})
		qr = "<br>收款码：<img src=\"/qr/" + c.get("qr_file", "") + "\" style=\"max-width:200px\">" if c.get("qr_file") else "<br>收款码：待商家上传"
		addr = c.get("address") or "待商家填写"
		chk = "checked" if m == "wechat" else ""
		opts += "<label><input type=\"radio\" name=\"pay_method\" value=\"" + m + "\" " + chk + "> " + label + qr + "<br><span class=\"small\">收款地址：" + addr + "</span></label><br>"
	body = "<div class=\"card\"><h3>" + p["name"] + " — ¥" + str(p["price_yuan"]) + "</h3><p>" + p["description"] + "</p><p class=\"small\">标准价 ¥" + str(p["price_yuan"]) + " · 加急 +¥" + str(RUSH_FEE) + "（总价 ¥" + str(p["price_yuan"] + RUSH_FEE) + "）</p></div>"
	body += "<form method=\"post\" action=\"/order\"><input type=\"hidden\" name=\"product_id\" value=\"" + str(p["id"]) + "\">"
	body += "<label><input type=\"checkbox\" name=\"rush\" value=\"1\"> 加急（+¥" + str(RUSH_FEE) + "）</label><br><br>"
	body += "<b>付款方式：</b><br>" + opts + "<br>"
	body += "<label>称呼 *<input name=\"customer_name\" required></label>"
	body += "<label>联系方式（微信/QQ/邮箱）*<input name=\"contact\" required></label>"
	body += "<div class=\"warn\">下单后，订单页会显示一个<b>带零头的精确付款金额</b>（如 ¥29.13，零头是订单识别码，请按这个金额付款，不要改）。微信/支付宝到账后，手机监听会自动识别并立即发货；也可以付完后在订单页提交付款凭证，商家核对后发货。</div>"
	body += "<button type=\"submit\">创建订单并显示付款金额</button></form>"
	return page(p["name"], body)
@app.route("/order", methods=["POST"])
def create_order():
	try:
		pid = int(request.form.get("product_id", "0"))
	except ValueError:
		abort(400)
	rush = 1 if request.form.get("rush") == "1" else 0
	pay_method = request.form.get("pay_method", "wechat")
	if pay_method not in PAY_METHODS:
		pay_method = "wechat"
	name = (request.form.get("customer_name") or "").strip()
	contact = (request.form.get("contact") or "").strip()
	if not name or not contact:
		return page("下单失败", "<p>称呼和联系方式为必填项，请返回重新填写。</p>"), 400
	conn = get_db()
	cur = conn.cursor()
	p = cur.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
	if not p:
		conn.close()
		abort(404)
	amount = p["price_yuan"] + (RUSH_FEE if rush else 0)
	order_no = gen_order_no()
	now = _now_str()
	# 监听模式：微信/支付宝下单先不发货，分配一个带零头的唯一应付金额，等手机监听到账通知匹配到账后再自动发货。
	# PayPal 没有手机监听，走同样的待支付订单，客户付完后在订单页提交付款凭证（见 order_proof）。
	pay_cents = _unique_pay_amount_cents(cur, amount * 100) if pay_method in ("wechat", "alipay") else amount * 100
	cur.execute("INSERT INTO orders(order_no,product_id,product_name,amount_yuan,rush,pay_method,customer_name,contact,pay_time,txn_last4,screenshot_name,status,delivery_code,delivery_file,created_at,pay_amount_cents,paid_at,pay_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (order_no, pid, p["name"], amount, rush, pay_method, name, contact, "", "", "", "待支付", "", "", now, pay_cents, "", ""))
	conn.commit()
	conn.close()
	return redirect(url_for("order_result", order_no=order_no))
@app.route("/order/<order_no>")
def order_result(order_no):
	conn = get_db()
	o = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
	pay = conn.execute("SELECT * FROM payment_config WHERE method=?", (o["pay_method"],)).fetchone() if o else None
	conn.close()
	if not o:
		abort(404)
	pay_amount = _fmt_cents(o["pay_amount_cents"] or o["amount_yuan"] * 100)
	head = "订单号：<b>" + o["order_no"] + "</b><br>商品：" + o["product_name"] + " · 付款方式：" + PAY_METHODS.get(o["pay_method"], "")
	if o["status"] == "待支付":
		qr = ""
		addr = ""
		if pay:
			if pay["qr_file"]:
				qr = "<br><img src=\"/qr/" + pay["qr_file"] + "\" style=\"max-width:220px\">"
			if pay["address"]:
				addr = "<br>收款地址：" + pay["address"]
		note = "请扫码按下面金额付款，到账后系统自动识别并立即发货（本页每10秒自动刷新一次）。"
		if o["pay_method"] == "paypal":
			note = "请按下面金额付款，付完后点下方“提交付款凭证”，商家核对后发货。"
		body = "<div class=\"warn\"><b>等待付款</b><br>" + head + "<br>请付：<b>¥" + pay_amount + "</b>（带零头，是订单识别码，请按这个金额付款，不要改）" + qr + addr + "</div>"
		body += "<p>" + note + "</p>"
		body += "<p><a href=\"/order/" + o["order_no"] + "/proof\"><button>我已付款，提交付款凭证</button></a></p>"
		body += "<p>订单状态：<b>待支付</b> · 下单时间：" + o["created_at"] + "</p>"
		body += "<script>setInterval(function(){fetch('/api/order/" + o["order_no"] + "/status').then(function(r){return r.json()}).then(function(j){if(j.paid || j.status != '待支付'){location.reload()}})},10000)</script>"
	elif o["status"] == "缺货待处理":
		body = "<div class=\"warn\"><b>订单已提交 · 缺货待处理</b><br>" + head + "<br>该商品当前无发货库存，本单未发货。请联系商家（2100640252@qq.com）处理，或等待商家补货后联系你。</div><p>订单状态：<b>" + o["status"] + "</b>（不是付款成功）</p>"
	else:
		dl = "<p>下载链接：<a href=\"/download/" + o["order_no"] + "\">点击下载发货文件</a></p>" if o["delivery_file"] else ""
		code = "<p>兑换码（一次性，请妥善保存）：<b>" + o["delivery_code"] + "</b></p>" if o["delivery_code"] else ""
		if o["status"] == "已付款":
			title = "已确认到账 · 自动发货"
			note = "手机监听已识别到账（" + (o["paid_at"] or "") + "），发货内容如下。"
		else:
			title = "订单已提交 · 等待商家确认收款"
			note = "<b>这不是“付款成功”。</b>本站未接通实时到账核验，系统已按你提交的凭证自动发货，商家需事后在收款后台核对到账，核对无误后订单才算确认。如未到账，商家会联系你。"
		body = "<div class=\"okbox\"><b>" + title + "</b><br>" + head + "<br>实付金额：¥" + pay_amount + "</div>"
		body += "<div class=\"warn\">" + note + "</div>"
		body += dl + code
		body += "<p>订单状态：<b>" + o["status"] + "</b> · 下单时间：" + o["created_at"] + "</p><p class=\"small\">请保存好订单号，如需帮助请联系商家：2100640252@qq.com</p>"
	return page("订单结果", body)
@app.route("/download/<order_no>")
def download(order_no):
	conn = get_db()
	o = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
	conn.close()
	if not o or not o["delivery_file"]:
		abort(404)
	return send_from_directory(str(FILES_DIR), o["delivery_file"], as_attachment=True)
@app.route("/qr/<filename>")
def serve_qr(filename):
	return send_from_directory(str(QR_DIR), secure_filename(filename))
@app.route("/admin/screenshot/<filename>")
def serve_screenshot(filename):
	if not admin_logged_in():
		return redirect(url_for("admin"))
	return send_from_directory(str(SCREENSHOTS_DIR), secure_filename(filename))
@app.route("/order/<order_no>/proof", methods=["GET", "POST"])
def order_proof(order_no):
	"""付款凭证兜底：监听没识别到账时，客户付完后在这里提交付款时间、单号后4位和截图，商家核对后发货。"""
	conn = get_db()
	o = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
	conn.close()
	if not o:
		abort(404)
	if o["status"] != "待支付":
		return redirect(url_for("order_result", order_no=order_no))
	if request.method == "GET":
		body = "<div class=\"card\"><h3>订单 " + o["order_no"] + " · " + o["product_name"] + " · 应付 ¥" + _fmt_cents(o["pay_amount_cents"] or o["amount_yuan"] * 100) + "</h3>"
		body += "<form method=\"post\" enctype=\"multipart/form-data\">"
		body += "<label>付款时间 *<input name=\"pay_time\" required placeholder=\"如 2026-11-15 01:30\"></label>"
		body += "<label>交易单号后4位 *<input name=\"txn_last4\" required maxlength=\"4\" placeholder=\"如 1234\"></label>"
		body += "<label>上传付款截图（可选，图片）<input type=\"file\" name=\"screenshot\" accept=\"image/*\"></label>"
		body += "<div class=\"warn\">提交后系统立即自动发货，订单状态为“待核实”，<b>不是付款成功</b>。商家会事后核对到账。</div>"
		body += "<button type=\"submit\">提交凭证并自动发货</button></form></div>"
		return page("提交付款凭证", body)
	pay_time = (request.form.get("pay_time") or "").strip()
	txn_last4 = (request.form.get("txn_last4") or "").strip()
	if not pay_time or not txn_last4:
		return page("提交失败", "<p>付款时间和交易单号后4位是必填项，请返回重新填写。</p>"), 400
	shot = request.files.get("screenshot")
	screenshot_stored = ""
	if shot and shot.filename:
		safe = secure_filename(shot.filename)
		if safe:
			screenshot_stored = order_no + "_" + safe
			shot.save(str(SCREENSHOTS_DIR / screenshot_stored))
	conn = get_db()
	cur = conn.cursor()
	o2 = cur.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
	if not o2 or o2["status"] != "待支付":
		conn.close()
		return redirect(url_for("order_result", order_no=order_no))
	dfile, dcode = _assign_delivery(cur, o2["product_id"], order_no)
	status = "待核实" if (dfile or dcode) else "缺货待处理"
	cur.execute("UPDATE orders SET pay_time=?, txn_last4=?, screenshot_name=?, status=?, delivery_file=?, delivery_code=? WHERE order_no=?", (pay_time, txn_last4, screenshot_stored, status, dfile, dcode, order_no))
	conn.commit()
	conn.close()
	return redirect(url_for("order_result", order_no=order_no))
@app.route("/api/order/<order_no>/status")
def order_status(order_no):
	conn = get_db()
	o = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
	conn.close()
	if not o:
		abort(404)
	return jsonify({"order_no": o["order_no"], "status": o["status"], "paid": o["status"] == "已付款", "delivery_code": o["delivery_code"], "download_url": ("/download/" + o["order_no"]) if o["delivery_file"] else ""})
def _push_params():
	p = {}
	p.update(request.args.to_dict())
	p.update(request.form.to_dict())
	try:
		j = request.get_json(silent=True) or {}
		for k, v in j.items():
			p.setdefault(k, str(v))
	except Exception:
		pass
	return p
def _verify_push_sign(p, key):
	sign = p.get("sign", "")
	if not sign:
		return False
	typ, price, t, pid = p.get("type", ""), p.get("price", ""), p.get("t", ""), p.get("pid", "")
	if pid and _md5(typ + price + t + pid + key) == sign:
		return True
	return _md5(typ + price + t + key) == sign
def _handle_pay_push(method, price_str, pid, notify_time, raw):
	"""处理一条到账通知：按方式+精确金额匹配最近60分钟内的待支付订单，匹配到就分配发货并标记已付款。返回匹配到的订单号（无匹配返回空串）。"""
	try:
		cents = int((Decimal(str(price_str)) * 100).to_integral_value())
	except (InvalidOperation, ValueError, TypeError):
		return ""
	if method not in ("wechat", "alipay") or cents <= 0:
		return ""
	now = _now_str()
	conn = get_db()
	cur = conn.cursor()
	cur.execute("INSERT INTO pay_notifications(method,amount_cents,price_str,pid,notify_time,payer,raw,matched_order_no,received_at) VALUES(?,?,?,?,?,?,?,?,?)", (method, cents, str(price_str), pid or "", notify_time or "", "", raw or "", "", now))
	nid = cur.lastrowid
	since = (datetime.now() - timedelta(minutes=MONITOR_MATCH_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
	row = cur.execute("SELECT * FROM orders WHERE status='待支付' AND pay_method=? AND pay_amount_cents=? AND created_at>=? ORDER BY created_at DESC LIMIT 1", (method, cents, since)).fetchone()
	matched = ""
	if row:
		dfile, dcode = _assign_delivery(cur, row["product_id"], row["order_no"])
		status = "已付款" if (dfile or dcode) else "缺货待处理"
		cur.execute("UPDATE orders SET status=?, delivery_file=?, delivery_code=?, paid_at=?, pay_note=? WHERE order_no=?", (status, dfile, dcode, now, str(price_str) + " " + (notify_time or ""), row["order_no"]))
		cur.execute("UPDATE pay_notifications SET matched_order_no=? WHERE id=?", (row["order_no"], nid))
		matched = row["order_no"]
	conn.commit()
	conn.close()
	return matched
@app.route("/appPush", methods=["GET", "POST"])
@app.route("/api/monitor/push", methods=["GET", "POST"])
def app_push():
	"""V免签监听App的到账推送：POST /appPush?type=1&price=29.13&t=...&pid=...&sign=md5(type+price+t+pid+key)。"""
	p = _push_params()
	if not _verify_push_sign(p, get_notify_key()):
		abort(403)
	method = MONITOR_TYPE_MAP.get(str(p.get("type", "")), "")
	_handle_pay_push(method, p.get("price", ""), p.get("pid", ""), p.get("t", ""), str(dict(p)))
	return "success"
@app.route("/appHeart", methods=["GET", "POST"])
def app_heart():
	"""V免签监听App的心跳：POST /appHeart?t=...&pid=...&sign=md5(t+pid+key)。"""
	p = _push_params()
	t, pid, sign = p.get("t", ""), p.get("pid", ""), p.get("sign", "")
	if not sign or _md5(t + pid + get_notify_key()) != sign:
		abort(403)
	set_setting("monitor_last_seen", _now_str())
	return "success"
@app.route("/api/pay/notify", methods=["POST"])
def api_pay_notify():
	"""通用到账通知接口（JSON）：{"method":"wechat","amount":"29.13","time":"...","payer":"..."}，密钥放 X-Notify-Key 请求头或 secret 字段。"""
	data = request.get_json(silent=True) or {}
	secret = request.headers.get("X-Notify-Key", "") or str(data.get("secret", ""))
	if secret != get_notify_key():
		abort(403)
	method = str(data.get("method", ""))
	if method in ("1",):
		method = "wechat"
	elif method in ("2",):
		method = "alipay"
	matched = _handle_pay_push(method, str(data.get("amount", "")), str(data.get("pid", "")), str(data.get("time", "")), str(data))
	return jsonify({"ok": True, "matched": matched})
@app.route("/health")
def health():
	return jsonify({"ok": True, "pay_verify_enabled": False, "pay_verify_mode": "disabled", "monitor_enabled": True, "monitor_last_seen": get_setting("monitor_last_seen", "")})
@app.route("/api/products")
def api_products():
	conn = get_db()
	rows = conn.execute("SELECT id,name,price_yuan,description,delivery_type FROM products WHERE active=1 ORDER BY id").fetchall()
	conn.close()
	return jsonify([dict(r) for r in rows])
@app.route("/admin", methods=["GET"])
def admin():
	h = get_setting("admin_password_hash", "")
	if not h:
		return page("首次设置管理员密码", "<p>首次使用请设置管理员密码（至少6位，仅保存在本机 SQLite 的哈希中，不会写进代码）。</p><form method=\"post\" action=\"/admin/setup\"><label>新密码<input type=\"password\" name=\"password\" required></label><label>确认密码<input type=\"password\" name=\"confirm\" required></label><button type=\"submit\">设置密码</button></form>")
	if not admin_logged_in():
		return page("管理员登录", "<form method=\"post\" action=\"/admin/login\"><label>管理员密码<input type=\"password\" name=\"password\" required></label><button type=\"submit\">登录</button></form><p class=\"small\">忘记密码：用 sqlite3 清空 settings 表中的 admin_password_hash 后重新设置（会保留订单，需谨慎操作）。</p>")
	conn = get_db()
	products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
	orders = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100").fetchall()
	pay = {r["method"]: dict(r) for r in conn.execute("SELECT * FROM payment_config").fetchall()}
	codes = conn.execute("SELECT codes.*, products.name AS pname FROM codes LEFT JOIN products ON products.id=codes.product_id ORDER BY codes.id DESC LIMIT 100").fetchall()
	files = conn.execute("SELECT delivery_files.*, products.name AS pname FROM delivery_files LEFT JOIN products ON products.id=delivery_files.product_id ORDER BY delivery_files.id DESC").fetchall()
	notes = conn.execute("SELECT * FROM pay_notifications ORDER BY id DESC LIMIT 20").fetchall()
	conn.close()
	note_rows = "".join("<tr><td>" + (n["method"] or "") + "</td><td>¥" + _fmt_cents(n["amount_cents"]) + "</td><td>" + (n["received_at"] or "") + "</td><td>" + (n["matched_order_no"] or "未匹配") + "</td></tr>" for n in notes)
	shop = get_setting("shop_name", "")
	ann = get_setting("announcement", "")
	verify = get_setting("pay_verify_enabled", "0")
	prod_rows = "".join("<tr><td>" + str(p["id"]) + "</td><td>" + p["name"] + "</td><td>¥" + str(p["price_yuan"]) + "</td><td>" + p["description"] + "</td><td>" + p["delivery_type"] + "</td><td>" + ("上架" if p["active"] else "下架") + "</td></tr>" for p in products)
	order_rows = ""
	for o in orders:
		shot = "<a href=\"/admin/screenshot/" + o["screenshot_name"] + "\" target=\"_blank\">看截图</a>" if o["screenshot_name"] else "无截图"
		paid = ("<br>到账：" + o["paid_at"]) if o["paid_at"] else ""
		order_rows += "<tr><td>" + o["order_no"] + "</td><td>" + o["product_name"] + "</td><td>¥" + str(o["amount_yuan"]) + "</td><td>" + PAY_METHODS.get(o["pay_method"], "") + "</td><td>" + o["customer_name"] + " / " + o["contact"] + "</td><td>应付 ¥" + _fmt_cents(o["pay_amount_cents"] or o["amount_yuan"] * 100) + "<br>" + (o["pay_time"] or "") + " · 单号后4位：" + (o["txn_last4"] or "") + " · " + shot + paid + "</td><td>" + o["status"] + "</td><td>" + (o["delivery_code"] or "") + "</td><td><form method=\"post\" action=\"/admin/order/" + o["order_no"] + "/verify\" style=\"display:inline\"><button>核实通过</button></form> <form method=\"post\" action=\"/admin/order/" + o["order_no"] + "/reject\" style=\"display:inline\"><button>驳回</button></form></td></tr>"
	code_rows = "".join("<tr><td>" + (c["pname"] or "") + "</td><td>" + c["code"] + "</td><td>" + ("已用(" + (c["order_no"] or "") + ")" if c["used"] else "未用") + "</td></tr>" for c in codes)
	file_rows = "".join("<tr><td>" + (f["pname"] or "") + "</td><td>" + f["filename"] + "</td><td>" + f["stored_name"] + "</td></tr>" for f in files)
	pay_forms = ""
	for m, label in PAY_METHODS.items():
		c = pay.get(m, {})
		pay_forms += "<div class=\"card\"><b>" + label + "</b><br><form method=\"post\" action=\"/admin/payment\" enctype=\"multipart/form-data\"><input type=\"hidden\" name=\"method\" value=\"" + m + "\"><label>收款地址<input name=\"address\" value=\"" + (c.get("address") or "") + "\"></label><label>上传收款码图片<input type=\"file\" name=\"qr_file\" accept=\"image/*\"></label><p class=\"small\">当前收款码：" + (c.get("qr_file") or "未上传") + " · 当前地址：" + (c.get("address") or "未填写") + "</p><button type=\"submit\">保存" + label + "收款设置</button></form></div>"
	body = "<p><a href=\"/admin/orders.csv\"><button>导出订单 CSV</button></a> <a href=\"/admin/logout\"><button>退出登录</button></a></p>"
	body += "<div class=\"warn\"><b>真实支付核验：未启用（pay_verify_enabled=" + verify + "）。</b>本站目前不接通任何支付 API，客户提交凭证后即自动发货、订单为“待核实”。要启用真核验需另行配置（见 README）：PayPal 需商家 Client ID / Secret + Webhook；微信支付需商户号 + APIv3 密钥 + 证书 + 回调域名；支付宝需 AppID + 应用私钥 + 支付宝公钥。未提供这些凭证前切勿声称“已验证到账”。</div>"
	body += "<div class=\"card\"><h3>手机监听（自建收款识别）</h3><p>手机上的 V免签监听 App 读到微信/支付宝到账通知后，会把金额发到本站，按精确金额自动匹配待支付订单并自动发货。</p><p>通知地址：<b>" + request.host_url.rstrip("/") + "</b><br>到账推送：<b>/appPush</b> · 心跳：<b>/appHeart</b><br>通讯密钥：<b>" + get_notify_key() + "</b>（填进手机 App 的“通讯密钥”，不要发给别人）<br>通道 ID：微信填 1，支付宝填 2<br>最近心跳：<b>" + (get_setting("monitor_last_seen", "") or "从未收到") + "</b></p><form method=\"post\" action=\"/admin/notify-key/regenerate\" style=\"display:inline\"><button>重新生成通讯密钥</button></form><p class=\"small\">换密钥后手机 App 里也要改成新的，否则推送会被拒绝。</p><table><tr><th>方式</th><th>到账金额</th><th>收到时间</th><th>匹配订单</th></tr>" + note_rows + "</table></div>"
	body += "<div class=\"card\"><h3>店铺设置</h3><form method=\"post\" action=\"/admin/settings\"><label>店名<input name=\"shop_name\" value=\"" + shop + "\"></label><label>公告<textarea name=\"announcement\">" + ann + "</textarea></label><button type=\"submit\">保存店铺设置</button></form></div>"
	body += "<div class=\"card\"><h3>收款设置</h3>" + pay_forms + "</div>"
	body += "<div class=\"card\"><h3>商品管理</h3><form method=\"post\" action=\"/admin/product/add\"><label>名称<input name=\"name\" required></label><label>价格（元）<input name=\"price_yuan\" type=\"number\" required></label><label>描述<input name=\"description\"></label><label>交付类型<input name=\"delivery_type\" value=\"数字文件\"></label><button type=\"submit\">新增商品</button></form>"
	body += "<table><tr><th>ID</th><th>名称</th><th>价格</th><th>描述</th><th>交付类型</th><th>状态</th></tr>" + prod_rows + "</table>"
	body += "<p class=\"small\">编辑/删除商品：按 ID 操作（名称、价格、描述可改，留空则不改）。</p><form method=\"post\" action=\"/admin/product/edit\"><label>商品ID<input name=\"id\" type=\"number\" required></label><label>新名称<input name=\"name\"></label><label>新价格<input name=\"price_yuan\" type=\"number\"></label><label>新描述<input name=\"description\"></label><button type=\"submit\">保存修改</button></form>"
	body += "<form method=\"post\" action=\"/admin/product/delete\"><label>商品ID<input name=\"id\" type=\"number\" required></label><button type=\"submit\">删除该商品</button></form></div>"
	body += "<div class=\"card\"><h3>发货库存</h3><form method=\"post\" action=\"/admin/delivery-file\" enctype=\"multipart/form-data\"><label>商品ID<input name=\"product_id\" type=\"number\" required></label><label>上传发货文件<input type=\"file\" name=\"delivery_file\" required></label><button type=\"submit\">上传发货文件</button></form>"
	body += "<form method=\"post\" action=\"/admin/codes/add\"><label>商品ID<input name=\"product_id\" type=\"number\" required></label><label>兑换码（每行一个）<textarea name=\"codes\" required></textarea></label><button type=\"submit\">添加兑换码</button></form>"
	body += "<table><tr><th>商品</th><th>文件</th><th>存储名</th></tr>" + file_rows + "</table><table><tr><th>商品</th><th>兑换码</th><th>状态</th></tr>" + code_rows + "</table></div>"
	body += "<div class=\"card\"><h3>订单（最新100条）</h3><table><tr><th>订单号</th><th>商品</th><th>金额</th><th>支付</th><th>客户</th><th>付款凭证</th><th>状态</th><th>兑换码</th><th>操作</th></tr>" + order_rows + "</table></div>"
	return page("商家后台", body)
@app.route("/admin/setup", methods=["POST"])
def admin_setup():
	if get_setting("admin_password_hash", ""):
		return redirect(url_for("admin"))
	pw = request.form.get("password", "")
	if len(pw) < 6 or pw != request.form.get("confirm", ""):
		return page("设置失败", "<p>密码至少6位，且两次输入需一致。请返回重试。</p>"), 400
	set_setting("admin_password_hash", generate_password_hash(pw))
	session["admin_logged_in"] = True
	return redirect(url_for("admin"))
@app.route("/admin/login", methods=["POST"])
def admin_login():
	h = get_setting("admin_password_hash", "")
	if h and check_password_hash(h, request.form.get("password", "")):
		session["admin_logged_in"] = True
		return redirect(url_for("admin"))
	return page("登录失败", "<p>密码错误，请返回重试。</p>"), 403
@app.route("/admin/logout")
def admin_logout():
	session.pop("admin_logged_in", None)
	return redirect(url_for("admin"))
@app.route("/admin/settings", methods=["POST"])
def admin_settings():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	set_setting("shop_name", request.form.get("shop_name", ""))
	set_setting("announcement", request.form.get("announcement", ""))
	return redirect(url_for("admin"))
@app.route("/admin/product/add", methods=["POST"])
def admin_product_add():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	conn = get_db()
	conn.execute("INSERT INTO products(name,price_yuan,description,delivery_type) VALUES(?,?,?,?)", (request.form.get("name", ""), int(request.form.get("price_yuan", "0") or 0), request.form.get("description", ""), request.form.get("delivery_type", "数字文件") or "数字文件"))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/product/edit", methods=["POST"])
def admin_product_edit():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	conn = get_db()
	pid = int(request.form.get("id", "0") or 0)
	if request.form.get("name"):
		conn.execute("UPDATE products SET name=? WHERE id=?", (request.form["name"], pid))
	if request.form.get("price_yuan"):
		conn.execute("UPDATE products SET price_yuan=? WHERE id=?", (int(request.form["price_yuan"]), pid))
	if request.form.get("description"):
		conn.execute("UPDATE products SET description=? WHERE id=?", (request.form["description"], pid))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/product/delete", methods=["POST"])
def admin_product_delete():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	conn = get_db()
	conn.execute("DELETE FROM products WHERE id=?", (int(request.form.get("id", "0") or 0),))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/payment", methods=["POST"])
def admin_payment():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	method = request.form.get("method", "")
	if method not in PAY_METHODS:
		abort(400)
	conn = get_db()
	if request.form.get("address") is not None:
		conn.execute("UPDATE payment_config SET address=? WHERE method=?", (request.form["address"], method))
	f = request.files.get("qr_file")
	if f and f.filename:
		fn = secure_filename(f.filename)
		stored = method + "-" + secrets.token_hex(4) + "-" + fn
		f.save(str(QR_DIR / stored))
		conn.execute("UPDATE payment_config SET qr_file=? WHERE method=?", (stored, method))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/delivery-file", methods=["POST"])
def admin_delivery_file():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	f = request.files.get("delivery_file")
	if not f or not f.filename:
		abort(400)
	pid = int(request.form.get("product_id", "0") or 0)
	fn = secure_filename(f.filename)
	stored = "p" + str(pid) + "-" + secrets.token_hex(4) + "-" + fn
	f.save(str(FILES_DIR / stored))
	conn = get_db()
	conn.execute("INSERT INTO delivery_files(product_id,filename,stored_name,created_at) VALUES(?,?,?,?)", (pid, fn, stored, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/codes/add", methods=["POST"])
def admin_codes_add():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	pid = int(request.form.get("product_id", "0") or 0)
	conn = get_db()
	for line in (request.form.get("codes", "") or "").splitlines():
		code = line.strip()
		if code:
			conn.execute("INSERT INTO codes(product_id,code,created_at) VALUES(?,?,?)", (pid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/order/<order_no>/verify", methods=["POST"])
def admin_order_verify(order_no):
	if not admin_logged_in():
		return redirect(url_for("admin"))
	conn = get_db()
	conn.execute("UPDATE orders SET status='已核实' WHERE order_no=?", (order_no,))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/order/<order_no>/reject", methods=["POST"])
def admin_order_reject(order_no):
	if not admin_logged_in():
		return redirect(url_for("admin"))
	conn = get_db()
	conn.execute("UPDATE orders SET status='已驳回' WHERE order_no=?", (order_no,))
	conn.commit()
	conn.close()
	return redirect(url_for("admin"))
@app.route("/admin/notify-key/regenerate", methods=["POST"])
def admin_notify_key_regenerate():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	set_setting("notify_key", secrets.token_hex(16))
	return redirect(url_for("admin"))
@app.route("/admin/orders.csv")
def admin_orders_csv():
	if not admin_logged_in():
		return redirect(url_for("admin"))
	conn = get_db()
	rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
	conn.close()
	buf = io.StringIO()
	w = csv.writer(buf)
	w.writerow(["order_no", "product_name", "amount_yuan", "pay_amount_cents", "rush", "pay_method", "customer_name", "contact", "pay_time", "txn_last4", "status", "paid_at", "delivery_code", "delivery_file", "created_at"])
	for r in rows:
		w.writerow([r["order_no"], r["product_name"], r["amount_yuan"], r["pay_amount_cents"], r["rush"], r["pay_method"], r["customer_name"], r["contact"], r["pay_time"], r["txn_last4"], r["status"], r["paid_at"], r["delivery_code"], r["delivery_file"], r["created_at"]])
	data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
	return send_file(data, mimetype="text/csv", as_attachment=True, download_name="orders.csv")
init_db()
if __name__ == "__main__":
	app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
