#!/usr/bin/env python3
"""
pos_stream_uploader.py

Listens on COM3 and COM4 for POS JSON payloads, builds full transaction objects,
writes raw JSON to disk, then transforms and POSTs to 360iQ Data API.

Modifications:
  • Store ID is force-overridden to "1001" so that the 360iQ UAT environment
    accepts every transaction.
  • EmployeeID and EmployeeFullName are populated from POS 'operator'.
  • Location.Description is set to a non-empty string ("Store 1001").
  • Subtotal, discounts, tax, and total are now sourced directly from POS transactionSummary.
  • Change calculation uses 'TOTAL DUE' from transactionSummary.
  • Full transactionSummary is stored in each raw JSON for audit.

References:
  – 360iQ Tax sub-model: requires 'amount' and 'Description'
  – 360iQ Transaction Model: Employee/Location fields required
"""

import os
import re
import json
import uuid
import time
import queue
import threading
import requests
import serial
import sys
import csv
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Open (or create) the log file for appending
_log = open("terminal_output.log", "a", buffering=1, encoding="utf-8")

class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            s.write(data)
    def flush(self):
        for s in self.streams:
            s.flush()

# Replace stdout and stderr with our Tee
sys.stdout = _Tee(sys.stdout, _log)
sys.stderr = _Tee(sys.stderr, _log)

# ─── CONFIGURATION ───
SERIAL_PORTS    = ['COM3', 'COM4']
BAUDRATE        = 9600
BYTESIZE        = serial.EIGHTBITS
PARITY          = serial.PARITY_NONE
STOPBITS        = serial.STOPBITS_ONE
RTSCTS          = True
TIMEOUT         = 1  # seconds

IDENTITY_URL    = 'https://identity-qa.go360iq.com/connect/token'
CLIENT_ID       = 'externalPartner_NSRPetrol'
CLIENT_SECRET   = 'PLuz6j0b1D8Iqi2Clq2qv'
CASH_URL        = 'https://data-api-uat.go360iq.com/v1/CashOperations'
TXN_URL         = 'https://data-api-uat.go360iq.com/v1/Transactions'
REFUND_URL      = 'https://data-api-uat.go360iq.com/v1/Refunds'

USER_TZ         = 'America/New_York'
LOG_DIR         = 'logs'
EVENTS_DIR      = 'events'
TRANSACTIONS_DIR= 'transactions'
CSV_REPORT      = '360iQDataAPI-AcceptanceReport.csv'
HEADER_PATTERN  = re.compile(r'mlen=(\d+)$')

# Queues and token cache
tx_queue      = queue.Queue()
parser_queue  = queue.Queue()
_token_data   = {'access_token': None, 'expires_at': 0.0}

# ─── DIRECTORY UTILITIES ───

def ensure_directories():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(EVENTS_DIR, exist_ok=True)
    os.makedirs(TRANSACTIONS_DIR, exist_ok=True)

# ─── AUTHENTICATION ───

def fetch_token() -> str:
    now = time.time()
    if _token_data['access_token'] and (_token_data['expires_at'] - 60) > now:
        return _token_data['access_token']
    resp = requests.post(
        IDENTITY_URL,
        data={
            'grant_type': 'client_credentials',
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET
        },
        timeout=10
    )
    resp.raise_for_status()
    js = resp.json()
    token = js['access_token']
    _token_data['access_token'] = token
    _token_data['expires_at']  = now + js.get('expires_in', 3600)
    print(f"[INFO] Fetched new token; expires in {js.get('expires_in',3600)}s.")
    return token

# ─── TIMESTAMP & GUID ───

def to_utc(local_ts: str) -> str:
    """Convert local timestamp to UTC, or use current time if conversion fails"""
    try:
        # If local_ts is very old, use current time instead to ensure transactions appear in frontend
        tz = ZoneInfo(USER_TZ)
        dt = datetime.fromisoformat(local_ts).replace(tzinfo=tz)
        
        # Check if timestamp is older than 2023
        if dt.year < 2023:
            # Use current time instead
            dt = datetime.now(tz)
            print(f"[INFO] Using current time ({dt.isoformat()}) instead of old timestamp: {local_ts}")
        
        return dt.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S')
    except Exception as e:
        print(f"[WARN] Error converting timestamp: {e}. Using current UTC time.")
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def generate_guid(store: str, terminal: str, seq: str, ts_utc: str) -> str:
    ns = f"{store}-{terminal}-{seq}-{ts_utc}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ns))

# ─── RAW LOGGING & EVENTS ───

def log_raw_json(port: str, raw: str):
    ensure_directories()
    path = os.path.join(LOG_DIR, f"pos_transactions_{port}.log")
    ts = datetime.now(timezone.utc).isoformat()
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f"{ts} {raw}\n")


def save_tx_event(tx: dict):
    ensure_directories()
    fname = f"{tx['seq']}_{tx['guid']}.json"
    with open(os.path.join(EVENTS_DIR, fname), 'w', encoding='utf-8') as f:
        json.dump(tx, f, indent=2)


def write_transaction_by_date(tx: dict, success: bool, status_code: int, resp_body: str = ""):
    ts = tx['ts_utc']
    date = ts.split('T')[0]
    y, m, d = date.split('-')
    base = os.path.join(TRANSACTIONS_DIR, y, m, d)
    sent = os.path.join(base, 'sent')
    failed = os.path.join(base, 'failed')
    os.makedirs(sent, exist_ok=True)
    os.makedirs(failed, exist_ok=True)
    fname = f"{tx['seq']}_{tx['guid']}.json"
    dest = sent if success else failed
    with open(os.path.join(dest, fname), 'w', encoding='utf-8') as f:
        json.dump(tx, f, indent=2)
    logf = os.path.join(dest, 'sent.log' if success else 'failed.log')
    snippet = (resp_body or '')[:200].replace('\n', ' ')
    with open(logf, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {tx['seq']}_{tx['guid']} {status_code} {snippet}\n")
    append_csv_report(tx, success, status_code)

def append_csv_report(tx: dict, success: bool, status_code: int):
    """Append a one-line summary of the transaction to the companion CSV."""
    row = [
        tx.get('ts_utc', ''),
        tx.get('guid', ''),
        tx.get('store', ''),
        tx.get('terminal', ''),
        tx.get('seq', ''),
        tx.get('type', ''),
        'success' if success else 'failed',
        status_code
    ]
    file_exists = os.path.isfile(CSV_REPORT)
    with open(CSV_REPORT, 'a', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        if not file_exists:
            writer.writerow(['ts_utc', 'guid', 'store', 'terminal', 'seq', 'type', 'result', 'status_code'])
        writer.writerow(row)

# ─── TENDER MAPPING ───

def map_tender(desc: str) -> str:
    d = desc.upper()
    if 'CASH' in d: return 'Cash'
    if any(x in d for x in ('VISA','MASTERCARD','AMEX','DISCOVER')): return 'CreditCard'
    if 'DEBIT' in d: return 'DebitCard'
    if d.startswith(('ACCT#','ACCOUNT')): return 'AccountPayment'
    return 'Other'

# ─── SERIAL-PORT READER ───

def read_from_port(port: str):
    while True:
        try:
            print(f"[INFO] Opening serial port {port}...")
            ser = serial.Serial(
                port=port,
                baudrate=BAUDRATE,
                bytesize=BYTESIZE,
                parity=PARITY,
                stopbits=STOPBITS,
                rtscts=RTSCTS,
                timeout=TIMEOUT
            )
            print(f"[INFO] Listening on {port}...")
            while True:
                hdr = ser.readline().decode('utf-8', errors='replace').strip()
                m = HEADER_PATTERN.match(hdr)
                if not m:
                    continue
                length = int(m.group(1))
                data = ser.read(length)
                try:
                    txt = data.decode('utf-8', errors='replace')
                except:
                    txt = data.decode('latin1', errors='ignore')
                log_raw_json(port, txt)
                try:
                    rec = json.loads(txt)
                except json.JSONDecodeError:
                    print(f"[WARN] Invalid JSON on {port}: {txt[:80]}…")
                    continue
                parser_queue.put((port, rec))
        except Exception as e:
            print(f"[ERROR] Port {port}: {e}. Retrying in 5s...")
            time.sleep(5)
        finally:
            try:
                ser.close()
            except:
                pass

# ─── PARSER WORKER ───

buffers = {p: None for p in SERIAL_PORTS}

def parser_worker():
    while True:
        port, rec = parser_queue.get()
        cmd = rec.get('CMD')
        # StartTransaction
        if cmd == 'StartTransaction':
            buffers[port] = {
                'meta': None,
                'items': [],
                'voids': [],
                'payments': [],
                'summary_list': [],
                'summary_map': {}
            }
            parser_queue.task_done()
            continue
        buf = buffers.get(port)
        if buf is None:
            parser_queue.task_done()
            continue
        # metaData
        if rec.get('metaData'):
            buf['meta'] = rec['metaData']
            parser_queue.task_done()
            continue
        # cartChangeTrail
        if rec.get('cartChangeTrail') is not None:
            raw = rec['cartChangeTrail']
            trail = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(trail, dict):
                trail = [trail]
            for c in trail:
                et = c.get('eventType')
                nm = c.get('itemName', '')
                pr = float(c.get('price', 0.0)) if c.get('price') is not None else 0.0
                qt = int(c.get('quantity', 1)) if c.get('quantity') is not None else 1
                entry = {
                    'name': nm,
                    'price': pr,
                    'quantity': qt,
                    'event': 'void' if et == 'voidLineItem' else 'add'
                }
                (buf['voids' if entry['event'] == 'void' else 'items']).append(entry)
            parser_queue.task_done()
            continue
        # paymentSummary
        if rec.get('paymentSummary') is not None:
            raw = rec['paymentSummary']
            pays = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(pays, dict):
                pays = [pays]
            for p in pays:
                amt = float(p.get('details', '0').replace('$', ''))
                buf['payments'].append({'amount': amt, 'tenderType': p.get('description', '')})
            parser_queue.task_done()
            continue
        # transactionSummary
        if rec.get('transactionSummary') is not None:
            raw = rec['transactionSummary']
            summ = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(summ, dict):
                summ = [summ]
            buf['summary_list'] = summ
            smap = {}
            for e in summ:
                key = e.get('description', '').upper().strip()
                val_str = e.get('details', '').replace('$', '').replace(',', '').strip()
                try:
                    val = float(val_str)
                except:
                    val = 0.0
                smap[key] = val
            buf['summary_map'] = smap
            parser_queue.task_done()
            continue
        # EndTransaction
        if cmd == 'EndTransaction':
            m = buf['meta'] or {}
            ts_loc = m.get('timeStamp', '')
            ts_utc = to_utc(ts_loc) if ts_loc else ''
            seq   = str(m.get('transactionSeqNumber', ''))
            store = '1001'
            term  = str(m.get('terminalNumber', ''))
            op    = m.get('operator', '')
            guid  = generate_guid(store, term, seq, ts_utc)
            tx = {
                'guid': guid,
                'ts_local': ts_loc,
                'ts_utc': ts_utc,
                'store': store,
                'terminal': term,
                'seq': seq,
                'type': m.get('transactionType', ''),
                'items': buf['items'],
                'voids': buf['voids'],
                'payments': buf['payments'],
                'transactionSummary': buf['summary_list'],
                'summary_map': buf['summary_map'],
                'employee_id': op,
                'employee_name': op,
                'location_desc': f"Store {store}"}
            save_tx_event(tx)
            tx_queue.put(tx)
            buffers[port] = None
            parser_queue.task_done()
            continue
        parser_queue.task_done()

# ─── PAYLOAD BUILDERS ───

def build_cash_op_payload(tx: dict) -> dict:
    ts = tx['ts_utc']
    biz = ts[:10].replace('-', '')
    seq = int(tx['seq'] or 0)
    return {
        'model': 'CashOperation',
        'Event': {
            'TransactionGUID': tx['guid'],
            'TransactionDateTimeStamp': ts,
            'TransactionType': 'New',
            'BusinessDate': biz,
            'Location': {'LocationID': tx['store'], 'Description': tx['location_desc']},
            'TransactionDevice': {'DeviceID': tx['terminal'], 'DeviceDescription': f"POS Terminal {tx['terminal']}"},
            'Employee': {'EmployeeID': tx['employee_id'], 'EmployeeFullName': tx['employee_name']},
            'EventTypeDrawer': {
                'Drawer': {
                    'DrawerEventGUID': tx['guid'],
                    'DrawerEventNumber': seq,
                    'DrawerOperationType': 'PaidOut',
                    'DrawerOpenTime': ts,
                    'CashManagement': [{'Amount': 0.00}]
                }
            }
        }
    }


def build_txn_payload(tx: dict) -> dict:
    sm = tx['summary_map']
    subtotal = sm.get('SUBTOTAL', 0.0)
    tax_amt  = next((v for k, v in sm.items() if k.startswith('TAX')), 0.0)
    total_due= sm.get('TOTAL DUE', subtotal + tax_amt)
    net_item = Decimal(subtotal).quantize(Decimal('0.01'), ROUND_HALF_UP)
    tax_d    = Decimal(tax_amt).quantize(Decimal('0.01'), ROUND_HALF_UP)
    tot_due  = Decimal(total_due).quantize(Decimal('0.01'), ROUND_HALF_UP)
    paid     = sum(p['amount'] for p in tx['payments'] if p['amount'] > 0)
    paid_d   = Decimal(paid).quantize(Decimal('0.01'), ROUND_HALF_UP)
    change   = (paid_d - tot_due).quantize(Decimal('0.01'), ROUND_HALF_UP)

    items_list = []
    promotions = []
    idx = 1
    for itm in tx['items'] + tx['voids']:
        is_void = itm['event'] == 'void'
        state   = 'Voided' if is_void else 'Added'
        typ     = 'Voided' if is_void else 'Sale'
        pid     = f"PID{tx['seq']}_{idx}"
        idx += 1

        is_promo = 'PROMO' in itm['name'].upper() or 'DISCOUNT' in itm['name'].upper()
        if is_promo and not is_void:
            promo_val = round(abs(itm['price'] * itm['quantity']), 2)
            promotions.append({
                'Value': promo_val,
                'Description': itm['name'],
                'Category': 'Promotion'
            })
            continue

        items_list.append({
            'OrderItemState': [{ 'ItemState': {'value': state}, 'Timestamp': tx['ts_utc'] }],
            'MenuProduct': {
                'menuProductID': pid,
                'name': itm['name'],
                'MenuItem': [{
                    'ItemType': typ,
                    'Category': 'General',
                    'iD': f"{pid}_MI",
                    'Description': itm['name'],
                    'Pricing': [{
                        'Tax': [],
                        'ItemPrice': float(Decimal(itm['price']).quantize(Decimal('0.01'), ROUND_HALF_UP)),
                        'Quantity': itm['quantity']
                    }],
                    'SKU': { 'productName': itm['name'], 'productCode': pid }
                }],
                'SKU': { 'productName': itm['name'], 'productCode': pid }
            }
        })

    payments = []
    pi = 0
    for p in tx['payments']:
        amt = Decimal(p['amount']).quantize(Decimal('0.01'), ROUND_HALF_UP)
        if amt == 0:
            continue
        ch = float(change) if pi == 0 else 0.0
        payments.append({
            'Timestamp': tx['ts_utc'], 'Status': 'Accepted' if amt >= 0 else 'Denied',
            'Amount': float(amt), 'Change': ch,
            'TenderType': {'value': map_tender(p['tenderType'])}
        })
        pi += 1

    tax_arr = [{ 'amount': float(tax_d), 'Description': 'Sales Tax' }] if tax_d > 0 else []
    has_voids = bool(tx['voids'])
    all_voided = has_voids and all(item['event'] == 'void' for item in tx['items'] + tx['voids'])
    order_state = 'Voided' if all_voided else 'Closed'
    transaction_type = 'Update' if has_voids else 'New'
    current_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    evt = {
        'TransactionGUID': tx['guid'],
        'TransactionDateTimeStamp': current_ts,
        'TransactionType': transaction_type,
        'BusinessDate': current_ts[:10].replace('-', ''),
        'Location': {'LocationID': tx['store'], 'Description': tx['location_desc']},
        'TransactionDevice': {'DeviceID': tx['terminal'], 'DeviceDescription': f"POS Terminal {tx['terminal']}"},
        'Employee': {'EmployeeID': tx['employee_id'], 'EmployeeFullName': tx['employee_name']},
        'EventTypeOrder': {
            'Order': {
                'OrderID': tx['guid'],
                'OrderNumber': int(tx['seq'] or 0),
                'OrderTime': current_ts,
                'OrderState': order_state,
                'OrderItem': items_list,
                'Total': {
                    'ItemPrice': float(net_item),
                    'Tax': tax_arr,
                    'Discount': promotions
                },
                'OrderItemCount': len(items_list),
                'Payment': payments
            }
        }
    }
    return { 'model': 'Transaction', 'Event': evt }



def build_refund_payload(tx: dict) -> dict:
    ts = tx['ts_utc']
    biz= ts[:10].replace('-', '')
    items_list=[]; idx=1; raw_sub=Decimal('0.00')
    for itm in tx['items']:
        price = Decimal(itm['price']).quantize(Decimal('0.01'), ROUND_HALF_UP)
        pid   = f"{tx['seq']}_{idx}"; idx+=1
        items_list.append({
            'OrderItemState': [{ 'ItemState': {'value': 'Added'}, 'Timestamp': ts }],
            'MenuProduct': {
                'menuProductID': pid,
                'name': itm['name'],
                'MenuItem': [{
                    'ItemType': 'Sale', 'Category': 'Refund', 'iD': f"{pid}_MI",
                    'Description': itm['name'], 'Pricing': [{ 'Tax': [], 'ItemPrice': float(price), 'Quantity': itm['quantity'] }],
                    'SKU': { 'productName': itm['name'], 'productCode': pid }
                }],
                'SKU': { 'productName': itm['name'], 'productCode': pid }
            }
        })
        raw_sub += price * itm['quantity']
    refund_total = sum(p['amount'] for p in tx['payments'])
    payments=[]
    for p in tx['payments']:
        amt = p['amount']
        if amt == 0: continue
        payments.append({
            'Timestamp': ts, 'Status': 'Accepted', 'Amount': amt, 'Change': 0.0,
            'TenderType': {'value': map_tender(p['tenderType'])}
        })
    order={
        'OrderID': tx['guid'], 'OrderNumber': int(tx['seq'] or 0), 'OrderTime': ts,
        'OrderState': 'Closed', 'OrderItem': items_list,
        'Total': { 'ItemPrice': float(raw_sub.quantize(Decimal('0.01'), ROUND_HALF_UP)), 'Tax': [] },
        'OrderItemCount': len(items_list), 'Payment': payments
    }
    return {
        'model': 'Refund',
        'Event':{
            'TransactionGUID': tx['guid'],
            'TransactionDateTimeStamp': ts,
            'TransactionType': 'New',
            'BusinessDate': biz,
            'Location': {'LocationID': tx['store'], 'Description': tx['location_desc']},
            'TransactionDevice': {'DeviceID': tx['terminal'], 'DeviceDescription': f"POS Terminal {tx['terminal']}"},
            'Employee': {'EmployeeID': tx['employee_id'], 'EmployeeFullName': tx['employee_name']},
            'EventTypeRefund': { 'Refund': { 'RefundTotal': refund_total, 'RefundTransactionType': { 'Order': order } } }
        }
    }

# ─── DISPATCHER ───

def dispatcher_worker():
    while True:
        tx = tx_queue.get()
        
        # Classify transaction type for appropriate URL and logging
        transaction_category = "unknown"
        
        if not tx['items'] and not tx['payments']:
            payload = build_cash_op_payload(tx)
            url = CASH_URL
            transaction_category = "cash-operation"
            
        elif tx['type'].lower() == 'refund':
            payload = build_refund_payload(tx)
            url = REFUND_URL
            transaction_category = "refund"
            
        else:
            # Standard transaction
            payload = build_txn_payload(tx)
            url = TXN_URL
            
            # Categorize the transaction for better logging
            has_voids = bool(tx['voids'])
            all_voided = has_voids and all(item['event'] == 'void' for item in tx['items'] + tx['voids'])
            has_promos = any('PROMO' in item['name'].upper() or item['price'] < 0 for item in tx['items'])
            
            if all_voided:
                transaction_category = "full-void"
            elif has_voids:
                transaction_category = "partial-void"
            elif has_promos:
                transaction_category = "promotion"
            else:
                transaction_category = "standard-sale"
        
        # Log what we're sending
        print(f"\n[INFO] Sending {transaction_category} transaction to {url.split('/')[-1]} endpoint")
        
        # Make the API request
        try:
            token = fetch_token()
            headers = {
                'Authorization': f"Bearer {token}",
                'External-Party-ID': CLIENT_ID,
                'Content-Type': 'application/json'
            }
            
            # Send the payload to the API
            print(f"[INFO] Request payload type: {payload['model']}")
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            status_code = resp.status_code
            body = resp.text
            
            # Log the result
            if 200 <= status_code < 300:
                print(f"[SUCCESS] {transaction_category.upper()} transaction sent successfully: Status {status_code}")
            else:
                print(f"[ERROR] Failed to send {transaction_category} transaction: Status {status_code}")
                print(f"[ERROR] Response body: {body[:200]}...")
                
        except Exception as e:
            status_code = 0
            body = str(e)
            print(f"[ERROR] Exception sending {transaction_category} transaction: {e}")
            
        # Record the result
        success = 200 <= status_code < 300
        write_transaction_by_date(tx, success, status_code, body)
        tx_queue.task_done()

# ─── MAIN ───
if __name__ == '__main__':
    ensure_directories()
    threading.Thread(target=parser_worker, daemon=True).start()
    threading.Thread(target=dispatcher_worker, daemon=True).start()
    for port in SERIAL_PORTS:
        threading.Thread(target=read_from_port, args=(port,), daemon=True).start()
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("[INFO] Shutting down...")