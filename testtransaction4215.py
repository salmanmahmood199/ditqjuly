#!/usr/bin/env python3
"""
Test Transaction 4213 - Accurate mapping from portal receipt
Based on COM4 Transaction 0877 with correct timing and structure
Portal shows proper PRODUCTS and DISCOUNTS sections
"""

import logging
import uuid
from api_client import APIClient
from config import Config

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')


def print_banner():
    """Print test banner"""
    print("=" * 80)
    print("TEST TRANSACTION 4214 - ACCURATE MAPPING FROM PORTAL RECEIPT")
    print("=" * 80)
    print("REGISTER TRANSACTION: 0877")
    print("Portal Receipt Data:")
    print("Items:")
    print("  • DM Banana24ct - 2 x $0.89 = $1.78")
    print("  • B&M PT Casino NICE Uprt - 1 x $1.73 = $1.73")
    print("Discounts:")
    print("  • PROMO EVD Bananas - $0.78")
    print("Subtotal: $3.51")
    print("Tax: $0.11")
    print("Total: $2.84")
    print("Payment: Cash $5.00")
    print("Change: $2.16")
    print("=" * 80)


def create_transaction_4213():
    """Create transaction 4213 - Based on actual portal receipt from register 0877"""
    import uuid

    transaction_data = {
        'Event': {
            'TransactionGUID': str(uuid.uuid4()),
            'TransactionDateTimeStamp': '2025-07-16T14:41:00',
            'TransactionType': 'New',
            'BusinessDate': '20250715',
            'Location': {
                'LocationID': '1001',
                'Description': 'Store 1001'
            },
            'TransactionDevice': {
                'DeviceID': '02',
                'DeviceDescription': 'POS Terminal 02'
            },
            'Employee': {
                'EmployeeID': 'OP15',
                'EmployeeFullName': 'OP15'
            },
            'EventTypeOrder': {
                'Order': {
                    'OrderID':
                    str(uuid.uuid4()),
                    'OrderNumber':
                    4214,
                    'OrderTime':
                    '2025-07-16T14:41:00',
                    'OrderState':
                    'Closed',
                    'OrderItem': [{
                        'OrderItemState': [{
                            'ItemState': {
                                'value': 'Added'
                            },
                            'Timestamp': '2025-07-16T14:41:00'
                        }],
                        'MenuProduct': {
                            'menuProductID':
                            'PID4213_1',
                            'name':
                            'DM Banana24ct',
                            'MenuItem': [{
                                'ItemType':
                                'Sale',
                                'Category':
                                'Produce',
                                'iD':
                                'PID4213_1_MI',
                                'Description':
                                'DM Banana24ct',
                                'Pricing': [{
                                    'Tax': [],
                                    'ItemPrice': 0.89,
                                    'Quantity': 2
                                }],
                                'SKU': {
                                    'productName': 'DM Banana24ct',
                                    'productCode': 'PID4213_1'
                                }
                            }],
                            'SKU': {
                                'productName': 'DM Banana24ct',
                                'productCode': 'PID4213_1'
                            }
                        }
                    }, {
                        'OrderItemState': [{
                            'ItemState': {
                                'value': 'Added'
                            },
                            'Timestamp': '2025-07-16T14:41:00'
                        }],
                        'MenuProduct': {
                            'menuProductID':
                            'PID4213_2',
                            'name':
                            'B&M PT Casino NICE Uprt',
                            'MenuItem': [{
                                'ItemType':
                                'Sale',
                                'Category':
                                'Grocery',
                                'iD':
                                'PID4213_2_MI',
                                'Description':
                                'B&M PT Casino NICE Uprt',
                                'Pricing': [{
                                    'Tax': [],
                                    'ItemPrice': 1.73,
                                    'Quantity': 1
                                }],
                                'SKU': {
                                    'productName': 'B&M PT Casino NICE Uprt',
                                    'productCode': 'PID4213_2'
                                }
                            }],
                            'SKU': {
                                'productName': 'B&M PT Casino NICE Uprt',
                                'productCode': 'PID4213_2'
                            }
                        }
                    }],
                    'Total': {
                        'ItemPrice':
                        3.51,
                        'Tax': [{
                            'amount': 0.11,
                            'Description': 'Sales Tax'
                        }],
                        'Discount': [{
                            'Value': 0.78,
                            'Description': 'PROMO EVD Bananas',
                            'Category': 'Promotion'
                        }]
                    },
                    'OrderItemCount':
                    2,
                    'Payment': [{
                        'Timestamp': '2025-07-16T14:41:00',
                        'Status': 'Accepted',
                        'Amount': 5.00,
                        'Change': 2.16,
                        'TenderType': {
                            'value': 'Cash'
                        }
                    }]
                }
            }
        }
    }

    return transaction_data


def fetch_token() -> str:
    """Authenticate with 360iQ Identity API and get access token"""
    import requests

    # Use the same credentials as the working transactions
    IDENTITY_URL = 'https://identity-qa.go360iq.com/connect/token'
    CLIENT_ID = 'externalPartner_NSRPetrol'
    CLIENT_SECRET = 'PLuz6j0b1D8Iqi2Clq2qv'

    logging.info("Fetching authentication token...")

    response = requests.post(IDENTITY_URL,
                             data={
                                 'grant_type': 'client_credentials',
                                 'client_id': CLIENT_ID,
                                 'client_secret': CLIENT_SECRET
                             },
                             timeout=10)

    if response.status_code != 200:
        logging.error(f"Authentication failed: {response.status_code}")
        logging.error(f"Response: {response.text}")
        return None

    token_data = response.json()
    access_token = token_data['access_token']
    expires_in = token_data.get('expires_in', 3600)

    logging.info(
        f"Authentication successful. Token expires in {expires_in} seconds")
    return access_token


def send_transaction_to_360iq(transaction_data: dict,
                              access_token: str) -> bool:
    """Send transaction to 360iQ Data API"""
    try:
        import requests
        import json

        headers = {
            'Authorization': f'Bearer {access_token}',
            'External-Party-ID': 'externalPartner_NSRPetrol',
            'Content-Type': 'application/json'
        }

        logging.info("Sending transaction to 360iQ Data API...")

        response = requests.post(
            'https://data-api-uat.go360iq.com/v1/Transactions',
            headers=headers,
            json=transaction_data,
            timeout=30)

        logging.info(f"API Response Status: {response.status_code}")
        logging.info(f"API Response: {response.text}")

        if response.status_code == 202:
            logging.info("Transaction sent successfully!")
            return True
        else:
            logging.error(
                f"Failed to send transaction: {response.status_code} - {response.text}"
            )
            return False

    except Exception as e:
        logging.error(f"Error sending transaction: {str(e)}")
        return False


def main():
    """Main function to send test transaction 4213"""
    print_banner()

    # Authenticate
    access_token = fetch_token()
    if not access_token:
        print("❌ Authentication failed!")
        return

    # Create transaction
    transaction_data = create_transaction_4213()

    # Print transaction summary
    order = transaction_data['Event']['EventTypeOrder']['Order']
    total = order['Total']

    print("\nTransaction Summary:")
    print(f"Transaction ID: {transaction_data['Event']['TransactionGUID']}")
    print(f"Order Number: {order['OrderNumber']}")
    print(f"Store: {transaction_data['Event']['Location']['LocationID']}")
    print(
        f"Terminal: {transaction_data['Event']['TransactionDevice']['DeviceID']}"
    )
    print(f"Employee: {transaction_data['Event']['Employee']['EmployeeID']}")
    print(f"Items: {len(order['OrderItem'])}")
    print(f"Subtotal: ${total['ItemPrice']:.2f}")
    print(f"Tax: ${total['Tax'][0]['amount']:.2f}")
    print(f"Discount: ${total['Discount'][0]['Value']:.2f}")
    print(f"Total: ${order['Payment'][0]['Amount']:.2f}")
    print(
        f"Payment: {order['Payment'][0]['TenderType']['value']} - ${order['Payment'][0]['Amount']:.2f}"
    )
    print(f"Change: ${order['Payment'][0]['Change']:.2f}")

    # Send transaction
    success = send_transaction_to_360iq(transaction_data, access_token)

    if success:
        print(f"\n✓ Transaction 4213 sent successfully!")
        print("This transaction accurately reflects the portal receipt data")
        print("Check the 360iQ UAT portal to verify the transaction appears.")
        print(
            "Portal URL: https://uat.go360iq.com/UAT/EZ360iQWeb/en-US/#/TRANSACTIONS"
        )
    else:
        print(f"\n❌ Failed to send transaction 4213")


if __name__ == "__main__":
    main()
