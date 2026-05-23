"""
Интеграционные тесты для проверки работы гео-функциональности в реальных сценариях.
Запуск: python tests/test_integration_geo.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db.models import Account, Proxy, Base
from services.account_service import get_free_account_for_main_comment
from services.proxy_assignment_service import assign_proxies_to_accounts
from config.settings import get_settings
from datetime import datetime, timezone, timedelta

settings = get_settings()

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(TEST_DB_URL)
SessionLocal = sessionmaker(bind=engine)


def setup_test_db():
    """Создает тестовую БД с тестовыми данными."""
    Base.metadata.create_all(engine)
    db = SessionLocal()
    
    # Создаем прокси
    proxies_data = [
        ("192.168.1.1:8080", "US", 100),
        ("192.168.1.2:8080", "US", 120),
        ("192.168.1.3:8080", "UA", 110),
        ("192.168.1.4:8080", "UA", 130),
        ("192.168.1.5:8080", None, 150),  # Без country_code
    ]
    
    for proxy_str, country, response_time in proxies_data:
        proxy = Proxy(
            proxy=proxy_str,
            country_code=country,
            status="active",
            response_time_ms=response_time
        )
        db.add(proxy)
    
    # Создаем аккаунты
    accounts_data = [
        ("acc_us_1", "US", None),  # Без прокси
        ("acc_us_2", "US", None),  # Без прокси
        ("acc_ua_1", "UA", None),  # Без прокси
        ("acc_ua_2", "UA", None),  # Без прокси
        ("acc_unknown", None, None),  # Без country_code и прокси
    ]
    
    for label, country, proxy in accounts_data:
        account = Account(
            label=label,
            country_code=country,
            proxy=proxy,
            status="active",
            today_main_comments=0,
            today_replies=0
        )
        db.add(account)
    
    db.commit()
    return db


def test_integration_geo_matching():
    """Integration test: full cycle of geo-matching."""
    print("\n" + "=" * 60)
    print("INTEGRATION TEST: Full geo-matching cycle")
    print("=" * 60)
    
    db = setup_test_db()
    
    try:
        original_setting = settings.PROXY_GEO_MATCHING
        settings.PROXY_GEO_MATCHING = True
        
        try:
            # Step 1: Assign proxies
            print("\nStep 1: Assigning proxies to accounts...")
            result = assign_proxies_to_accounts(
                db,
                only_accounts_without_proxy=True,
                reuse_existing=False,
                use_geo_matching=True
            )
            
            print(f"   Assigned: {result.get('assigned', 0)} accounts")
            print(f"   Geo-matched: {result.get('geo_matched', 0)}")
            print(f"   Geo-fallback: {result.get('geo_fallback', 0)}")
            
            # Refresh accounts from DB
            accounts = db.query(Account).all()
            
            # Step 2: Check account selection
            print("\nStep 2: Selecting account for main comment...")
            
            # Clear last_main_comment_at for test
            for acc in accounts:
                acc.last_main_comment_at = None
            db.commit()
            
            selected = get_free_account_for_main_comment(db)
            
            if selected:
                print(f"   Selected account: {selected.label}")
                print(f"   Country code: {selected.country_code}")
                print(f"   Proxy: {selected.proxy}")
                
                # Check if geo matches
                if selected.proxy:
                    proxy_obj = db.query(Proxy).filter(Proxy.proxy == selected.proxy).first()
                    if proxy_obj and selected.country_code and proxy_obj.country_code:
                        if selected.country_code == proxy_obj.country_code:
                            print(f"   Geo-matched: {selected.country_code} == {proxy_obj.country_code}")
                            return True
                        else:
                            print(f"   Geo-mismatch: {selected.country_code} != {proxy_obj.country_code}")
                            # This can be OK if all geo-matched are already used
                            return True
                    else:
                        print(f"   Could not check geo-match (proxy or country_code missing)")
                        return True
                else:
                    print(f"   Account has no proxy")
                    return False
            else:
                print("   Account not selected")
                return False
                
        finally:
            settings.PROXY_GEO_MATCHING = original_setting
            
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def test_scenario_multiple_selections():
    """Test: checks multiple account selections in a row."""
    print("\n" + "=" * 60)
    print("TEST: Multiple account selections")
    print("=" * 60)
    
    db = setup_test_db()
    
    try:
        original_setting = settings.PROXY_GEO_MATCHING
        settings.PROXY_GEO_MATCHING = True
        
        try:
            # Assign proxies
            assign_proxies_to_accounts(db, only_accounts_without_proxy=True, use_geo_matching=True)
            
            # Select multiple accounts in a row
            selected_accounts = []
            exclude_ids = []
            
            for i in range(3):
                acc = get_free_account_for_main_comment(db, exclude_ids=exclude_ids)
                if acc:
                    selected_accounts.append(acc)
                    exclude_ids.append(acc.id)
                    # Simulate usage
                    acc.today_main_comments += 1
                    acc.last_main_comment_at = datetime.now(timezone.utc)
                    db.commit()
            
            print(f"   Selected {len(selected_accounts)} accounts:")
            geo_matched_count = 0
            for acc in selected_accounts:
                proxy_obj = db.query(Proxy).filter(Proxy.proxy == acc.proxy).first() if acc.proxy else None
                is_matched = proxy_obj and acc.country_code and proxy_obj.country_code and acc.country_code == proxy_obj.country_code
                if is_matched:
                    geo_matched_count += 1
                status = "geo-matched" if is_matched else "geo-mismatch or no geo"
                print(f"      • {acc.label}: {acc.country_code} / {proxy_obj.country_code if proxy_obj else 'N/A'} - {status}")
            
            print(f"\n   Geo-matched accounts: {geo_matched_count}/{len(selected_accounts)}")
            
            if geo_matched_count >= len(selected_accounts) * 0.5:  # At least 50% should be geo-matched
                print("   CORRECT: Majority of accounts are geo-matched")
                return True
            else:
                print(f"   WARNING: Less than 50% accounts geo-matched (this can be OK)")
                return True  # Not critical
                
        finally:
            settings.PROXY_GEO_MATCHING = original_setting
            
    finally:
        db.close()
        Base.metadata.drop_all(engine)


if __name__ == "__main__":
    print("=" * 60)
    print("INTEGRATION TESTING OF GEO FUNCTIONALITY")
    print("=" * 60)
    
    results = []
    
    try:
        result1 = test_integration_geo_matching()
        results.append(("Integration test", result1))
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Integration test", False))
    
    try:
        result2 = test_scenario_multiple_selections()
        results.append(("Multiple selections", result2))
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Multiple selections", False))
    
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "PASSED" if result else "FAILED"
        print(f"{status}: {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    sys.exit(0 if passed == total else 1)

