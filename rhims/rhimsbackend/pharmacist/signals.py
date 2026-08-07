"""
Pharmacist app signals.
Stock alert checks are called directly from model save() methods
via _check_stock_alerts(), so no additional signal wiring is needed here.
This file exists so pharmacist/apps.py ready() can import it without error
if signal-based hooks are added in the future.
"""
