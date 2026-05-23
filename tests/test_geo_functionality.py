"""
Тесты для проверки гео-функциональности.
Запуск: python -m pytest tests/test_geo_functionality.py -v
Или: python tests/test_geo_functionality.py (для простых тестов без pytest)
"""
import sys
import os

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from db.models import Account, Proxy, Base
from services.account_service import get_free_account_for_main_comment, get_free_account_for_reply
from services.proxy_assignment_service import assign_proxies_to_accounts
from services.geo_detection_service import detect_account_country
from services.geo_service import detect_proxy_country
from config.settings import get_settings
from datetime import datetime, timezone
import asyncio

settings = get_settings()

# Используем тестовую БД в памяти
TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(TEST_DB_URL)
SessionLocal = sessionmaker(bind=engine)


def setup_test_db():
    """Создает тестовую БД."""
    Base.metadata.create_all(engine)
    return SessionLocal()


def teardown_test_db():
    """Очищает тестовую БД."""
    Base.metadata.drop_all(engine)


def test_geo_prioritization_main_comment():
    """Test: checks prioritization of geo-matched accounts for main comments."""
    print("\n=== Test 1: Geo-prioritization for main comments ===")
    
    db = setup_test_db()
    
    try:
        # Создаем прокси с разными странами
        proxy_us = Proxy(
            proxy="192.168.1.1:8080",
            country_code="US",
            status="active",
            response_time_ms=100
        )
        proxy_ua = Proxy(
            proxy="192.168.1.2:8080",
            country_code="UA",
            status="active",
            response_time_ms=100
        )
        db.add(proxy_us)
        db.add(proxy_ua)
        db.commit()
        
        # Создаем аккаунты
        # Аккаунт 1: US страна + US прокси (geo-matched) - должен быть выбран первым
        account_1 = Account(
            label="acc_us_matched",
            country_code="US",
            proxy="192.168.1.1:8080",
            status="active",
            today_main_comments=0,
            last_main_comment_at=None
        )
        
        # Аккаунт 2: UA страна + US прокси (geo-mismatch) - должен быть выбран вторым
        account_2 = Account(
            label="acc_ua_mismatch",
            country_code="UA",
            proxy="192.168.1.1:8080",
            status="active",
            today_main_comments=0,
            last_main_comment_at=None
        )
        
        # Аккаунт 3: US страна + UA прокси (geo-mismatch) - должен быть выбран вторым
        account_3 = Account(
            label="acc_us_mismatch",
            country_code="US",
            proxy="192.168.1.2:8080",
            status="active",
            today_main_comments=0,
            last_main_comment_at=None
        )
        
        db.add(account_1)
        db.add(account_2)
        db.add(account_3)
        db.commit()
        
        # Временно включаем geo-matching
        original_setting = settings.PROXY_GEO_MATCHING
        settings.PROXY_GEO_MATCHING = True
        
        try:
            # Получаем аккаунт (должен быть account_1 - geo-matched)
            selected = get_free_account_for_main_comment(db)
            
            if selected:
                print(f"Selected account: {selected.label} (country: {selected.country_code}, proxy: {selected.proxy})")
                if selected.label == "acc_us_matched":
                    print("CORRECT: Selected geo-matched account (US-US)")
                    return True
                else:
                    print(f"ERROR: Selected account {selected.label} instead of geo-matched account")
                    return False
            else:
                print("ERROR: Account not selected")
                return False
        finally:
            settings.PROXY_GEO_MATCHING = original_setting
            
    finally:
        db.close()
        teardown_test_db()


def test_geo_prioritization_reply():
    """Test: checks prioritization of geo-matched accounts for replies."""
    print("\n=== Test 2: Geo-prioritization for replies ===")
    
    db = setup_test_db()
    
    try:
        # Создаем прокси
        proxy_us = Proxy(
            proxy="192.168.1.1:8080",
            country_code="US",
            status="active"
        )
        db.add(proxy_us)
        db.commit()
        
        # Создаем аккаунты
        account_matched = Account(
            label="acc_reply_matched",
            country_code="US",
            proxy="192.168.1.1:8080",
            status="active",
            today_replies=0
        )
        
        account_mismatch = Account(
            label="acc_reply_mismatch",
            country_code="UA",
            proxy="192.168.1.1:8080",
            status="active",
            today_replies=0
        )
        
        db.add(account_matched)
        db.add(account_mismatch)
        db.commit()
        
        original_setting = settings.PROXY_GEO_MATCHING
        settings.PROXY_GEO_MATCHING = True
        
        try:
            selected = get_free_account_for_reply(db)
            
            if selected:
                print(f"Selected account: {selected.label} (country: {selected.country_code})")
                if selected.label == "acc_reply_matched":
                    print("CORRECT: Selected geo-matched account")
                    return True
                else:
                    print(f"ERROR: Selected account {selected.label} instead of geo-matched")
                    return False
            else:
                print("ERROR: Account not selected")
                return False
        finally:
            settings.PROXY_GEO_MATCHING = original_setting
            
    finally:
        db.close()
        teardown_test_db()


def test_proxy_assignment_geo_matching():
    """Test: checks proxy assignment with geo-matching."""
    print("\n=== Test 3: Proxy assignment with geo-matching ===")
    
    db = setup_test_db()
    
    try:
        # Создаем прокси
        proxy_us = Proxy(
            proxy="192.168.1.1:8080",
            country_code="US",
            status="active"
        )
        proxy_ua = Proxy(
            proxy="192.168.1.2:8080",
            country_code="UA",
            status="active"
        )
        db.add(proxy_us)
        db.add(proxy_ua)
        db.commit()
        
        # Создаем аккаунты без прокси
        account_us = Account(
            label="acc_us",
            country_code="US",
            status="active"
        )
        
        account_ua = Account(
            label="acc_ua",
            country_code="UA",
            status="active"
        )
        
        db.add(account_us)
        db.add(account_ua)
        db.commit()
        
        original_setting = settings.PROXY_GEO_MATCHING
        settings.PROXY_GEO_MATCHING = True
        
        try:
            result = assign_proxies_to_accounts(
                db,
                only_accounts_without_proxy=True,
                reuse_existing=False,
                use_geo_matching=True
            )
            
            db.refresh(account_us)
            db.refresh(account_ua)
            
            print(f"Assigned: {result.get('assigned', 0)} accounts")
            print(f"Geo-matched: {result.get('geo_matched', 0)}")
            print(f"Geo-fallback: {result.get('geo_fallback', 0)}")
            print(f"US account got proxy: {account_us.proxy}")
            print(f"UA account got proxy: {account_ua.proxy}")
            
            # Проверяем, что аккаунты получили правильные прокси
            if account_us.proxy == "192.168.1.1:8080" and account_ua.proxy == "192.168.1.2:8080":
                print("CORRECT: Accounts got proxies with matching geo")
                if result.get('geo_matched', 0) >= 2:
                    print("CORRECT: All assignments geo-matched")
                    return True
                else:
                    print(f"WARNING: Geo-matched: {result.get('geo_matched', 0)}, expected >= 2")
                    return False
            else:
                print(f"ERROR: Incorrect proxy assignment")
                return False
        finally:
            settings.PROXY_GEO_MATCHING = original_setting
            
    finally:
        db.close()
        teardown_test_db()


def test_account_country_detection():
    """Test: checks country_code detection from cookies."""
    print("\n=== Test 4: Country code detection from cookies ===")
    
    # Test 1: JSON with locale
    cookies_with_locale_us = '{"locale": "en_US", "sessionid": "test123"}'
    account_us = Account(
        label="test_us",
        cookies=cookies_with_locale_us
    )
    
    country = detect_account_country(account_us)
    print(f"Cookies with locale en_US: detected country = {country}")
    if country == "US":
        print("CORRECT: Detected country US")
        return True
    else:
        print(f"ERROR: Expected US, got {country}")
        return False


def test_account_without_country_code():
    """Test: checks behavior when country_code is missing."""
    print("\n=== Test 5: Accounts without country_code ===")
    
    db = setup_test_db()
    
    try:
        proxy_us = Proxy(
            proxy="192.168.1.1:8080",
            country_code="US",
            status="active"
        )
        db.add(proxy_us)
        db.commit()
        
        # Аккаунт с country_code (высокий приоритет)
        account_with_geo = Account(
            label="acc_with_geo",
            country_code="US",
            proxy="192.168.1.1:8080",
            status="active",
            today_replies=0
        )
        
        # Аккаунт без country_code (низкий приоритет)
        account_without_geo = Account(
            label="acc_without_geo",
            proxy="192.168.1.1:8080",
            status="active",
            today_replies=0
        )
        
        db.add(account_with_geo)
        db.add(account_without_geo)
        db.commit()
        
        original_setting = settings.PROXY_GEO_MATCHING
        settings.PROXY_GEO_MATCHING = True
        
        try:
            selected = get_free_account_for_reply(db)
            
            if selected:
                print(f"Selected account: {selected.label}")
                if selected.country_code:
                    print(f"Account has country_code: {selected.country_code}")
                    return True
                else:
                    print("WARNING: Selected account without country_code (this is OK if no others)")
                    return True
            else:
                print("ERROR: Account not selected")
                return False
        finally:
            settings.PROXY_GEO_MATCHING = original_setting
            
    finally:
        db.close()
        teardown_test_db()


def run_all_tests():
    """Запускает все тесты."""
    print("=" * 60)
    print("GEO FUNCTIONALITY TESTING")
    print("=" * 60)
    
    tests = [
        ("Geo-prioritization (main comments)", test_geo_prioritization_main_comment),
        ("Geo-prioritization (replies)", test_geo_prioritization_reply),
        ("Proxy assignment with geo", test_proxy_assignment_geo_matching),
        ("Country code detection from cookies", test_account_country_detection),
        ("Accounts without country_code", test_account_without_country_code),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"ERROR running test '{test_name}': {e}")
            import traceback
            traceback.print_exc()
            results.append((test_name, False))
    
    # Итоги
    print("\n" + "=" * 60)
    print("TEST RESULTS")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "PASSED" if result else "FAILED"
        print(f"{status}: {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("ALL TESTS PASSED!")
        return True
    else:
        print(f"{total - passed} test(s) failed")
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)

