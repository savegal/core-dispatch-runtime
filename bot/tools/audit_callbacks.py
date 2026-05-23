"""
Утилита для аудита callback_data и их обработчиков.
Находит мертвые кнопки, отсутствующие обработчики, неиспользуемые handlers.
"""
import ast
import re
import os
from pathlib import Path
from typing import Dict, Set, List, Tuple
from collections import defaultdict


class CallbackAuditor:
    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root)
        self.bot_dir = self.project_root / "bot"
        self.callback_data_from_buttons: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        self.handlers_from_code: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        self.prefix_handlers: Dict[str, List[Tuple[str, int]]] = defaultdict(list)

    def find_callbacks_in_file(self, filepath: Path) -> None:
        """Парсит файл и находит все callback_data в клавиатурах."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Поиск через регулярные выражения для callback_data
            patterns = [
                r'callback_data=["\']([^"\']+)["\']',  # callback_data="..."
                r'callback_data=(["\'])([^"\']+)\1',  # callback_data='...'
                r'CallbackData\([^)]*\)',  # CallbackData(...) - сложный случай
            ]
            
            for pattern in patterns[:2]:  # Пока простые паттерны
                for match in re.finditer(pattern, content):
                    callback_data = match.group(1) if len(match.groups()) == 1 else match.group(2)
                    # Очистка от экранирования
                    callback_data = callback_data.replace('\\"', '"').replace("\\'", "'")
                    line_num = content[:match.start()].count('\n') + 1
                    self.callback_data_from_buttons[callback_data].append((str(filepath.relative_to(self.project_root)), line_num))
        except Exception as e:
            print(f"Error processing {filepath}: {e}")

    def find_handlers_in_file(self, filepath: Path) -> None:
        """Парсит файл и находит все обработчики callback_query."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Поиск F.data == "..."
            pattern_exact = r'F\.data\s*==\s*["\']([^"\']+)["\']'
            for match in re.finditer(pattern_exact, content):
                handler_data = match.group(1)
                line_num = content[:match.start()].count('\n') + 1
                self.handlers_from_code[handler_data].append((str(filepath.relative_to(self.project_root)), line_num))

            # Поиск F.data.in_({"...", "..."})
            pattern_in = r'F\.data\.in_\(\{([^}]+)\}\)'
            for match in re.finditer(pattern_in, content, flags=re.DOTALL):
                line_num = content[:match.start()].count('\n') + 1
                for handler_data in re.findall(r'["\']([^"\']+)["\']', match.group(1)):
                    self.handlers_from_code[handler_data].append((str(filepath.relative_to(self.project_root)), line_num))
            
            # Поиск F.data.startswith("...")
            pattern_prefix = r'F\.data\.startswith\(["\']([^"\']+)["\']\)'
            for match in re.finditer(pattern_prefix, content):
                prefix = match.group(1)
                line_num = content[:match.start()].count('\n') + 1
                self.prefix_handlers[prefix].append((str(filepath.relative_to(self.project_root)), line_num))

            # Поиск F.data.regexp(r"^prefix...")
            pattern_regex = r'F\.data\.regexp\((?:r)?["\']([^"\']+)["\']\)'
            for match in re.finditer(pattern_regex, content):
                pattern = match.group(1)
                if not pattern.startswith("^"):
                    continue
                literal = []
                for char in pattern[1:]:
                    if char.isalnum() or char in {":", "_", "-"}:
                        literal.append(char)
                    else:
                        break
                prefix = "".join(literal)
                if prefix:
                    line_num = content[:match.start()].count('\n') + 1
                    self.prefix_handlers[prefix].append((str(filepath.relative_to(self.project_root)), line_num))
                
        except Exception as e:
            print(f"Error processing handlers in {filepath}: {e}")

    def scan_directory(self) -> None:
        """Сканирует директорию bot/ на наличие callback_data и handlers."""
        keyboards_dir = self.bot_dir / "keyboards"
        handlers_dir = self.bot_dir / "handlers"
        
        # Сканируем keyboards для callback_data
        if keyboards_dir.exists():
            for filepath in keyboards_dir.rglob("*.py"):
                self.find_callbacks_in_file(filepath)
        
        # Сканируем handlers для обработчиков
        if handlers_dir.exists():
            for filepath in handlers_dir.rglob("*.py"):
                self.find_handlers_in_file(filepath)
        
        # Также сканируем handlers для callback_data (они могут создавать кнопки)
        if handlers_dir.exists():
            for filepath in handlers_dir.rglob("*.py"):
                self.find_callbacks_in_file(filepath)

    def check_prefix_match(self, callback_data: str, prefix: str) -> bool:
        """Проверяет, соответствует ли callback_data префиксу."""
        return callback_data.startswith(prefix)

    def generate_report(self) -> str:
        """Генерирует отчет об аудите."""
        dead_buttons = []
        unused_handlers = []
        prefix_matches = set()
        
        # Проверяем все callback_data на наличие обработчиков
        for callback_data, locations in self.callback_data_from_buttons.items():
            has_exact_handler = callback_data in self.handlers_from_code
            has_prefix_handler = any(
                self.check_prefix_match(callback_data, prefix) 
                for prefix in self.prefix_handlers.keys()
            )
            
            if not has_exact_handler and not has_prefix_handler:
                dead_buttons.append((callback_data, locations))
            elif has_prefix_handler:
                # Запоминаем, что этот callback обработан через префикс
                matching_prefixes = tuple([
                    prefix for prefix in self.prefix_handlers.keys()
                    if self.check_prefix_match(callback_data, prefix)
                ])
                prefix_matches.add((callback_data, matching_prefixes))
        
        # Проверяем, какие обработчики не используются
        all_callback_data = set(self.callback_data_from_buttons.keys())
        for handler_data, locations in self.handlers_from_code.items():
            if handler_data not in all_callback_data:
                # Проверяем, может быть есть callback с префиксом
                has_prefix_match = any(
                    self.check_prefix_match(cb, handler_data) 
                    for cb in all_callback_data
                )
                if not has_prefix_match:
                    unused_handlers.append((handler_data, locations))
        
        # Генерируем отчет
        report_lines = ["# Отчет аудита callback_data и handlers\n\n"]
        report_lines.append(f"Дата: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        report_lines.append("## Статистика\n\n")
        report_lines.append(f"- Всего callback_data в кнопках: {len(self.callback_data_from_buttons)}\n")
        report_lines.append(f"- Всего точных обработчиков: {len(self.handlers_from_code)}\n")
        report_lines.append(f"- Всего префикс-обработчиков: {len(self.prefix_handlers)}\n\n")
        
        report_lines.append(f"## ❌ МЕРТВЫЕ КНОПКИ (DEAD BUTTONS): {len(dead_buttons)}\n\n")
        if dead_buttons:
            for callback_data, locations in sorted(dead_buttons):
                report_lines.append(f"### `{callback_data}`\n")
                for filepath, line_num in locations:
                    report_lines.append(f"- `{filepath}:{line_num}`\n")
                report_lines.append("\n")
        else:
            report_lines.append("✅ Нет мертвых кнопок!\n\n")
        
        report_lines.append(f"## ⚠️ НЕИСПОЛЬЗУЕМЫЕ ОБРАБОТЧИКИ (UNUSED HANDLERS): {len(unused_handlers)}\n\n")
        if unused_handlers:
            for handler_data, locations in sorted(unused_handlers):
                report_lines.append(f"### `{handler_data}`\n")
                for filepath, line_num in locations:
                    report_lines.append(f"- `{filepath}:{line_num}`\n")
                report_lines.append("\n")
        else:
            report_lines.append("✅ Все обработчики используются!\n\n")
        
        report_lines.append("## 📋 ПРЕФИКС-СООТВЕТСТВИЯ\n\n")
        if prefix_matches:
            for callback_data, prefixes in sorted(prefix_matches):
                report_lines.append(f"### `{callback_data}`\n")
                report_lines.append(f"Обрабатывается через префиксы: {', '.join(prefixes)}\n\n")
        
        report_lines.append("## 🔍 ПРЕФИКС-ОБРАБОТЧИКИ\n\n")
        for prefix, locations in sorted(self.prefix_handlers.items()):
            report_lines.append(f"### `{prefix}*`\n")
            for filepath, line_num in locations:
                report_lines.append(f"- `{filepath}:{line_num}`\n")
            report_lines.append("\n")
        
        return "".join(report_lines)


def main():
    """Запускает аудит и сохраняет отчет."""
    import sys
    import io
    # Устанавливаем UTF-8 для вывода
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    
    auditor = CallbackAuditor()
    print("Scanning files...")
    auditor.scan_directory()
    print(f"   Found callback_data: {len(auditor.callback_data_from_buttons)}")
    print(f"   Found handlers: {len(auditor.handlers_from_code)}")
    print(f"   Found prefix handlers: {len(auditor.prefix_handlers)}")
    
    print("\nGenerating report...")
    report = auditor.generate_report()
    
    report_path = Path("audit_report.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\nReport saved to {report_path}\n")
    print(report)


if __name__ == "__main__":
    main()
