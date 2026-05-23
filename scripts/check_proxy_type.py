import asyncio
import aiohttp
import logging
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from db.models import Account, Proxy
from config.settings import get_settings
import socks
import socket
import requests

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Get DB_URL from settings
DB_URL = get_settings().DB_URL

def check_proxy_protocol(proxy_str):
    """
    Determines if a proxy is HTTP or SOCKS5 by trying to connect.
    Returns: 'http', 'socks5', or None (dead)
    """
    # Parse proxy string
    # Formats: user:pass@host:port or host:port:user:pass or host:port
    # We expect the format stored in DB, usually with protocol prefix or without.
    # The user's DB has prefixes now.
    
    clean_proxy = proxy_str
    if "://" in clean_proxy:
        protocol, clean_proxy = clean_proxy.split("://", 1)
    
    parts = clean_proxy.split('@')
    if len(parts) == 2:
        auth, endpoint = parts
        if ':' in auth:
            user, password = auth.split(':', 1)
        else:
            user, password = auth, ""
            
        if ':' in endpoint:
            host, port = endpoint.split(':')
            port = int(port)
        else:
            return None
    else:
        # Try host:port:user:pass format if applicable, or just host:port
        parts = clean_proxy.split(':')
        if len(parts) == 4:
            host, port, user, password = parts
            port = int(port)
        elif len(parts) == 2:
            host, port = parts
            port = int(port)
            user, password = None, None
        else:
            return None

    # Test HTTP
    try:
        proxies = {
            "http": f"http://{user}:{password}@{host}:{port}" if user else f"http://{host}:{port}",
            "https": f"http://{user}:{password}@{host}:{port}" if user else f"http://{host}:{port}",
        }
        response = requests.get("http://highlight.run", proxies=proxies, timeout=10)
        if response.status_code == 200:
            return "http"
    except Exception as e:
        pass

    # Test SOCKS5
    try:
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, host, port, True, user, password)
        s.settimeout(10)
        s.connect(("highlight.run", 80))
        s.sendall(b"GET / HTTP/1.1\r\nHost: highlight.run\r\n\r\n")
        response = s.recv(1024)
        if response.startswith(b"HTTP"):
            s.close()
            return "socks5"
        s.close()
    except Exception as e:
        pass

    return None

def fix_proxies_in_db():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Get all unique proxy strings from Accounts
        accounts = session.query(Account).filter(Account.proxy != None).all()
        
        # Also check Proxy table if used
        # proxies_table = session.query(Proxy).all()
        
        logger.info(f"Checking {len(accounts)} accounts with proxies...")
        
        processed_proxies = {} # proxy_str -> correct_protocol
        
        updated_count = 0
        
        for account in accounts:
            current_proxy = account.proxy
            if not current_proxy:
                continue
                
            # Normalize to strip protocol for checking key
            if "://" in current_proxy:
                current_protocol, raw_proxy = current_proxy.split("://", 1)
            else:
                current_protocol = None
                raw_proxy = current_proxy
            
            if raw_proxy in processed_proxies:
                correct_proto = processed_proxies[raw_proxy]
            else:
                logger.info(f"Testing proxy: {raw_proxy} (Current DB proto: {current_protocol})")
                correct_proto = check_proxy_protocol(raw_proxy)
                processed_proxies[raw_proxy] = correct_proto
                logger.info(f"  -> Result: {correct_proto}")
            
            if correct_proto:
                new_proxy_str = f"{correct_proto}://{raw_proxy}"
                if new_proxy_str != current_proxy:
                    logger.info(f"Updating Account {account.id}: {current_proxy} -> {new_proxy_str}")
                    account.proxy = new_proxy_str
                    updated_count += 1
            else:
                logger.warning(f"Proxy {raw_proxy} failed both HTTP and SOCKS5 checks. Leaving as is.")
        
        session.commit()
        logger.info(f"Finished. Updated {updated_count} accounts.")
        
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    fix_proxies_in_db()
