import sys
import os
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Настройка путей для импорта модулей проекта
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("scripts.fix_proxy_protocol")

def fix_proxies():
    """
    Исправляет протоколы прокси в базе данных.
    1. Меняет порт 40102 на 50102 и добавляет socks5:// (специфично для текущей проблемы).
    2. (Опционально) Добавляет socks5:// к другим прокси.
    """
    settings = get_settings()
    engine = create_engine(settings.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        logger.info("Starting proxy protocol fix...")
        
        # 1. Исправление специфичной проблемы с портом 40102
        # Ищем записи с портом 40102
        # Обычно прокси выглядят как "user:pass@host:port" или "host:port" или с протоколом
        
        # FIX: Escape colon for SQLAlchemy or better, use parameters for the search string
        search_pattern = '%:40102'
        
        accounts_to_fix = session.execute(text(
            "SELECT id, proxy FROM accounts WHERE proxy LIKE :pattern AND proxy NOT LIKE '%://%'"
        ), {"pattern": search_pattern}).fetchall()
        
        fixed_count_specific = 0
        
        for acc_id, proxy in accounts_to_fix:
            if not proxy:
                continue
                
            # Меняем порт и добавляем протокол
            # Используем python replace, так как это проще чем SQL replace для всех диалектов
            new_proxy = f"socks5://{proxy.replace(':40102', ':50102')}"
            
            session.execute(text(
                "UPDATE accounts SET proxy = :proxy WHERE id = :id"
            ), {"proxy": new_proxy, "id": acc_id})
            
            fixed_count_specific += 1
            logger.info(f"Fixed account {acc_id}: {proxy} -> {new_proxy}")
            
        # Также обновляем таблицу proxies если она используется для статистики
        # (хотя прокси привязаны к аккаунтам, таблица proxies может содержать дубликаты)
        try:
            proxies_to_fix = session.execute(text(
                "SELECT id, proxy FROM proxies WHERE proxy LIKE :pattern AND proxy NOT LIKE '%://%'"
            ), {"pattern": search_pattern}).fetchall()
            
            for proxy_id, proxy_val in proxies_to_fix:
                new_proxy_val = f"socks5://{proxy_val.replace(':40102', ':50102')}"
                session.execute(text(
                    "UPDATE proxies SET proxy = :proxy WHERE id = :id"
                ), {"proxy": new_proxy_val, "id": proxy_id})
        except Exception as e:
            logger.warning(f"Could not update proxies table (might not exist or be different): {e}")

        session.commit()
        logger.info(f"Successfully fixed {fixed_count_specific} accounts with port 40102.")
        
        if fixed_count_specific == 0:
            logger.info("No accounts found with port 40102 without protocol. Maybe already fixed or different format?")
            
            # Проверка, может они уже с socks5 но старый порт?
            wrong_port_socks = session.execute(text(
                "SELECT id, proxy FROM accounts WHERE proxy LIKE :pattern AND proxy LIKE 'socks5://%'"
            ), {"pattern": search_pattern}).fetchall()
            
            if wrong_port_socks:
                logger.info(f"Found {len(wrong_port_socks)} accounts with socks5:// but wrong port 40102. Fixing...")
                for acc_id, proxy in wrong_port_socks:
                    new_proxy = proxy.replace(':40102', ':50102')
                    session.execute(text(
                        "UPDATE accounts SET proxy = :proxy WHERE id = :id"
                    ), {"proxy": new_proxy, "id": acc_id})
                    logger.info(f"Fixed account {acc_id}: {proxy} -> {new_proxy}")
                session.commit()

    except Exception as e:
        session.rollback()
        logger.error(f"Error fixing proxies: {e}", exc_info=True)
    finally:
        session.close()

if __name__ == "__main__":
    fix_proxies()
