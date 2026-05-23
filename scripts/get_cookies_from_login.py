"""
Скрипт для автоматического получения cookies TikTok через логин по email/password.

Использование:
    python scripts/get_cookies_from_login.py email password [proxy]

Пример:
    python scripts/get_cookies_from_login.py user@example.com password123
    python scripts/get_cookies_from_login.py user@example.com password123 http://proxy:8080
"""

import asyncio
import sys
import json
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from patchright.async_api import async_playwright
from config.settings import get_settings

settings = get_settings()


async def login_and_get_cookies(email: str, password: str, proxy: str | None = None) -> dict:
    """
    Логинится в TikTok и получает cookies ЧЕРЕЗ ПРОКСИ (важно для безопасности).
    
    ⚠️ ВАЖНО: Cookies должны быть получены через тот же прокси, который будет использоваться в работе!
    Иначе TikTok свяжет cookies с вашим реальным IP и может заблокировать аккаунт.
    
    Args:
        email: Email аккаунта
        password: Пароль аккаунта
        proxy: ОБЯЗАТЕЛЬНЫЙ прокси (формат: http://user:pass@host:port или socks5://...)
              ⚠️ БЕЗ ПРОКСИ НЕ РЕКОМЕНДУЕТСЯ! Cookies будут привязаны к вашему IP.
    
    Returns:
        dict: Словарь с cookies в формате {name: value}
    """
    if not proxy:
        print("⚠️ ВНИМАНИЕ: Вы запускаете скрипт БЕЗ ПРОКСИ!")
        print("⚠️ Cookies будут привязаны к вашему реальному IP адресу.")
        print("⚠️ Это может привести к банам при использовании через другой IP/прокси.")
        response = input("Продолжить без прокси? (yes/no): ")
        if response.lower() != 'yes':
            raise Exception("Отменено пользователем. Используйте прокси для безопасности.")
    
    playwright = await async_playwright().start()
    browser = None
    context = None
    page = None
    
    try:
        # Настройки запуска браузера
        launch_options = {
            "headless": False,  # Показываем браузер для отладки
            "channel": "chromium"
        }
        
        # КРИТИЧНО: Добавляем прокси (обязательно для безопасности)
        if proxy:
            if proxy.startswith("http://") or proxy.startswith("https://"):
                launch_options["proxy"] = {"server": proxy}
            elif proxy.startswith("socks5://"):
                launch_options["proxy"] = {"server": proxy}
            else:
                # Предполагаем http://
                launch_options["proxy"] = {"server": f"http://{proxy}"}
            print(f"🌐 Использую прокси: {proxy.split('@')[-1] if '@' in proxy else proxy}")
        else:
            print("⚠️ Работа БЕЗ прокси - не рекомендуется!")
        
        browser = await playwright.chromium.launch(**launch_options)
        
        # Создаём контекст
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        page = await context.new_page()
        
        print(f"🌐 Открываю TikTok...")
        await page.goto("https://www.tiktok.com/login/email-or-username", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        
        # Ищем поле email/username
        print(f"📧 Ввожу email...")
        email_selectors = [
            'input[type="email"]',
            'input[type="text"][placeholder*="email" i]',
            'input[placeholder*="Email" i]',
            'input[name="email"]',
            'input[name="username"]',
            'input[data-e2e="login-email-input"]'
        ]
        
        email_input = None
        for selector in email_selectors:
            try:
                email_input = await page.wait_for_selector(selector, timeout=5000, state="visible")
                if email_input:
                    break
            except Exception:
                continue
        
        if not email_input:
            raise Exception("Не найдено поле для ввода email")
        
        await email_input.fill(email)
        await asyncio.sleep(1)
        
        # Ищем поле password
        print(f"🔒 Ввожу пароль...")
        password_selectors = [
            'input[type="password"]',
            'input[placeholder*="password" i]',
            'input[name="password"]',
            'input[data-e2e="login-password-input"]'
        ]
        
        password_input = None
        for selector in password_selectors:
            try:
                password_input = await page.wait_for_selector(selector, timeout=5000, state="visible")
                if password_input:
                    break
            except Exception:
                continue
        
        if not password_input:
            raise Exception("Не найдено поле для ввода пароля")
        
        await password_input.fill(password)
        await asyncio.sleep(1)
        
        # Ищем кнопку логина
        print(f"🔘 Нажимаю кнопку входа...")
        login_selectors = [
            'button[type="submit"]',
            'button[data-e2e="login-button"]',
            'button:has-text("Log in")',
            'button:has-text("Войти")',
            'button:has-text("Sign in")',
            'button[class*="login"]',
            'button[class*="submit"]'
        ]
        
        login_button = None
        for selector in login_selectors:
            try:
                login_button = await page.wait_for_selector(selector, timeout=5000, state="visible")
                if login_button and await login_button.is_enabled():
                    break
            except Exception:
                continue
        
        if not login_button:
            raise Exception("Не найдена кнопка входа")
        
        await login_button.click()
        print(f"⏳ Ожидаю авторизацию...")
        
        # Ждём редиректа на главную страницу или появления капчи
        try:
            # Ждём либо редиректа, либо появления капчи
            await page.wait_for_url("https://www.tiktok.com/**", timeout=30000, wait_until="domcontentloaded")
        except Exception:
            # Проверяем, не появилась ли капча
            captcha = await page.query_selector('[class*="captcha"], [id*="captcha"], iframe[src*="captcha"]')
            if captcha:
                print("⚠️ Обнаружена капча! Пожалуйста, решите её вручную в браузере.")
                print("⏳ Ожидаю решения капчи...")
                # Ждём, пока пользователь решит капчу (максимум 5 минут)
                try:
                    await page.wait_for_url("https://www.tiktok.com/**", timeout=300000, wait_until="domcontentloaded")
                except Exception:
                    raise Exception("Капча не решена в течение 5 минут")
        
        # Проверяем, что мы залогинены (проверяем наличие cookies)
        await asyncio.sleep(3)
        
        # Получаем cookies
        print(f"🍪 Получаю cookies...")
        cookies = await context.cookies()
        
        # Фильтруем только нужные cookies TikTok
        tiktok_cookies = {}
        important_cookies = ['sid_tt', 'sessionid', 'uid_tt', 'sid_guard', 'msToken']
        
        for cookie in cookies:
            if cookie['domain'] in ['.tiktok.com', 'www.tiktok.com']:
                cookie_name = cookie['name']
                if cookie_name in important_cookies or any(key in cookie_name.lower() for key in ['session', 'sid', 'uid', 'token']):
                    tiktok_cookies[cookie_name] = cookie['value']
        
        # Проверяем наличие обязательных cookies
        if 'sid_tt' not in tiktok_cookies and 'sessionid' not in tiktok_cookies:
            # Проверяем, может быть мы не залогинились
            current_url = page.url
            if 'login' in current_url.lower():
                raise Exception("Не удалось войти. Проверьте email и пароль.")
            
            # Если cookies нет, но мы на главной - возможно TikTok изменил формат
            print("⚠️ Не найдены стандартные cookies. Показываю все cookies...")
            for cookie in cookies:
                if cookie['domain'] in ['.tiktok.com', 'www.tiktok.com']:
                    tiktok_cookies[cookie['name']] = cookie['value']
        
        if not tiktok_cookies:
            raise Exception("Не удалось получить cookies. Возможно, вход не выполнен.")
        
        print(f"✅ Успешно получено {len(tiktok_cookies)} cookies!")
        return tiktok_cookies
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        raise
    finally:
        if page:
            await page.close()
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()


async def main():
    if len(sys.argv) < 3:
        print("Использование: python get_cookies_from_login.py <email> <password> <proxy>")
        print("\n⚠️ ВАЖНО: Прокси ОБЯЗАТЕЛЕН для безопасности!")
        print("   Cookies должны быть получены через тот же прокси, который будет использоваться в работе.")
        print("\nПример:")
        print("  python get_cookies_from_login.py user@example.com password123 http://user:pass@proxy.com:8080")
        print("  python get_cookies_from_login.py user@example.com password123 socks5://user:pass@proxy.com:1080")
        print("\n⚠️ БЕЗ ПРОКСИ: Cookies будут привязаны к вашему IP - это небезопасно!")
        sys.exit(1)
    
    email = sys.argv[1]
    password = sys.argv[2]
    proxy = sys.argv[3] if len(sys.argv) > 3 else None
    
    if not proxy:
        print("\n❌ ОШИБКА: Прокси не указан!")
        print("⚠️ Для безопасности cookies должны быть получены через прокси.")
        print("   Используйте тот же прокси, который будет назначен аккаунту в системе.")
        sys.exit(1)
    
    try:
        cookies = await login_and_get_cookies(email, password, proxy)
        
        # Формируем JSON для импорта
        cookies_json = json.dumps(cookies, indent=2)
        
        print("\n" + "="*60)
        print("✅ COOKIES ПОЛУЧЕНЫ!")
        print("="*60)
        print("\nФормат для импорта в бота:")
        print("-"*60)
        print(f"Аккаунт_{email.split('@')[0]};{json.dumps(cookies)}")
        print("-"*60)
        
        # Сохраняем в файл
        output_file = Path(f"cookies_{email.split('@')[0]}.json")
        output_file.write_text(cookies_json, encoding='utf-8')
        print(f"\n💾 Cookies сохранены в файл: {output_file}")
        
        # Также сохраняем в формате для импорта (с прокси!)
        import_file = Path(f"accounts_import_{email.split('@')[0]}.txt")
        import_line = f"Аккаунт_{email.split('@')[0]};{json.dumps(cookies)}"
        if proxy:
            import_line += f";{proxy}"
        import_file.write_text(import_line, encoding='utf-8')
        print(f"📄 Файл для импорта: {import_file}")
        print(f"✅ Прокси сохранён: {proxy.split('@')[-1] if '@' in proxy else proxy}")
        print("\n💡 Теперь вы можете импортировать этот файл через бота!")
        print("   Прокси уже указан в файле, система автоматически назначит его аккаунту.")
        
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

