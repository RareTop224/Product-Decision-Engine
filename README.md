# Product Decision Engine

Product Decision Engine прошёл Phase 0 и начал Фазу 1 — проверку PostgreSQL
Data Engine и возможности регулярно поддерживать evidence-backed данные.

Проект по-прежнему намеренно не содержит web-интерфейса, API и авторизации.
Факты хранятся с evidence, а фильтрация и денежные расчёты выполняются
детерминированным кодом. JSON Golden Dataset остаётся воспроизводимой эталонной
выборкой; PostgreSQL становится следующим хранилищем, но не новым источником
истины.

## Локальный запуск

Проект использует Python 3.11+.

```powershell
python -m pip install -e .
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python scripts/evaluate_phase0.py
```

Evaluation-команда читает JSON из `data/golden`, записывает воспроизводимый
отчёт в `reports/generated/phase0-report.md` и не изменяет исходный dataset.

## PostgreSQL bootstrap Фазы 1

Локальная проверка использует один PostgreSQL-контейнер без дополнительной
инфраструктуры:

```powershell
docker compose up -d postgres
$env:PDE_DATABASE_URL = "postgresql://pde:pde_local_only@localhost:55432/product_decision_engine"
python scripts/migrate_phase1.py
```

Команда применяет versioned SQL migrations, дважды импортирует Golden Dataset
и проверяет отсутствие дубликатов, равенство всех доменных сущностей и
побайтовое совпадение отчёта Фазы 0 после чтения из PostgreSQL.

## Текущий статус

Рабочий срез фиксирует предметную модель, hard constraints, TCO-математику,
валидацию evidence, диапазоны наблюдаемых цен и формат данных. Golden Dataset
содержит 30 реальных моделей семи брендов и 15 сценариев. Минимальный объём
выборки достигнут. После промежуточного `REASSESS` решающий тест проверил две
прямые пары на полной сетке объёма, доли цвета и горизонта с синхронными
корзинами KNS и Regard. Обе пары меняют победителя в зависимости от сценария;
продавцы совпадают в 23 из 24 и 24 из 24 точек. Полный учёт starter/maintenance
меняет победителя упрощённого TCO в 17 из 96 расчётов продавец × сценарий.

Решение по результатам Фазы 0 — продолжить к Data Engine. Текущий процессный
статус Фазы 1 после успешного PostgreSQL bootstrap —
`CONTINUE PHASE 1 / M2`, а не `GO` к публичному интерфейсу.
Полный контракт и отрицательные критерии описаны в
`docs/phase1-contract.md`. Главный риск теперь — не расчёт TCO и не миграция,
а доля моделей, для которых цены, наличие и OEM-корзины удастся регулярно
обновлять без чрезмерной ручной работы.
