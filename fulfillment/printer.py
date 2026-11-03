"""
Zebra thermal printer utilities.

Direct-to-printer via raw TCP socket (port 9100) — no print dialog, no drivers.
The printer must be network-accessible from the Django server.

Configure ZEBRA_PRINTER_HOST = "ip:port" in settings (e.g. "192.168.1.50:9100").
Leave blank to skip direct printing (label PDF URL is always available as fallback).
"""

import logging
import socket
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


def send_zpl_to_printer(zpl_content: str, host: str = None, port: int = 9100, timeout: int = 10) -> dict:
    """
    Send raw ZPL string directly to a Zebra printer over TCP.

    Args:
        zpl_content: Raw ZPL string (starts with ^XA, ends with ^XZ)
        host: Printer IP. Falls back to ZEBRA_PRINTER_HOST setting.
        port: Raw socket port (default 9100)
        timeout: Connection timeout in seconds

    Returns:
        dict with keys: success, message, error
    """
    if not host:
        printer_setting = getattr(settings, "ZEBRA_PRINTER_HOST", "")
        if not printer_setting:
            return {"success": False, "error": "ZEBRA_PRINTER_HOST not configured"}
        if ":" in printer_setting:
            host, port_str = printer_setting.rsplit(":", 1)
            port = int(port_str)
        else:
            host = printer_setting

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(zpl_content.encode("utf-8"))
        logger.info("ZPL sent to printer %s:%s (%d bytes)", host, port, len(zpl_content))
        return {"success": True, "message": f"Sent to {host}:{port}"}
    except socket.timeout:
        logger.error("Printer timeout connecting to %s:%s", host, port)
        return {"success": False, "error": f"Timeout connecting to printer at {host}:{port}"}
    except ConnectionRefusedError:
        logger.error("Printer connection refused at %s:%s", host, port)
        return {"success": False, "error": f"Printer at {host}:{port} refused connection"}
    except OSError as e:
        logger.error("Printer socket error %s:%s — %s", host, port, e)
        return {"success": False, "error": f"Network error: {e}"}


def fetch_and_print_zpl_url(zpl_url: str, host: str = None) -> dict:
    """
    Fetch ZPL content from a URL (EasyPost label_zpl_url) and send to printer.

    Args:
        zpl_url: URL to ZPL file from EasyPost
        host: Printer IP (falls back to settings)

    Returns:
        dict with keys: success, message, error
    """
    try:
        with urllib.request.urlopen(zpl_url, timeout=15) as resp:
            zpl_content = resp.read().decode("utf-8")
    except Exception as e:
        logger.error("Failed to fetch ZPL from %s: %s", zpl_url, e)
        return {"success": False, "error": f"Could not fetch ZPL: {e}"}

    return send_zpl_to_printer(zpl_content, host=host)


def printer_is_reachable(host: str = None, port: int = 9100, timeout: int = 3) -> bool:
    """Quick connectivity check — returns True if printer TCP port is open."""
    if not host:
        printer_setting = getattr(settings, "ZEBRA_PRINTER_HOST", "")
        if not printer_setting:
            return False
        if ":" in printer_setting:
            host, port_str = printer_setting.rsplit(":", 1)
            port = int(port_str)
        else:
            host = printer_setting
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
